"""SessionStart catchup: detect pending sids and fire sessionend_async for each.

CLI: python -m marrow.sessionstart_catchup

Pending = sessions in events with no sessionend_extract audit_log entry
(both ok and skip:short_session count as handled).
Spawns one popen_detach child per pending sid; does NOT wait.
"""
from __future__ import annotations

import sys
from pathlib import Path

from . import config, storage
from .popen_detach import popen_detach

_LOGS_DIR = Path.home() / ".config" / "marrow" / "logs"

_PENDING_SQL = """
SELECT DISTINCT session_id FROM events
WHERE session_id NOT IN (
  SELECT target_id FROM audit_log
  WHERE action = 'sessionend_extract'
    AND summary IN ('ok', 'skip:short_session')
)
"""


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001 — argv reserved for future flags
    db = config.db_path()
    conn = storage.connect(db)
    try:
        rows = conn.execute(_PENDING_SQL).fetchall()
    finally:
        conn.close()

    pending = [r["session_id"] for r in rows if r["session_id"]]
    for sid in pending:
        log_path = _LOGS_DIR / f"sessionend_async_{sid}.log"
        popen_detach(
            [sys.executable, "-m", "marrow.sessionend_async", "--sid", sid],
            log_path=log_path,
        )

    print(f"catchup: spawned {len(pending)} sessionend_async workers", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
