"""Kanban verify gate — pre-completion replay enforcement for cards that declare one.

A card body may carry a one-line marker::

    verify-gate: check=<path> [delivery=<dir>] [kind=bugfix|delivery|code|doc] [timeout=<s>]

When it does, ``kanban_complete`` replays the commands listed in that check.json's
``gate.replay`` (argv lists, run under the task workspace) *before* the write
transaction: every exit code must be 0 or the completion is refused with the card
left untouched (same contract as the ``HallucinatedCardsError`` family). Three
gates, evaluated in order: check replay (D4), delivery-lint presence (D5), and —
for ``kind=bugfix`` — a probe-set diff against the dispatch-time baseline event
(D6). Failures land an audit event and a card comment before the structured
refusal, so the board keeps the evidence and the worker gets repair instructions.

The platform knows nothing about verify-kit: it reads only ``gate.replay`` /
``gate.probes`` and exit codes, so the verification runner can change without
touching this module. Modes: ``off`` (silent), ``shadow`` (record + comment, never
block), ``enforce`` (block). Resolution order per call: ``HERMES_KANBAN_VERIFY_GATE``
env override, then ``kanban.verify_gate_mode`` config (default shadow) — a boolean
``False`` there (what ``config set kanban.verify_gate_mode off`` actually stores,
via the CLI's scalar-word coercion on this unregistered key) reads as ``off``.
Enforce blocks only on enforce-level delivery trees; ``backend``/``tools``/
``design`` are warn trees — recorded + commented, never blocking (P6). Cards with
no marker are a <1ms line scan and nothing else.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path, PurePath
from typing import Any, Optional

logger = logging.getLogger(__name__)

MODES = ("off", "shadow", "enforce")
ENV_OVERRIDE = "HERMES_KANBAN_VERIFY_GATE"
MODE_CONFIG_KEY = "verify_gate_mode"
TIMEOUT_CONFIG_KEY = "verify_gate_timeout"
DEFAULT_TIMEOUT_BUDGET = 1200
MAX_TIMEOUT_BUDGET = 3600
PER_COMMAND_TIMEOUT = 600
DECLARATION_PREFIX = "verify-gate:"
KNOWN_KEYS = frozenset({"check", "delivery", "kind", "timeout"})
KINDS = frozenset({"code", "bugfix", "delivery", "doc"})
BASELINE_EVENT = "verify_gate_baseline"
REJECTED_EVENT = "verify_gate_rejected"
INFRA_ESCALATION_AFTER = 3

_DELIVERY_RUNNER_TOKEN = "verify_kit.py"


class DeclarationError(ValueError):
    """A malformed or ambiguous ``verify-gate:`` marker (fail-closed)."""


class GateError(Exception):
    """A gate condition failed; ``infra`` marks environment faults (D8)."""

    def __init__(self, message: str, *, infra: bool = False):
        super().__init__(message)
        self.infra = infra


class GateInfraError(GateError):
    """Environment fault (unreadable check.json, spawn failure) — still fail-closed."""

    def __init__(self, message: str):
        super().__init__(message, infra=True)


# --- Declaration parsing (§4.2) — also imported by kanban_db_dispatch ----------

def parse_declaration(body: str) -> Optional[dict]:
    """Parse the card's ``verify-gate:`` marker; ``None`` when the card has none.

    First match wins by design; a *second* marker line is ambiguous and raises.
    Keys: ``check`` (check.json path), ``delivery`` (deliverable dir), ``kind``
    (defaults to ``code`` when ``check=`` is present, ``delivery`` when only
    ``delivery=`` is, ``doc`` otherwise), ``timeout`` (seconds, replay budget).
    """
    hits = [ln.strip() for ln in (body or "").splitlines()
            if ln.strip().lower().startswith(DECLARATION_PREFIX)]
    if not hits:
        return None
    if len(hits) > 1:
        raise DeclarationError(
            f"ambiguous verify-gate declaration: {len(hits)} '{DECLARATION_PREFIX}' lines; "
            "keep exactly one")
    decl: dict[str, Any] = {"check": None, "delivery": None, "kind": None, "timeout": None}
    text = hits[0][len(DECLARATION_PREFIX):].strip()
    for token in text.split():
        key, sep, value = token.partition("=")
        key = key.lower()
        if not sep or not value:
            raise DeclarationError(
                f"malformed verify-gate token {token!r} (expected key=value)")
        if key not in KNOWN_KEYS:
            raise DeclarationError(
                f"unknown verify-gate key {key!r}; valid keys: "
                f"{', '.join(sorted(KNOWN_KEYS))}")
        if key == "timeout":
            try:
                seconds = int(value)
            except ValueError:
                raise DeclarationError(f"verify-gate timeout={value!r} is not an integer") from None
            if seconds <= 0:
                raise DeclarationError(f"verify-gate timeout={seconds} must be positive")
            decl["timeout"] = seconds
        else:
            decl[key] = value
    if decl["kind"] is None:
        decl["kind"] = "code" if decl["check"] else ("delivery" if decl["delivery"] else "doc")
    if decl["kind"] not in KINDS:
        raise DeclarationError(
            f"unknown verify-gate kind {decl['kind']!r}; valid kinds: {', '.join(sorted(KINDS))}")
    if decl["kind"] != "doc" and not decl["check"]:
        raise DeclarationError(
            f"kind={decl['kind']} requires check=<path to check.json>; "
            "use kind=doc to declare a card with no verification duty")
    return decl


def resolve_path(raw: str, workspace: Optional[Path] = None) -> Path:
    """Absolute paths pass through; relative paths anchor on the worker workspace."""
    path = Path(raw)
    if path.is_absolute():
        return path
    return (workspace or _workspace()) / path


def baseline_snapshot_payload(decl: dict, workspace: str) -> Optional[dict]:
    """Dispatch-time probe baseline for a declared check.json (D6), or ``None``
    when there is nothing worth recording. A missing file snapshots an empty
    probe set (the worker legitimately creates check.json later); an unparseable
    file records nothing, so completion falls back to the ``regression:`` rule."""
    check_path = resolve_path(decl["check"], Path(workspace))
    payload: dict[str, Any] = {
        "check_path": str(check_path), "sha256": None, "probe_ids": [],
        "recorded_at": int(time.time())}
    try:
        raw = check_path.read_bytes()
    except OSError:
        return payload
    try:
        contract = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    gate = contract.get("gate") if isinstance(contract, dict) else None
    probes = gate.get("probes") if isinstance(gate, dict) else None
    if isinstance(probes, list):
        payload["probe_ids"] = [str(p.get("id")) for p in probes
                                if isinstance(p, dict) and p.get("id")]
    payload["sha256"] = hashlib.sha256(raw).hexdigest()
    return payload


def record_dispatch_baseline(task: Any, workspace: str, board: Any = None) -> None:
    """Dispatcher-side hook (called from ``kanban_db_dispatch._default_spawn`` right
    before the worker Popen): freeze the card's declared check.json probe ids into a
    ``verify_gate_baseline`` audit event so a ``kind=bugfix`` completion must add at
    least one new probe against this baseline (D6). Best-effort by contract: any
    failure logs a warning and spawns anyway — with no baseline event the
    completion-side gate falls back to the ``regression:``-prefix rule."""
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        decl = parse_declaration(task.body or "")
        if not decl or not decl.get("check"):
            return
        payload = baseline_snapshot_payload(decl, workspace)
        if payload is None:
            return
        conn = kbc.connect(board=board)
        try:
            with kb.write_txn(conn):
                kb._append_event(conn, task.id, BASELINE_EVENT, payload)
        finally:
            conn.close()
        logger.info("verify-gate baseline recorded for %s (%d probes)",
                    task.id, len(payload.get("probe_ids") or []))
    except Exception:
        logger.warning("verify-gate baseline snapshot failed for %s; spawning anyway",
                       task.id, exc_info=True)


# --- Mode / budget resolution (§4.9) ------------------------------------------

def _workspace() -> Path:
    return Path(os.environ["HERMES_KANBAN_WORKSPACE"]) \
        if os.environ.get("HERMES_KANBAN_WORKSPACE") else Path.cwd()


def _config() -> Any:
    from hermes_cli.config import load_config
    return load_config()


def gate_mode() -> str:
    """``HERMES_KANBAN_VERIFY_GATE`` env override, else ``kanban.verify_gate_mode``."""
    raw = os.environ.get(ENV_OVERRIDE, "").strip().lower()
    if raw in MODES:
        return raw
    from hermes_cli.config import cfg_get
    try:
        mode = cfg_get(_config(), "kanban", MODE_CONFIG_KEY, default="shadow")
    except Exception:
        # An unreadable config must not take the board's completion path down;
        # shadow is the safe default (records, never blocks).
        logger.debug("verify-gate: config read failed; defaulting mode", exc_info=True)
        return "shadow"
    if mode is False:
        # `hermes config set kanban.verify_gate_mode off` stores the boolean False:
        # the key is unregistered in DEFAULT_CONFIG, so the CLI's scalar-word table
        # coerces "off" (same for false/no/0). Honor it as the off it spelled; any
        # other unlisted value still fails safe to shadow.
        return "off"
    return mode if mode in MODES else "shadow"


def _timeout_budget(decl_timeout: Optional[int]) -> int:
    if decl_timeout:
        return min(decl_timeout, MAX_TIMEOUT_BUDGET)
    from hermes_cli.config import cfg_get
    try:
        configured = cfg_get(_config(), "kanban", TIMEOUT_CONFIG_KEY, default=DEFAULT_TIMEOUT_BUDGET)
    except Exception:
        logger.debug("verify-gate: config read failed; defaulting budget", exc_info=True)
        configured = DEFAULT_TIMEOUT_BUDGET
    return min(int(configured or DEFAULT_TIMEOUT_BUDGET), MAX_TIMEOUT_BUDGET)


# --- check.json gate contract (§4.3) ------------------------------------------

def _load_gate_contract(check_path: Path) -> dict:
    """Read only the top-level ``gate`` object; everything else belongs to the runner."""
    try:
        raw = check_path.read_bytes()
    except OSError as exc:
        raise GateInfraError(f"check.json not readable at {check_path}: {exc}") from exc
    try:
        contract = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise GateInfraError(f"check.json at {check_path} is not valid JSON: {exc}") from exc
    if not isinstance(contract, dict):
        raise GateInfraError(f"check.json at {check_path} must be a JSON object")
    gate = contract.get("gate")
    if not isinstance(gate, dict):
        raise GateError("check.json is missing the top-level \"gate\" object the gate replays")
    replay = gate.get("replay")
    if not isinstance(replay, list) or not replay:
        raise GateError("check.json gate.replay must be a non-empty array of {cmd, cwd?} steps")
    for i, step in enumerate(replay):
        if not isinstance(step, dict) or not isinstance(step.get("cmd"), list) \
                or not step["cmd"] or not all(isinstance(a, str) and a for a in step["cmd"]):
            raise GateError(
                f"gate.replay[{i}] must carry cmd as a non-empty argv list of strings "
                "(shell strings are not accepted)")
        if "cwd" in step and not isinstance(step["cwd"], str):
            raise GateError(f"gate.replay[{i}] cwd must be a string")
    probes = gate.get("probes") or []
    if not isinstance(probes, list):
        raise GateError("check.json gate.probes must be an array of {id, cmd}")
    return {"replay": replay,
            "probe_ids": [str(p.get("id")) for p in probes
                          if isinstance(p, dict) and p.get("id")]}


# --- Replay execution (§4.4) ---------------------------------------------------

def _run_replay(replay: list, workspace: Path, budget: float, started: float) -> tuple[list, Optional[GateError]]:
    """Run steps until the first non-zero exit / timeout / spawn failure. Returns
    (per-command results, failure-or-None). Results carry rc, elapsed, an output
    tail and whether the step failed for infra reasons."""
    results: list[dict] = []
    for i, step in enumerate(replay):
        remaining = budget - (time.monotonic() - started)
        if remaining <= 0:
            return results, GateError(
                f"replay exceeded its {int(budget)}s budget after {len(results)}/{len(replay)} "
                "commands; move long suites out of the completion gate (they belong in CI)")
        cwd = resolve_path(step.get("cwd") or ".", workspace)
        timeout = max(1, min(PER_COMMAND_TIMEOUT, int(remaining)))
        argv = step["cmd"]
        t0 = time.monotonic()
        infra = not cwd.is_dir()
        if infra:
            rc, tail, err = None, "", f"cwd does not exist: {cwd}"
        else:
            try:
                proc = subprocess.run(
                    argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                    check=False)
                rc, tail, err = proc.returncode, (proc.stdout or "") + (proc.stderr or ""), None
            except subprocess.TimeoutExpired:
                rc, tail, err = None, "", f"timed out after {timeout}s"
            except OSError as exc:
                rc, tail, err = None, "", str(exc)
                infra = True
        entry = {
            "index": i, "cmd": argv, "rc": rc, "infra": infra,
            "elapsed_s": round(time.monotonic() - t0, 2),
            "tail": tail[-2000:],
        }
        results.append(entry)
        if rc != 0:
            detail = err if err else f"exit code {rc}"
            message = (f"replay command {i + 1}/{len(replay)} failed: "
                       f"{' '.join(argv)} → {detail}")
            return results, (GateInfraError(message) if infra else GateError(message))
    return results, None


# --- Delivery-lint presence (§4.5) --------------------------------------------

def _find_delivery_step(replay: list, delivery_dir: str, workspace: Path) -> Optional[dict]:
    """The declared delivery dir must be linted by a verify-kit ``delivery`` step —
    existence check only; the platform never substitutes its own lint."""
    want = os.path.normcase(os.path.normpath(str(resolve_path(delivery_dir, workspace))))
    for step in replay:
        cmd = step["cmd"]
        if _DELIVERY_RUNNER_TOKEN not in " ".join(cmd) or "delivery" not in cmd:
            continue
        if os.path.normcase(os.path.normpath(str(resolve_path(cmd[-1], workspace)))) == want:
            return step
    return None


_DELIVERY_TEMPLATE = (
    '{"cmd": ["python", "<path to verify_kit.py>", "delivery", "<delivery dir>"], "cwd": "."}')


# --- Probe diff (§4.6) ----------------------------------------------------------

def _baseline_probe_ids(conn, tid: str) -> Optional[list]:
    """Latest dispatch-time baseline probe ids, or ``None`` when no baseline event
    exists (card dispatched before the gate shipped / check.json absent at spawn)."""
    from hermes_cli import kanban_db as kb
    events = [e for e in kb.list_events(conn, tid) if e.kind == BASELINE_EVENT]
    if not events:
        return None
    payload = events[-1].payload or {}
    ids = payload.get("probe_ids")
    return [str(i) for i in ids] if isinstance(ids, list) else []


def _check_probe_diff(probe_ids: list, baseline: Optional[list]) -> Optional[str]:
    """``None`` when the bugfix gate passes; else the refusal reason."""
    current = set(probe_ids)
    if baseline is None:
        if not any(pid.startswith("regression:") for pid in probe_ids):
            return ("no dispatch-time probe baseline exists and no gate.probes id carries the "
                    "\"regression:\" prefix — declare at least one regression probe "
                    "(P3 fallback) so the fix is pinned by a test")
        return None
    missing = current - set(baseline)
    if not missing:
        return ("bugfix gate: probe set did not grow past the dispatch-time baseline "
                f"{sorted(set(baseline))} — add at least one new regression probe "
                f"(current: {sorted(current)})")
    return None


# --- Failure recording (§4.7) ---------------------------------------------------

def _sha256_of(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _failures_comment(decl: dict, trigger: str, failure: GateError, results: list,
                      check_sha256: Optional[str], mode: str, tid: str,
                      escalate: bool, level: str = "enforce") -> str:
    last = results[-1] if results else None
    if mode == "shadow":
        tag, verdict_txt = "[shadow]", "将被拒绝（shadow 记录）"
    elif level == "warn":
        tag, verdict_txt = "[warn]", "不拦截（warn 交付树：只记审计+评注）"
    else:
        tag, verdict_txt = "", "拒绝"
    level_note = f", level={level}" if level == "warn" else ""
    lines = [
        f"[verify-gate]{tag} kanban_complete "
        f"{verdict_txt}（task {tid}, mode={mode}{level_note}）",
        f"- 触发项：{trigger}",
    ]
    if last:
        rendered = " ".join(last["cmd"])
        detail = "超时" if last["rc"] is None else f"rc={last['rc']}"
        lines.append(f"- 命令：{rendered} → {detail} ({last['elapsed_s']}s)")
        if last["tail"]:
            tail = last["tail"].rstrip()[-800:]
            lines.append("- 失败输出尾部：\n```\n" + tail + "\n```")
    if check_sha256:
        lines.append(f"- check.json sha256: {check_sha256[:16]}…")
    if decl.get("check"):
        rerun = f"cd <workspace> && {last and ' '.join(last['cmd']) or '<replay 命令>'}"
        lines.append(f"- 复跑命令（worker 本地验证用）：{rerun}")
    if failure.infra:
        lines.append("- 类别：GATE_INFRA_ERROR（环境/声明故障，非验证失败）")
    if escalate:
        lines.append(
            f"- ⚠ GATE_INFRA_ERROR 已连续 ≥{INFRA_ESCALATION_AFTER} 次：疑似环境故障。"
            "请 operator 介入（修复环境或 `hermes kanban complete --force`），worker 停止重试。")
    lines.append("- 卡片仍为 in-flight，本次 kanban_complete 未产生任何状态变更。")
    return "\n".join(lines)


def _consecutive_infra_rejections(conn, tid: str) -> int:
    from hermes_cli import kanban_db as kb
    count = 0
    for event in reversed(kb.list_events(conn, tid)):
        if event.kind != REJECTED_EVENT:
            break
        if (event.payload or {}).get("infra"):
            count += 1
        else:
            break
    return count


def _record_failure(conn, tid: str, mode: str, trigger: str, failure: GateError,
                    results: list, check_sha256: Optional[str], decl: dict,
                    level: str = "enforce") -> None:
    """Audit event + card comment. Neither may block the refusal itself (comment
    write failures degrade to a warning log)."""
    from agent.redact import redact_sensitive_text
    from hermes_cli import kanban_db as kb
    escalate = failure.infra and _consecutive_infra_rejections(conn, tid) + 1 >= INFRA_ESCALATION_AFTER
    payload = {
        "mode": mode, "level": level, "trigger": trigger, "infra": failure.infra,
        "check_sha256": check_sha256,
        "commands": [{k: r[k] for k in ("cmd", "rc", "elapsed_s", "infra")} for r in results],
        "decl": {k: decl.get(k) for k in ("check", "delivery", "kind", "timeout")},
    }
    try:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, REJECTED_EVENT, payload)
    except Exception:
        logger.warning("verify-gate: could not land %s event on %s", REJECTED_EVENT, tid, exc_info=True)
    try:
        kb.add_comment(conn, tid, author="verify-gate", body=redact_sensitive_text(
            _failures_comment(decl, trigger, failure, results, check_sha256, mode, tid,
                              escalate, level)))
    except Exception:
        logger.warning("verify-gate: could not comment failure report on %s", tid, exc_info=True)


def _reject(message: str):
    """Structured refusal in the shape of the ``HallucinatedCardsError`` family
    (still in-flight, repair instructions, no kanban_block nudge)."""
    from tools.kanban_tools import _Reject  # lazy: kanban_tools imports this module
    return _Reject(message)


# --- Delivery-tree gate levels (P6) ----------------------------------------------

# backend/tools/design stay warn until their lint budgets are re-baselined
# (delivery-tree-gate-levels, t_b541fe02): the gate records + comments but never
# blocks there, even in enforce mode. Everything else — cards with no delivery
# declaration and unlisted trees — stays enforce (fail-safe: the gate keeps its
# full blocking power anywhere the levels are not explicitly relaxed).
WARN_DELIVERY_TREES = frozenset({"backend", "tools", "design"})


def delivery_tree_level(decl: dict) -> str:
    """``"warn"`` when the marker's ``delivery=`` names a warn tree, else ``"enforce"``.

    The tree is the first path component of the declared delivery dir (Windows or
    POSIX separators both normalize); cards without a ``delivery=`` declaration and
    unlisted trees default to ``"enforce"``.
    """
    delivery = str(decl.get("delivery") or "").replace("\\", "/").strip().strip("/")
    tree = delivery.split("/", 1)[0].lower() if delivery else ""
    return "warn" if tree in WARN_DELIVERY_TREES else "enforce"


# --- Entry point ----------------------------------------------------------------

def verify_gate(conn, task, tid: str, *, mode: Optional[str] = None,
                timeout_budget: Optional[int] = None) -> Optional[dict]:
    """Run the declared gates before ``kb.complete_task``. Returns metadata to merge
    into the completion (``pass`` / shadow ``would_reject``), ``None`` for cards the
    gate does not apply to, and raises a structured reject in enforce mode on failure."""
    decl = None
    try:
        try:
            decl = parse_declaration(task.body or "")
        except DeclarationError as exc:
            # A malformed marker is fail-closed too (D8); record it like any other gate
            # failure so shadow mode still surfaces the bad declaration.
            record_decl = {"check": None, "delivery": None, "kind": None, "timeout": None}
            mode = mode or gate_mode()
            if mode != "off":
                _record_failure(conn, tid, mode, "verify-gate 声明解析", GateError(str(exc)),
                                [], None, record_decl)
            if mode == "enforce":
                raise _reject(
                    f"kanban_complete blocked by verify-gate (task {tid}): {exc}. Your task is "
                    "still in-flight (no state change). Fix the verify-gate declaration line in "
                    "the card body, then retry kanban_complete with the same handoff.") from exc
            return {"mode": mode, "verdict": "would_reject", "error": str(exc)}
        if decl is None:
            return None
        mode = mode or gate_mode()
        if mode == "off" or decl["kind"] == "doc":
            return None
        level = delivery_tree_level(decl)
        started = time.monotonic()
        workspace = _workspace()
        check_path = resolve_path(decl["check"], workspace)
        check_sha256 = _sha256_of(check_path)
        contract = _load_gate_contract(check_path)
        budget = float(timeout_budget or _timeout_budget(decl.get("timeout")))
        failure: Optional[GateError] = None
        trigger = ""
        results, failure = _run_replay(contract["replay"], workspace, budget, started)
        if failure:
            trigger = "check 重放"
        if not failure and decl.get("delivery"):
            step = _find_delivery_step(contract["replay"], decl["delivery"], workspace)
            if step is None:
                failure = GateError(
                    f"delivery={decl['delivery']} declared but gate.replay has no verify-kit "
                    f"delivery step for that directory; add e.g. {_DELIVERY_TEMPLATE}")
                trigger = "delivery lint 缺失"
        if not failure and decl["kind"] == "bugfix":
            reason = _check_probe_diff(
                contract["probe_ids"], _baseline_probe_ids(conn, tid))
            if reason:
                failure = GateError(reason)
                trigger = "bugfix 回归探针 diff"
        elapsed = round(time.monotonic() - started, 2)
        if failure:
            _record_failure(conn, tid, mode, trigger, failure, results, check_sha256,
                            decl, level)
            if mode == "enforce" and level == "enforce":
                raise _reject(
                    f"kanban_complete blocked by verify-gate (mode=enforce, task {tid}): "
                    f"{failure}. Your task is still in-flight (no state change) — this is a "
                    "verification refusal, not a failed completion. Fix the failing check, "
                    "re-run the replay locally until every exit code is 0 (see the verify-gate "
                    "comment on the card for the exact command), then retry kanban_complete "
                    "with the same handoff. Do NOT kanban_block for a verification failure.")
            verdict = "would_reject"
        else:
            verdict = "pass"
        meta = {"mode": mode, "level": level, "verdict": verdict,
                "check_sha256": check_sha256, "replayed": len(results),
                "elapsed_s": elapsed}
        if decl["kind"] == "bugfix":
            baseline = _baseline_probe_ids(conn, tid)
            meta["baseline"] = "diff" if baseline is not None else "missing-fallback"
            meta["probes"] = len(contract["probe_ids"])
        if decl.get("delivery"):
            meta["delivery"] = decl["delivery"]
        return meta
    except GateError as exc:
        # Contract/deck failures raised outside _run_replay still owe the board a
        # record before the refusal (D7: steps 1+3 happen even for hard failures).
        if decl is not None and mode != "off":
            level = delivery_tree_level(decl)
            try:
                _record_failure(conn, tid, mode or gate_mode(),
                                "check.json 契约", exc, [], None, decl, level)
            except Exception:
                logger.warning("verify-gate: failure recording itself failed", exc_info=True)
            if (mode or gate_mode()) == "enforce" and level == "enforce":
                raise _reject(
                    f"kanban_complete blocked by verify-gate (task {tid}): {exc}. Your task is "
                    "still in-flight (no state change). Repair the declaration/contract named "
                    "above, then retry kanban_complete with the same handoff.") from exc
            return {"mode": mode or gate_mode(), "level": level,
                    "verdict": "would_reject", "error": str(exc)}
        raise _reject(f"kanban_complete blocked by verify-gate: {exc}") from exc
