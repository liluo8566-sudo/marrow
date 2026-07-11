"""Smart session title — LLM-summarized, ≤8 cn-chars / 8 en-words.

Wired by ``hooks._maybe_set_session_title``: each user_prompt_submit
fires this module as a detached subprocess (``python -m marrow.title``)
when the session is eligible, so the LLM call never blocks the hook.

Length rule: 8 CJK chars OR 8 ASCII tokens — matches the user's '≤8字'
across languages. The prompt asks the model to follow the dominant
language of the user messages, so cn chats get cn titles and en chats
get en titles. cli + wx use the same path; cc's internal summary is
opaque to us, so marrow owns this end-to-end.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config, repo, storage

# Length cap on the final title. CJK char = 1 unit; ASCII token = 1 unit.
TITLE_UNITS_MAX = 8
# Skip summarisation until the user has actually engaged enough to title well.
MIN_PROMPT_COUNT = 5
# Cap on how many turns we feed the LLM — enough signal without dragging
# multi-thousand-token transcripts into a cheap-tier call.
MAX_TURNS_FOR_CONTEXT = 8


def _is_cjk(ch: str) -> bool:
    """True for CJK ideographs + CJK / fullwidth punctuation."""
    if not ch:
        return False
    cp = ord(ch)
    return 0x3000 <= cp <= 0x9FFF or 0xFF00 <= cp <= 0xFFEF


def truncate_units(text: str, max_units: int = TITLE_UNITS_MAX) -> str:
    """Cap ``text`` at ``max_units``: each CJK char = 1 unit, each ASCII
    alnum/-/_/' token = 1 unit. Other characters pass through without
    consuming a unit (so a hyphen between two CJK chars does not cost a
    unit, and trailing punctuation does not exhaust the budget)."""
    if not text:
        return ""
    out: list[str] = []
    n = 0
    i = 0
    L = len(text)
    while i < L and n < max_units:
        c = text[i]
        if _is_cjk(c):
            out.append(c)
            n += 1
            i += 1
        elif c.isascii() and (c.isalnum() or c in "-_'"):
            j = i
            while j < L and text[j].isascii() and (text[j].isalnum() or text[j] in "-_'"):
                j += 1
            out.append(text[i:j])
            n += 1
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out).strip(" .,:;。，、！？!?\"'`")


def _extract_text(content) -> str:
    """Pull the plaintext head from a cc jsonl message.content shape."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
        return " ".join(parts).strip()
    return ""


def gather_turns(jsonl_path: str | Path, max_turns: int = MAX_TURNS_FOR_CONTEXT) -> list[tuple[str, str]]:
    """Read user/assistant message heads from a cc jsonl. Stops after
    ``max_turns`` entries. Returns ``[(role, text), ...]``."""
    if not jsonl_path:
        return []
    p = Path(jsonl_path)
    if not p.exists():
        return []
    turns: list[tuple[str, str]] = []
    try:
        with p.open(encoding="utf-8") as f:
            for ln in f:
                try:
                    d = json.loads(ln)
                except Exception:  # noqa: BLE001 — skip malformed lines
                    continue
                t = d.get("type")
                if t not in ("user", "assistant"):
                    continue
                msg = d.get("message") or {}
                text = _extract_text(msg.get("content"))
                if not text:
                    continue
                cap = 500 if t == "user" else 400
                turns.append((t, text[:cap]))
                if len(turns) >= max_turns:
                    break
    except OSError:
        return []
    return turns


def _count_user_prompts(jsonl_path: str | Path) -> int:
    if not jsonl_path:
        return 0
    p = Path(jsonl_path)
    if not p.exists():
        return 0
    n = 0
    try:
        with p.open(encoding="utf-8") as f:
            for ln in f:
                try:
                    d = json.loads(ln)
                except Exception:  # noqa: BLE001 — skip malformed lines
                    continue
                if d.get("type") != "user":
                    continue
                msg = d.get("message") or {}
                if _extract_text(msg.get("content")):
                    n += 1
    except OSError:
        return 0
    return n


_PROMPT_TPL = (
    "Read the conversation and produce a short session title.\n\n"
    "Rules:\n"
    "- Maximum 8 Chinese characters OR 8 English words.\n"
    "- Match the dominant language of the USER messages "
    "(cn user → cn title; en user → en title).\n"
    "- Capture the main topic, not the greeting or meta-talk.\n"
    "- Output the title only. No quotes, no punctuation, no prefix.\n\n"
    "Conversation:\n{turns}\n\nTitle:"
)


def build_prompt(turns: list[tuple[str, str]]) -> str:
    body = "\n".join(f"[{role}] {text}" for role, text in turns)
    return _PROMPT_TPL.format(turns=body)


def _prior_user_count(summary: str | None) -> int:
    if not summary or "|uc=" not in summary:
        return 999
    try:
        return int(summary.rsplit("|uc=", 1)[1])
    except ValueError:
        return 999


def _should_skip_summarize(conn, sid: str, current_user_count: int) -> bool:
    """Upgradeable dedup: thin early titles get one later improvement chance."""
    row = conn.execute(
        "SELECT summary FROM audit_log "
        "WHERE action='title_summarize' AND target_table='sessions' AND target_id=? "
        "ORDER BY id DESC LIMIT 1",
        (sid,),
    ).fetchone()
    if row is None:
        return False
    prior_uc = _prior_user_count(row["summary"])
    if prior_uc >= 10:
        return True
    return current_user_count < prior_uc * 3


def _record_ok(conn, sid: str, title: str, user_count: int) -> None:
    conn.execute(
        "INSERT INTO audit_log (target_table, target_id, action, summary) "
        "VALUES ('sessions', ?, 'title_summarize', ?)",
        (sid, f"{title}|uc={user_count}"),
    )
    conn.commit()


def summarize(sid: str, jsonl_path: str | None = None) -> str | None:
    """Run the title summariser for one session. Returns the title written,
    or ``None`` when we skipped (already done, too few turns, missing jsonl)
    or when the LLM call failed (no audit row written → next hook re-tries)."""
    if not sid:
        return None
    conn = storage.connect(config.db_path())
    try:
        if jsonl_path is None:
            # Lazy import — avoids circular hooks ↔ title at module load.
            from . import hooks
            jsonl_path = hooks._locate_jsonl(sid)
        if not jsonl_path:
            return None
        turns = gather_turns(jsonl_path)
        user_count = _count_user_prompts(jsonl_path)
        if _should_skip_summarize(conn, sid, user_count):
            return None
        if user_count < MIN_PROMPT_COUNT:
            return None
        try:
            from .llm import LLMClient
            client = LLMClient(config.load())
            raw = client.call("title_summarize", build_prompt(turns), tier="cheap")
        except Exception:  # noqa: BLE001 — LLM failure leaves the row retry-eligible
            return None
        title = truncate_units(raw.strip())
        if not title:
            return None
        cur = repo.get_session(sid)
        channel = (cur or {}).get("channel") or "cli"
        model = (cur or {}).get("model")
        repo.upsert_session(sid, model, channel, title=title)
        _record_ok(conn, sid, title, user_count)
        return title
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(description="LLM-summarise sessions.title for one sid.")
    p.add_argument("--sid", required=True)
    p.add_argument("--jsonl", default=None)
    args = p.parse_args()
    res = summarize(args.sid, args.jsonl)
    if res:
        print(res)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
