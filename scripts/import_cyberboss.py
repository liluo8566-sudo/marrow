"""
One-shot import script: CC transcripts + diary → marrow events/diary tables.

Usage:
  python scripts/import_cyberboss.py --dry-run          # default: preview only
  python scripts/import_cyberboss.py --apply            # actually write to DB

Defaults:
  --src   /Volumes/F101/claude-backup-2026-05-30/dot-claude/projects/D---W-C--Desktop-claude-playground
  --diary /Users/born_blazing_bright/archive/cyberboss-20260615/state/diary
  --db    /Users/born_blazing_bright/.config/marrow/marrow.db
"""

import argparse
import glob
import json
import re
import sqlite3
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_SRC = (
    "/Volumes/F101/claude-backup-2026-05-30/dot-claude/projects"
    "/D---W-C--Desktop-claude-playground"
)
DEFAULT_DIARY = (
    "/Users/born_blazing_bright/archive/cyberboss-20260615/state/diary"
)
DEFAULT_DB = "/Users/born_blazing_bright/.config/marrow/marrow.db"

# ---------------------------------------------------------------------------
# Noise filters for transcript content
# ---------------------------------------------------------------------------
_NOISE_PREFIXES = (
    "WECHAT SESSION INSTRUCTIONS",
    "Caveat:",
)
_NOISE_SUBSTRINGS = (
    "<task-notification>",
    "<system-reminder>",
    "<command-name>",
)
_ROUTE_HEADER_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]\s*")
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$")


def _unwrap_envelope(text: str) -> tuple[str | None, str]:
    """Unwrap cyberboss bridge JSON envelopes in assistant replies.

    {"action":"send_message","message":...} -> message text
    {"action":"silent"} (or other actions)  -> skip
    Unparseable JSON-looking text           -> keep as-is
    Returns (text_or_None, skip_reason).
    """
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


def _extract_text(obj: dict) -> tuple[str | None, str]:
    """
    Return (text_or_None, skip_reason).
    skip_reason is '' when text is returned successfully.
    """
    role = obj.get("type")
    content = obj.get("message", {}).get("content", "")

    if role == "user":
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            # skip if any tool_result block present
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
        # clean route header
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

    # noise filters
    for prefix in _NOISE_PREFIXES:
        if text.startswith(prefix):
            return None, "noise_prefix"
    for sub in _NOISE_SUBSTRINGS:
        if sub in text:
            return None, "noise_substring"

    return text, ""


# ---------------------------------------------------------------------------
# Parse transcripts
# ---------------------------------------------------------------------------
def parse_transcripts(src_dir: str) -> tuple[list[dict], dict]:
    """
    Returns (events_list, skip_counts).
    events_list: dicts ready for DB insertion.
    skip_counts: reason → count.
    """
    files = sorted(glob.glob(src_dir + "/*.jsonl"))
    events: list[dict] = []
    skips: dict[str, int] = {}
    parse_errors = 0

    for fpath in files:
        with open(fpath, encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    parse_errors += 1
                    continue

                # type filter
                role = obj.get("type")
                if role not in ("user", "assistant"):
                    skips["wrong_type"] = skips.get("wrong_type", 0) + 1
                    continue

                # isMeta / isSidechain filter
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
                        "channel": "cyberboss",
                        "compressed": 0,
                        "source_hash": uuid,
                    }
                )

    if parse_errors:
        skips["parse_error"] = parse_errors

    return events, skips


# ---------------------------------------------------------------------------
# Parse diaries
# ---------------------------------------------------------------------------
def parse_diaries(diary_dir: str) -> tuple[list[dict], list[dict]]:
    """
    Returns (diary_rows, diary_events).
    diary_rows: for the diary table.
    diary_events: corresponding events rows (one per diary).
    """
    files = sorted(glob.glob(diary_dir + "/*.md"))
    diary_rows: list[dict] = []
    diary_events: list[dict] = []

    for fpath in files:
        date = Path(fpath).stem
        with open(fpath, encoding="utf-8") as fh:
            overview = fh.read().strip()
        if not overview:
            continue

        diary_rows.append(
            {
                "date": date,
                "content": overview,  # store full text in content
                "overview": overview,
            }
        )
        diary_events.append(
            {
                "session_id": "cyberboss-diary",
                "timestamp": date + "T15:00:00Z",
                "role": "assistant",
                "content": overview,
                "channel": "cyberboss",
                "compressed": 0,
                "source_hash": "diary-" + date,
            }
        )

    return diary_rows, diary_events


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def load_existing_hashes(con: sqlite3.Connection) -> set[str]:
    cur = con.execute(
        "SELECT source_hash FROM events WHERE channel='cyberboss' AND source_hash IS NOT NULL"
    )
    return {row[0] for row in cur.fetchall()}


def load_existing_dates(con: sqlite3.Connection) -> set[str]:
    cur = con.execute("SELECT date FROM diary")
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


def insert_diary_rows(con: sqlite3.Connection, rows: list[dict]) -> None:
    con.executemany(
        """
        INSERT INTO diary (date, content, overview)
        VALUES (:date, :content, :overview)
        """,
        rows,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Import cyberboss data into marrow DB")
    parser.add_argument("--src", default=DEFAULT_SRC, help="Transcript .jsonl directory")
    parser.add_argument("--diary", default=DEFAULT_DIARY, help="Diary .md directory")
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
    print(f"src:   {args.src}")
    print(f"diary: {args.diary}")
    print(f"db:    {args.db}")
    print()

    # Parse
    print("Parsing transcripts...")
    t_events, t_skips = parse_transcripts(args.src)
    print(f"  raw candidate events: {len(t_events)}")

    print("Parsing diaries...")
    d_rows, d_events = parse_diaries(args.diary)
    print(f"  diary files parsed:   {len(d_rows)}")
    print()

    # Connect and deduplicate
    con = sqlite3.connect(args.db)
    try:
        existing_hashes = load_existing_hashes(con)
        existing_dates = load_existing_dates(con)

        # Deduplicate transcript events
        new_t_events = [e for e in t_events if e["source_hash"] not in existing_hashes]
        dup_t = len(t_events) - len(new_t_events)

        # Deduplicate diary events (check against hashes too)
        new_d_events = [e for e in d_events if e["source_hash"] not in existing_hashes]
        dup_d_ev = len(d_events) - len(new_d_events)

        # Deduplicate diary rows
        new_d_rows = [r for r in d_rows if r["date"] not in existing_dates]
        dup_d_rows = len(d_rows) - len(new_d_rows)

        # Stats
        print("=== DRY-RUN STATISTICS ===" if dry_run else "=== APPLY STATISTICS ===")
        print(f"Transcript events to insert:  {len(new_t_events)}")
        print(f"  skipped (duplicate hash):   {dup_t}")
        print(f"  skipped (parse/filter):     {sum(t_skips.values())}")
        for reason, count in sorted(t_skips.items()):
            print(f"    {reason}: {count}")
        print()
        print(f"Diary rows to insert:         {len(new_d_rows)}")
        print(f"  skipped (date conflict):    {dup_d_rows}")
        print(f"Diary events to insert:       {len(new_d_events)}")
        print(f"  skipped (duplicate hash):   {dup_d_ev}")
        print()

        # Sample 3 transcript events
        print("--- 3 sample transcript events (content truncated to 100 chars) ---")
        for ev in new_t_events[:3]:
            snippet = ev["content"][:100].replace("\n", "↵")
            print(f"  [{ev['role']}] {ev['timestamp']} | {snippet}")
        print()

        # Sample 1 diary event
        if new_d_events:
            sample = new_d_events[0]
            snippet = sample["content"][:100].replace("\n", "↵")
            print(f"--- sample diary event ---")
            print(f"  [{sample['role']}] {sample['timestamp']} | {snippet}")
            print()

        if dry_run:
            print("Dry-run complete. Pass --apply to write to DB.")
            return

        # Apply
        print("Writing to DB...")
        with con:  # single transaction; auto-rollback on exception
            if new_t_events:
                insert_events(con, new_t_events)
            if new_d_rows:
                insert_diary_rows(con, new_d_rows)
            if new_d_events:
                insert_events(con, new_d_events)

        total_events = len(new_t_events) + len(new_d_events)
        print(f"Done. Inserted {total_events} events, {len(new_d_rows)} diary rows.")

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        con.close()


if __name__ == "__main__":
    main()
