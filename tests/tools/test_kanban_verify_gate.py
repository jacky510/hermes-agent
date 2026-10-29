"""Verify-gate: kanban_complete replays a card's declared check.json before finishing.

The gate (``tools/kanban_verify_gate.py``, wired into ``_handle_complete`` before the
``complete_task`` write txn) must: refuse a completion whose replay exits non-zero
while leaving the card untouched; record the refusal as an audit event + card comment
with repair instructions; record would-rejects in shadow mode without blocking; force
bugfix cards to grow their regression-probe set past the dispatch-time baseline; and
stay a no-op for cards without a ``verify-gate:`` marker. Sandbox board per test —
never the real ``~/.hermes`` board.
"""
import json
import sys

import pytest


PASS_CMD = [sys.executable, "-c", "pass"]
FAIL_CMD = [sys.executable, "-c", "import sys; sys.exit(3)"]


@pytest.fixture
def board(monkeypatch, tmp_path):
    """Sandbox board + worker env for one claimed task; returns (ws, helpers)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_WORKSPACES_ROOT"):
        monkeypatch.delenv(var, raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    ws = tmp_path / "ws"
    ws.mkdir()

    def make_task(body):
        conn = kbc.connect()
        try:
            tid = kb.create_task(conn, title="gate card", assignee="test-worker", body=body)
            assert kb.claim_task(conn, tid) is not None
            run_id = kb._current_run_id(conn, tid)
        finally:
            conn.close()
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
        return tid, run_id

    def write_check(replay, probes=()):
        path = ws / "check.json"
        path.write_text(json.dumps({"gate": {
            "replay": [{"cmd": cmd, "cwd": "."} for cmd in replay],
            "probes": [{"id": pid, "cmd": PASS_CMD} for pid in probes],
        }}), encoding="utf-8")
        return path

    def events(tid, kind):
        conn = kbc.connect()
        try:
            return [e for e in kb.list_events(conn, tid) if e.kind == kind]
        finally:
            conn.close()

    def comments(tid):
        conn = kbc.connect()
        try:
            return kb.list_comments(conn, tid)
        finally:
            conn.close()

    def status(tid):
        conn = kbc.connect()
        try:
            return kb.get_task(conn, tid).status
        finally:
            conn.close()

    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(ws))
    from types import SimpleNamespace
    return SimpleNamespace(ws=ws, kb=kb, kbc=kbc, make_task=make_task,
                           write_check=write_check, events=events,
                           comments=comments, status=status)


def call_complete(board, tid, run_id, **overrides):
    from tools import kanban_tools as kt
    args = {"task_id": tid, "summary": "done work"}
    args.update(overrides)
    return json.loads(kt._handle_complete(args))


# --- Legs 1-2: unmarked cards are untouched; a green replay completes ----------

def test_unmarked_card_completes_without_gate_metadata(board, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_VERIFY_GATE", "enforce")
    tid, run_id = board.make_task("plain card, no marker\n")
    out = call_complete(board, tid, run_id)
    assert out.get("ok") is True
    assert "verify_gate" not in out


def test_green_replay_passes_and_records_metadata(board, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_VERIFY_GATE", "enforce")
    board.write_check([PASS_CMD])
    tid, run_id = board.make_task("verify-gate: check=check.json\n")
    out = call_complete(board, tid, run_id)
    assert out.get("ok") is True
    gate = out["verify_gate"]
    assert gate["verdict"] == "pass"
    assert gate["mode"] == "enforce"
    assert gate["replayed"] == 1
    assert board.status(tid) == "done"
    assert board.events(tid, "verify_gate_rejected") == []


# --- Legs 3-4: enforce refusals are structured, recorded, and leave the card ---

def test_failing_replay_rejects_and_leaves_card_inflight(board, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_VERIFY_GATE", "enforce")
    board.write_check([PASS_CMD, FAIL_CMD])
    tid, run_id = board.make_task("verify-gate: check=check.json\n")
    out = call_complete(board, tid, run_id)
    assert out.get("error")
    assert "still in-flight" in out["error"]
    assert "kanban_complete blocked" in out["error"]
    rejections = board.events(tid, "verify_gate_rejected")
    assert len(rejections) == 1
    assert rejections[0].payload["mode"] == "enforce"
    assert rejections[0].payload["commands"][-1]["rc"] == 3
    (comment,) = [c for c in board.comments(tid) if c.author == "verify-gate"]
    assert "rc=3" in comment.body
    assert "复跑命令" in comment.body
    assert board.status(tid) == "running"


def test_missing_check_json_fails_closed(board, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_VERIFY_GATE", "enforce")
    tid, run_id = board.make_task("verify-gate: check=missing.json\n")
    out = call_complete(board, tid, run_id)
    assert out.get("error")
    assert "GATE_INFRA_ERROR" in out["error"] or "check.json" in out["error"]
    assert board.events(tid, "verify_gate_rejected")
    assert board.status(tid) == "running"


# --- Leg 5: shadow records would-rejects without blocking ----------------------

def test_shadow_records_would_reject_without_blocking(board, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_VERIFY_GATE", "shadow")
    board.write_check([FAIL_CMD])
    tid, run_id = board.make_task("verify-gate: check=check.json\n")
    out = call_complete(board, tid, run_id)
    assert out.get("ok") is True
    assert out["verify_gate"]["verdict"] == "would_reject"
    (comment,) = [c for c in board.comments(tid) if c.author == "verify-gate"]
    assert "[shadow]" in comment.body
    assert board.events(tid, "verify_gate_rejected")
    assert board.status(tid) == "done"


# --- Leg 6: bugfix cards must grow the baseline probe set ----------------------

def _insert_baseline(board, tid, probe_ids):
    conn = board.kbc.connect()
    try:
        with board.kb.write_txn(conn):
            board.kb._append_event(conn, tid, "verify_gate_baseline",
                                   {"sha256": "x", "probe_ids": probe_ids, "recorded_at": 0})
    finally:
        conn.close()


def test_bugfix_without_new_probe_rejects(board, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_VERIFY_GATE", "enforce")
    board.write_check([PASS_CMD], probes=["a", "b"])
    tid, run_id = board.make_task("verify-gate: check=check.json kind=bugfix\n")
    _insert_baseline(board, tid, ["a", "b"])
    out = call_complete(board, tid, run_id)
    assert out.get("error")
    assert "probe" in out["error"]
    assert board.status(tid) == "running"


def test_bugfix_with_new_probe_passes(board, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_VERIFY_GATE", "enforce")
    board.write_check([PASS_CMD], probes=["a", "b", "c"])
    tid, run_id = board.make_task("verify-gate: check=check.json kind=bugfix\n")
    _insert_baseline(board, tid, ["a", "b"])
    out = call_complete(board, tid, run_id)
    assert out.get("ok") is True
    gate = out["verify_gate"]
    assert gate["verdict"] == "pass"
    assert gate["baseline"] == "diff"


def test_bugfix_missing_baseline_falls_back_to_regression_prefix(board, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_VERIFY_GATE", "enforce")
    board.write_check([PASS_CMD], probes=["a"])
    tid, run_id = board.make_task("verify-gate: check=check.json kind=bugfix\n")
    out = call_complete(board, tid, run_id)
    assert out.get("error")  # no regression: prefix and no baseline → refuse
    assert board.status(tid) == "running"

    board.write_check([PASS_CMD], probes=["regression: layout-overflow"])
    tid2, run_id2 = board.make_task("verify-gate: check=check.json kind=bugfix\n")
    out2 = call_complete(board, tid2, run_id2)
    assert out2.get("ok") is True
    assert out2["verify_gate"]["baseline"] == "missing-fallback"


# --- Leg 7: delivery cards must carry a verify-kit lint step -------------------

def test_delivery_without_lint_step_rejects_with_template(board, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_VERIFY_GATE", "enforce")
    board.write_check([PASS_CMD])
    tid, run_id = board.make_task("verify-gate: check=check.json delivery=app\n")
    out = call_complete(board, tid, run_id)
    assert out.get("error")
    assert "verify_kit.py" in out["error"] and "delivery" in out["error"]
    assert board.status(tid) == "running"


def test_delivery_with_matching_lint_step_passes(board, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_VERIFY_GATE", "enforce")
    (board.ws / "verify_kit.py").write_text("import sys; sys.exit(0)\n", encoding="utf-8")
    board.write_check([PASS_CMD, [sys.executable, "verify_kit.py", "delivery", "app"]])
    tid, run_id = board.make_task("verify-gate: check=check.json delivery=app\n")
    out = call_complete(board, tid, run_id)
    assert out.get("ok") is True
    assert out["verify_gate"]["verdict"] == "pass"


# --- Declaration parsing contracts ---------------------------------------------

def test_declaration_parsing_contracts():
    from tools.kanban_verify_gate import parse_declaration
    assert parse_declaration("no marker here") is None
    decl = parse_declaration("verify-gate: check=c.json delivery=app kind=bugfix timeout=30\n")
    assert decl == {"check": "c.json", "delivery": "app", "kind": "bugfix", "timeout": 30}
    assert parse_declaration("verify-gate: check=c.json\n")["kind"] == "code"
    assert parse_declaration("verify-gate:\n")["kind"] == "doc"
    with pytest.raises(ValueError):
        parse_declaration("verify-gate: check=a.json\nverify-gate: check=b.json\n")
    with pytest.raises(ValueError):
        parse_declaration("verify-gate: chk=a.json\n")
    with pytest.raises(ValueError):
        parse_declaration("verify-gate: kind=bugfix\n")  # bugfix needs check=
    with pytest.raises(ValueError):
        parse_declaration("verify-gate: timeout=abc\n")


def test_off_mode_skips_even_declared_cards(board, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_VERIFY_GATE", "off")
    board.write_check([FAIL_CMD])
    tid, run_id = board.make_task("verify-gate: check=check.json\n")
    out = call_complete(board, tid, run_id)
    assert out.get("ok") is True
    assert "verify_gate" not in out
    assert board.events(tid, "verify_gate_rejected") == []


# --- Dispatch-time baseline snapshot (D6, kanban_db_dispatch hook) --------------

def test_dispatch_baseline_records_probes_and_tolerates_missing_file(board, monkeypatch):
    from tools.kanban_verify_gate import record_dispatch_baseline
    body = "verify-gate: check=check.json kind=bugfix\n"
    tid, _ = board.make_task(body)
    board.write_check([PASS_CMD], probes=["a", "b"])
    conn = board.kbc.connect()
    try:
        task = board.kb.get_task(conn, tid)
    finally:
        conn.close()
    record_dispatch_baseline(task, str(board.ws), None)
    (baseline,) = board.events(tid, "verify_gate_baseline")
    assert baseline.payload["probe_ids"] == ["a", "b"]
    assert baseline.payload["sha256"]

    # Worker legitimately creates check.json after dispatch: empty baseline, still recorded.
    empty_ws = board.ws / "fresh-worker-ws"
    empty_ws.mkdir()
    tid2, _ = board.make_task(body)
    conn = board.kbc.connect()
    try:
        task2 = board.kb.get_task(conn, tid2)
    finally:
        conn.close()
    record_dispatch_baseline(task2, str(empty_ws), None)
    (baseline2,) = board.events(tid2, "verify_gate_baseline")
    assert baseline2.payload["probe_ids"] == []

    # Cards without a check= declaration record nothing.
    tid3, _ = board.make_task("no marker\n")
    conn = board.kbc.connect()
    try:
        task3 = board.kb.get_task(conn, tid3)
    finally:
        conn.close()
    record_dispatch_baseline(task3, str(board.ws), None)
    assert board.events(tid3, "verify_gate_baseline") == []
