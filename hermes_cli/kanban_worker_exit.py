"""Worker exit-code classification for the kanban dispatcher's crash accounting.

Split from ``kanban_db_dispatch`` (that file sits over its file-lines ratchet, so
it may only shrink; moved code keeps its cap). No external caller holds a patch
seam on these — the dispatch module imports them back and its own seam functions
(``_classify_worker_exit`` and friends) stay put.
"""
from __future__ import annotations


def exit_code_kind(code: int) -> tuple[str, int]:
    """``(kind, code)`` for a worker's exit code, however it was observed."""
    from hermes_cli import kanban_db as _kb

    if code == 0:
        return ("clean_exit", 0)
    if code == _kb.KANBAN_RATE_LIMIT_EXIT_CODE:
        return ("rate_limited", code)
    if code == _kb.KANBAN_TERMINAL_PROVIDER_EXIT_CODE:
        return ("terminal_provider", code)
    return ("nonzero_exit", code)
