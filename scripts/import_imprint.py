"""
One-shot import script: Telegram conversation log (imprint bot) → marrow events.

Usage:
  python scripts/import_imprint.py              # dry-run (default)
  python scripts/import_imprint.py --apply      # write to DB
  python scripts/import_imprint.py --src-jsonl /path/to/dir --apply  # also import CC transcripts

Defaults:
  --src-sqlite  /tmp/cloud-daddy/memory.db
  --db          /Users/born_blazing_bright/.config/marrow/marrow.db
"""

import argparse
import glob
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_SRC_SQLITE = "/tmp/cloud-daddy/memory.db"
DEFAULT_DB = "/Users/born_blazing_bright/.config/marrow/marrow.db"

_TZ_SHANGHAI = timezone(timedelta(hours=8))

# ---------------------------------------------------------------------------
# Noise filters (for --src-jsonl CC transcript phase, copied from import_cyberboss)
# ---------------------------------------------------------------------------
_NOISE_PREFIXES = (
    "WECHAT SESSION INSTRUCTIONS",
    "Caveat:",
    "SYSTEM ACTION MODE",
    "<local-command-",
    "<bash-",
)
_NOISE_SUBSTRINGS = (
    "<task-notification>",
    "<system-reminder>",
    "<command-name>",
)
_ROUTE_HEADER_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]\s*")
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$")


def _unwrap_envelope(text: str) -> "tuple[Optional[str], str]":
    s = _JSON_FENCE_RE.sub("", text.strip()).strip()
    if not s.startswith("{"):
        return text, ""
    try:
        j = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return text, ""
    if not isinstance(j, dict) or "action" not in j:
        return text, ""
    if j.get("action") == "send_message" and j.get("message"):
        return str(j["message"]), ""
    return None, "envelope_no_message"


def _extract_text(obj: dict) -> "tuple[Optional[str], str]":
    role = obj.get("type")
    content = obj.get("message", {}).get("content", "")

    if role == "user":
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            if any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content
            ):
                return None, "tool_result_block"
            text = "\n".join(
                b["text"]
                for b in content
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
            )
        else:
            return None, "unknown_content_type"
        text = _ROUTE_HEADER_RE.sub("", text)

    elif role == "assistant":
        if not isinstance(content, list):
            return None, "no_text_block"
        text_parts = [
            b["text"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
        ]
        if not text_parts:
            return None, "no_text_block"
        text = "\n".join(text_parts)
        text, reason = _unwrap_envelope(text)
        if text is None:
            return None, reason
    else:
        return None, "unexpected_role"

    text = text.strip()
    if not text:
        return None, "empty_after_strip"

    for prefix in _NOISE_PREFIXES:
        if text.startswith(prefix):
            return None, "noise_prefix"
    for sub in _NOISE_SUBSTRINGS:
        if sub in text:
            return None, "noise_substring"

    return text, ""


# ---------------------------------------------------------------------------
# Parse CC transcripts (--src-jsonl phase)
# ---------------------------------------------------------------------------
def parse_transcripts(src_dir: str) -> tuple[list[dict], dict]:
    files = sorted(glob.glob(src_dir + "/*.jsonl"))
    events: list[dict] = []
    skips: dict[str, int] = {}
    parse_errors = 0

    for fpath in files:
        with open(fpath, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    parse_errors += 1
                    continue

                role = obj.get("type")
                if role not in ("user", "assistant"):
                    skips["wrong_type"] = skips.get("wrong_type", 0) + 1
                    continue
                if obj.get("isMeta") is True:
                    skips["is_meta"] = skips.get("is_meta", 0) + 1
                    continue
                if obj.get("isSidechain") is True:
                    skips["is_sidechain"] = skips.get("is_sidechain", 0) + 1
                    continue

                text, reason = _extract_text(obj)
                if text is None:
                    skips[reason] = skips.get(reason, 0) + 1
                    continue

                uuid = obj.get("uuid")
                if not uuid:
                    skips["no_uuid"] = skips.get("no_uuid", 0) + 1
                    continue

                events.append(
                    {
                        "session_id": obj.get("sessionId", ""),
                        "timestamp": obj.get("timestamp", ""),
                        "role": role,
                        "content": text,
                        "channel": "imprint",
                        "compressed": 0,
                        "source_hash": uuid,
                    }
                )

    if parse_errors:
        skips["parse_error"] = parse_errors

    return events, skips


# ---------------------------------------------------------------------------
# Parse Telegram sqlite source
# ---------------------------------------------------------------------------
_DIRECTION_MAP = {"in": "user", "out": "assistant"}


def _shanghai_to_utc(naive_str: str) -> str:
    """'YYYY-MM-DD HH:MM:SS' (Shanghai +08:00) → 'YYYY-MM-DDTHH:MM:SSZ'"""
    dt_local = datetime.strptime(naive_str, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=_TZ_SHANGHAI
    )
    dt_utc = dt_local.astimezone(timezone.utc)
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_sqlite(src_db: str) -> tuple[list[dict], dict]:
    skips: dict[str, int] = {}
    events: list[dict] = []

    con = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT id, direction, content, session_id, created_at "
            "FROM conversation_log "
            "WHERE platform='telegram' AND is_test=0 "
            "ORDER BY id"
        ).fetchall()
    finally:
        con.close()

    for row_id, direction, content, session_id, created_at in rows:
        # empty/whitespace content
        if not content or not content.strip():
            skips["empty_content"] = skips.get("empty_content", 0) + 1
            continue

        # unknown direction
        role = _DIRECTION_MAP.get(direction)
        if role is None:
            skips["unknown_direction"] = skips.get("unknown_direction", 0) + 1
            continue

        sid = session_id.strip() if session_id and session_id.strip() else "imprint-tg"
        ts = _shanghai_to_utc(created_at)

        events.append(
            {
                "session_id": sid,
                "timestamp": ts,
                "role": role,
                "content": content.strip(),
                "channel": "imprint",
                "compressed": 0,
                "source_hash": f"imprint-tg-{row_id}",
            }
        )

    return events, skips


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def load_existing_hashes(con: sqlite3.Connection) -> set[str]:
    cur = con.execute(
        "SELECT source_hash FROM events WHERE channel='imprint' AND source_hash IS NOT NULL"
    )
    return {row[0] for row in cur.fetchall()}


def insert_events(con: sqlite3.Connection, rows: list[dict]) -> None:
    con.executemany(
        """
        INSERT INTO events
            (session_id, timestamp, role, content, channel, compressed, source_hash)
        VALUES
            (:session_id, :timestamp, :role, :content, :channel, :compressed, :source_hash)
        """,
        rows,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Import imprint data into marrow DB")
    parser.add_argument("--src-sqlite", default=DEFAULT_SRC_SQLITE, help="Source memory.db path")
    parser.add_argument("--src-jsonl", default=None, help="CC transcript .jsonl directory (optional)")
    parser.add_argument("--db", default=DEFAULT_DB, help="marrow.db path")
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually write to DB (default is dry-run)",
    )
    args = parser.parse_args()

    dry_run = not args.apply

    print(f"Mode: {'DRY-RUN' if dry_run else 'APPLY'}")
    print(f"src-sqlite: {args.src_sqlite}")
    if args.src_jsonl:
        print(f"src-jsonl:  {args.src_jsonl}")
    print(f"db:         {args.db}")
    print()

    # Parse sqlite source
    print("Parsing Telegram sqlite source...")
    tg_events, tg_skips = parse_sqlite(args.src_sqlite)
    print(f"  candidate events: {len(tg_events)}")
    print()

    # Parse CC transcripts (optional)
    jl_events: list[dict] = []
    jl_skips: dict[str, int] = {}
    if args.src_jsonl:
        print("Parsing CC transcripts (jsonl)...")
        jl_events, jl_skips = parse_transcripts(args.src_jsonl)
        print(f"  candidate events: {len(jl_events)}")
        print()

    # Connect and deduplicate
    con = sqlite3.connect(args.db)
    try:
        existing_hashes = load_existing_hashes(con)

        new_tg = [e for e in tg_events if e["source_hash"] not in existing_hashes]
        dup_tg = len(tg_events) - len(new_tg)

        new_jl = [e for e in jl_events if e["source_hash"] not in existing_hashes]
        dup_jl = len(jl_events) - len(new_jl)

        print("=== DRY-RUN STATISTICS ===" if dry_run else "=== APPLY STATISTICS ===")
        print(f"Telegram rows to insert:      {len(new_tg)}")
        print(f"  skipped (duplicate hash):   {dup_tg}")
        print(f"  skipped (filter):           {sum(tg_skips.values())}")
        for reason, count in sorted(tg_skips.items()):
            print(f"    {reason}: {count}")
        print()
        if args.src_jsonl:
            print(f"JSONL events to insert:       {len(new_jl)}")
            print(f"  skipped (duplicate hash):   {dup_jl}")
            print(f"  skipped (parse/filter):     {sum(jl_skips.values())}")
            for reason, count in sorted(jl_skips.items()):
                print(f"    {reason}: {count}")
            print()

        # Sample 3 rows
        print("--- 3 sample Telegram events (content truncated to 100 chars) ---")
        for ev in new_tg[:3]:
            snippet = ev["content"][:100].replace("\n", "↵")
            print(f"  [{ev['role']}] {ev['timestamp']} | {snippet}")
        print()

        if dry_run:
            print("Dry-run complete. Pass --apply to write to DB.")
            return

        # Apply
        print("Writing to DB...")
        with con:
            if new_tg:
                insert_events(con, new_tg)
            if new_jl:
                insert_events(con, new_jl)

        total = len(new_tg) + len(new_jl)
        print(f"Done. Inserted {total} events.")

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        con.close()


if __name__ == "__main__":
    main()
