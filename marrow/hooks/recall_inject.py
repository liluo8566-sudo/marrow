"""UserPromptSubmit: recall fusion injection + hit rendering."""
from __future__ import annotations

import json
import os
import re as _re
import sys
from datetime import datetime, timezone
from .. import config, cortex_bridge, outbox, repo, storage
from ..timeutil import (
    utc_iso_to_local_datetime,
    format_recall_ts,
    reltime_short,
)
from ._shared import _read_input
from .lifecycle import (
    _is_worktree_session,
    _maybe_set_session_model,
    _maybe_set_session_title,
)
from .state import (
    _TABLE_KINDS,
    _WX_TIME_PREFIX_RE,
    _load_recall_seen,
    _load_sticker_nudge,
    _recall_session_log_path,
    _save_recall_seen,
    _save_sticker_nudge,
    _strip_wx_time_prefix,
)

# ── pure recall-render helpers (extracted for testability) ───────────────────

def _apply_rel_cutoff(hits: list[dict], rel_cutoff: float) -> list[dict]:
    """Drop hits whose score < top_score * rel_cutoff. Returns filtered list."""
    if not hits:
        return []
    top_score = hits[0].get("score", 0.0)
    cutoff = top_score * rel_cutoff
    return [h for h in hits if (h.get("score") or 0.0) >= cutoff]


# Per-kind id prefix for recall heads (event -> ev, memes -> me, etc.).
_KIND_ABBREV = {
    "event": "ev", "memes": "me", "milestone": "ms",
    "entity": "en", "diary": "d", "task": "t",
}


def _meme_date(ts: str) -> str:
    """Meme creation date as 'MM-DD' (configured local timezone), or 'YYYY' if >1y old.
    Empty on missing/unparseable timestamp."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        local = dt.astimezone(config.get_tz())
        if (now - dt).total_seconds() >= 365 * 86400:
            return local.strftime("%Y")
        return local.strftime("%m-%d")
    except Exception:
        return ""


def _milestone_date(ts: str) -> str:
    """Milestone date with the T00:00 junk stripped. Keeps calendar precision:
    'YYYY' / 'YYYY-MM' / 'YYYY-MM-DD' (whatever the stored date carried).
    The date is a calendar value, not an instant — no tz conversion."""
    if not ts:
        return ""
    return ts.split("T", 1)[0]


def _recall_head(h: dict) -> str:
    """Shared recall-row head: '<time-label> <abbrev>#<id>' (content appended
    by the caller as ': <content>'). Kept identical across the injection
    renderer and the recall log so both read the same.
      event     -> [<channel> <reltime>] ev#<id>   (channel fallback: cli)
      memes     -> [<MM-DD|YYYY>] me#<id>
      milestone -> [<YYYY[-MM[-DD]]>] ms#<id>       (never T00:00)
      entity    -> en#<id>                          (no time)
      diary     -> [<format_recall_ts>] d#<id>      (existing time handling)
      task      -> [<format_recall_ts>] t#<id>
    """
    kind = h.get("kind") or "event"
    ref = f"{_KIND_ABBREV.get(kind, kind)}#{h.get('id', '?')}"
    if kind == "event":
        ch = (h.get("channel") or "cli").strip() or "cli"
        rt = reltime_short(h.get("timestamp") or "")
        return f"[{ch} {rt}] {ref}" if rt else f"[{ch}] {ref}"
    if kind == "memes":
        d = _meme_date(h.get("timestamp") or "")
        return f"[{d}] {ref}" if d else ref
    if kind == "milestone":
        d = _milestone_date(h.get("timestamp") or "")
        return f"[{d}] {ref}" if d else ref
    if kind == "entity":
        return ref
    # diary / task — keep existing format_recall_ts handling.
    ts = format_recall_ts(h.get("timestamp") or "")
    return f"{ts} {ref}" if ts else ref


def _render_hit_block(rank: int, h: dict, rank_caps: list[int]) -> list[str]:
    """Return the markdown lines for one recall hit at the given rank.

    rank_caps[rank] (falling back to rank_caps[-1]) controls max content chars.
    Context turns (h['_context']) are only rendered for rank-0 event hits.
    Pure function — no I/O, no DB access.
    """
    cap = rank_caps[rank] if rank < len(rank_caps) else rank_caps[-1]
    block: list[str] = []
    head = _recall_head(h)
    kind = h.get("kind") or "event"
    content_full = (h.get("content") or "").replace("\n", " ")
    if kind in _TABLE_KINDS:
        block.append(f"- {head}: {content_full[:cap]}")
    else:
        ctxs = h.get("_context") or [] if rank == 0 else []
        main_cap = max(40, cap - 60) if ctxs else cap
        main = content_full[:main_cap]
        block.append(f"- {head}: {main}")
        remaining = max(0, cap - len(main))
        if ctxs and remaining > 0:
            per_ctx = max(0, remaining // len(ctxs))
            for c in ctxs:
                if per_ctx <= 0:
                    break
                cts = utc_iso_to_local_datetime(c.get("timestamp") or "")
                csnip = _strip_wx_time_prefix(
                    (c.get("content") or "").replace("\n", " ")
                )[:per_ctx]
                if not csnip:
                    continue
                arrow = "↑" if c.get("rel") == "prev" else "↓"
                block.append(f"    {arrow} [{cts}] ({c.get('role')}) {csnip}")
    return block


def user_prompt_submit() -> int:
    """Inject top-K recall hits as UserPromptSubmit additionalContext.

    Also handles mm controls before recall.
    Config flag: [recall] vector = true (default on). Set false to disable.
    Fusion weights come from [recall] in config; recall.recall_fusion blends
    vec + bm25 + recency + affect. Fail-soft: any error falls through to a
    no-op so the user prompt always reaches the model.
    """
    inp = _read_input()

    # Worktree / subagent gate: cc instances in a NON-primary git worktree
    # OR dispatched via Task tool (transcript_path under /tasks/) are
    # task-isolated runs. They take direction from the user prompt + main
    # session only; no personal recall context.
    cwd = inp.get("cwd") if isinstance(inp, dict) else None
    tpath = inp.get("transcript_path") if isinstance(inp, dict) else None
    is_subagent = bool(tpath and "/tasks/" in tpath)
    if _is_worktree_session(cwd or "") or is_subagent:
        return 0

    # Cortex wake-turn injections (cortex window only). The cortex daemon types
    # the wake bell / machine marker straight into the window as a user turn;
    # each shape is handled here and stops before recall, while ordinary chat
    # turns fall through untouched. Text + paths are config-routed.
    if cortex_bridge.is_cortex_session(tpath):
        _prompt = (inp.get("prompt") or "").strip() if isinstance(inp, dict) else ""
        # Free-round tuck-in ([NEW ROUND]): only the short marker line is typed
        # into the window; its diff-mode note (and any ct notes claimed for that
        # round) were STAGED by cortex and are injected here COVERTLY, same as
        # the wake bell — so the note never shows on screen and never doubles
        # (consume-once read, 07-14 incident stays closed). A tuck-in is a
        # machine line but never a wake BELL, so this branch is checked before
        # the wake-marker branch, and it never triggers the user-wake reset.
        _tuck = cortex_bridge.tuck_in_marker()
        if _tuck and cortex_bridge.line_starts_with_marker(_prompt, _tuck):
            _body = cortex_bridge.free_round_note_text()
            if _body:
                json.dump({"hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": _body,
                }}, sys.stdout)
            return 0
        # FUSE / CTL machine-marker turns arriving down the ear channel: cortex
        # wrote ONLY the marker (+ CTL args) to wake_signal.log; the full
        # instruction body is injected here COVERTLY so she never SEES it on
        # screen. Line-start shape check tolerates the ear envelope wrapper; a real
        # user prompt merely quoting the marker mid-sentence never matches.
        if cortex_bridge.line_starts_with_marker(_prompt, cortex_bridge._FUSE_MARKER):
            _body = cortex_bridge.fuse_prompt_text()
            if _body:
                json.dump({"hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": _body,
                }}, sys.stdout)
            return 0
        if cortex_bridge.line_starts_with_marker(_prompt, cortex_bridge._CTL_MARKER):
            _body = cortex_bridge.ctl_sleep_text(_prompt)
            if _body:
                json.dump({"hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": _body,
                }}, sys.stdout)
            return 0
        # Wake turn → inject the full wakeup note. The VISIBLE bell is human text
        # only; its machine data lives in the wake_state receipt (match_wake_bell):
        #   receipt = exact on-screen match -> consume the receipt + epoch-check
        #             (stale token = a newer epoch superseded this alarm -> suppress).
        #   shape   = receipt gone/expired but the line starts with the template
        #             prefix -> fail OPEN (process the wake), audit the degraded path.
        # Exact-match (receipt) / line-start (shape), never substring: a real
        # user prompt merely quoting the bell text falls through to the
        # user-wake reset + recall, never swallowed here.
        _bell = cortex_bridge.match_wake_bell(_prompt)
        if _bell is not None:
            _kind, _tok, _degraded = _bell
            if _kind == "receipt":
                cortex_bridge._consume_wake_receipt()
            if _degraded:
                cortex_bridge._wake_audit(
                    "wake_bell_shape", "", "receipt missing -> shape fallback (fail-open)")
            # Staleness check only when a real epoch token is present (receipt).
            # The shape fallback has no token -> fails OPEN.
            if _tok is not None and not cortex_bridge.wake_token_current(_tok):
                cortex_bridge._wake_audit(
                    "wake_line_stale", f"gen={_tok[0]}",
                    "suppressed (superseded epoch)")
                return 0
            _note = cortex_bridge.wakeup_note_text(tpath)
            # Merge any ct-targeted outbox notes into the wake payload — the
            # normal delivery path below never runs on a wake turn, so ct notes
            # must be consumed here (same atomic claim/consume ordering).
            try:
                _ob = outbox.deliver(
                    inp.get("session_id") if isinstance(inp, dict) else None,
                    "ct", is_cortex=True, db=config.db_path())
            except Exception:
                _ob = None
            _payload = "\n\n".join(p for p in (_note, _ob) if p)
            if _payload:
                json.dump({"hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": _payload,
                }}, sys.stdout)
            return 0
        # Real user message (NOT a machine line down the ear channel) → user-wake
        # reset: flip awake, kill the pending alarm + sentinel, spawn a watchdog.
        # Best-effort; never blocks the prompt or the recall below.
        if not cortex_bridge.is_machine_line(_prompt):
            try:
                cortex_bridge._cortex_user_wake_reset(inp if isinstance(inp, dict) else {})
            except Exception:
                pass

    # cwd exclude gate — opt-out per-dir via config.toml [recall].exclude_cwds.
    _ex_cwds = config.load().get("recall", {}).get("exclude_cwds", []) or []
    if cwd and any(cwd.startswith(p) for p in _ex_cwds):
        return 0

    prompt_text = (inp.get("prompt") or "").strip() if isinstance(inp, dict) else ""
    sid = inp.get("session_id") if isinstance(inp, dict) else None

    # Pipeline-prompt gate: a hand-run digest/eval claude (spawned without
    # llm.py's --setting-sources isolation) still loads this hook. Its prompt
    # opens with a transcript fence — never inject, log, or backfill
    # title/model for it.
    if prompt_text.startswith("===== BEGIN ORIGINAL TRANSCRIPT"):
        return 0

    # Outbox delivery (cli/session/ct notes): claim + render notes targeting this
    # session (exact sid, 'cli' broadcast for cli sessions, 'ct' for the cortex
    # session), consume-once. The wake branch above delivers ct notes on a wake
    # turn (and returns before here); a normal cortex turn never hits that branch,
    # so ct notes must be claimed here too — same atomic claim resolves the race
    # so a row taken by either path is never re-delivered. Seeds _nudge_line so it
    # lands on every emit path (renders above recall / other nudges).
    _is_ct_claimant = cortex_bridge.is_cortex_session(tpath)
    _nudge_line: str | None = None
    try:
        _msg_note = outbox.deliver(
            sid, os.environ.get("MARROW_CHANNEL") or "cli",
            is_cortex=_is_ct_claimant,
            db=config.db_path())
        if _msg_note:
            _nudge_line = _msg_note
    except Exception:
        pass

    # Sticker nudge: increment turn counter; flag nudge if 10 turns since last sticker.
    if sid and os.environ.get("MARROW_BRIDGE") == "1":
        try:
            _sn = _load_sticker_nudge(sid)
            _sn["turn_count"] = _sn.get("turn_count", 0) + 1
            if _sn["turn_count"] - _sn.get("last_sticker_turn", 0) >= 10:
                user_name = config.persona()["user_name"]
                _sticker_line = f"你怎么还不发表情包，{user_name}都等急了——翻翻 sticker(action=search) 找个应景的发一下。"
                _nudge_line = f"{_nudge_line}\n{_sticker_line}" if _nudge_line else _sticker_line
                _sn["last_sticker_turn"] = _sn["turn_count"]
            _save_sticker_nudge(sid, _sn)
        except Exception:
            pass

    # tl_add nudge: fire the 10-turn (config) reminder for sids that have gone
    # too long without recording a timeline line. Appends to any sticker nudge.
    if sid:
        try:
            from .. import tl_nudge as _tln
            if _tln.enabled():
                conn = storage.connect(config.db_path())
                try:
                    _tl_hint = _tln.maybe_nudge(conn, sid)
                finally:
                    conn.close()
                if _tl_hint:
                    _nudge_line = f"{_nudge_line}\n{_tl_hint}" if _nudge_line else _tl_hint
        except Exception:
            pass

    # Sticky title + model backfill for wx /resume picker — run regardless
    # of recall config so short-lived cli sessions still get a model written.
    _maybe_set_session_model(sid)
    _maybe_set_session_title(sid, prompt_text)
    try:
        repo.touch_session_active(sid, db=config.db_path())
    except Exception:  # noqa: BLE001 — best-effort timestamp bump
        pass

    cfg = config.load()
    if not cfg.get("recall", {}).get("vector", False):
        return 0

    if not prompt_text:
        return 0

    # Strip synapse-wx bridge boilerplate before recall so media Read
    # instructions / merge notes / dot sentinels never become query needles.
    # Emptiness is judged with the [time: ...] anchor ALSO removed (recall.py
    # strips it internally anyway) so a pure-media bubble skips recall.
    from ..transcript import strip_wx_boilerplate as _strip_wx, strip_harness_markers as _strip_harness
    recall_query = _strip_harness(_strip_wx(prompt_text))
    if not recall_query or not _WX_TIME_PREFIX_RE.sub("", recall_query).strip():
        return 0

    rcfg = cfg.get("recall", {})
    ctx_n = int(rcfg.get("event_context_window", 1))
    budget_chars = int(rcfg.get("budget_chars", 800))
    timelane_budget = int(rcfg.get("timelane_budget", 400))
    _default_rank_caps = [300, 120, 120, 40, 40]
    rank_caps: list[int] = rcfg.get("rank_caps", _default_rank_caps) or _default_rank_caps
    rel_cutoff: float = float(rcfg.get("rel_cutoff", 0.6))

    # ── time-lane: detect cue, run windowed recall first ─────────────────────
    windowed_hits: list[dict] = []
    cue = None
    try:
        from ..timecue import parse_time_cue
        cue = parse_time_cue(recall_query)
    except Exception:
        cue = None

    seen = _load_recall_seen(sid)

    if cue is not None:
        try:
            from .. import recall as recall_mod
            conn = storage.connect(config.db_path())
            try:
                _stripped = cue.stripped.strip()
                # Check if stripped text has substantive content
                _has_content = bool(
                    len([c for c in _stripped if "一" <= c <= "鿿"]) >= 2
                    or any(len(w) >= 3 for w in _re.sub(r"[^\w\s]", " ", _stripped).split()
                           if w.isascii())
                )
                if _has_content:
                    windowed_hits = recall_mod.recall_with_config(
                        conn, _stripped, current_cwd=cwd,
                        since=cue.since_utc, until=cue.until_utc,
                    )
                else:
                    # No substantive keyword — return digest rows for the window
                    windowed_hits = recall_mod.fetch_window_digests(
                        conn, cue.since_utc, cue.until_utc,
                    )
            finally:
                conn.close()
        except Exception:
            windowed_hits = []

    # Dedup windowed hits against already-seen
    wlane: list[dict] = []
    for h in windowed_hits:
        hid = int(h.get("id") or 0)
        kind = h.get("kind") or "event"
        if hid and (kind, hid) in seen:
            continue
        wlane.append(h)
    # windowed hits skip rel_cutoff — they are time-pinned, not semantic ranked

    # ── semantic recall with boilerplate-stripped query ───────────────────────
    try:
        from .. import recall as recall_mod
        conn = storage.connect(config.db_path())
        try:
            hits = recall_mod.recall_with_config(conn, recall_query, current_cwd=cwd)
        finally:
            conn.close()
    except Exception:
        hits = []

    if not hits and not wlane:
        if _nudge_line:
            json.dump(
                {"hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": _nudge_line,
                }},
                sys.stdout,
            )
        return 0

    # ── relative score cutoff (semantic pool only) ────────────────────────────
    hits = _apply_rel_cutoff(hits, rel_cutoff)

    # ── per-session dedup for semantic hits ───────────────────────────────────
    # Build windowed seen set first so semantic dedup excludes them too
    wlane_seen: set[tuple[str, int]] = set()
    for h in wlane:
        hid = int(h.get("id") or 0)
        kind = h.get("kind") or "event"
        if hid:
            wlane_seen.add((kind, hid))

    candidates: list[dict] = []
    for h in hits:
        hid = int(h.get("id") or 0)
        kind = h.get("kind") or "event"
        if hid and (kind, hid) in seen:
            continue
        if hid and (kind, hid) in wlane_seen:
            continue  # already in windowed lane
        candidates.append(h)

    if not candidates and not wlane:
        if _nudge_line:
            json.dump(
                {"hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": _nudge_line,
                }},
                sys.stdout,
            )
        return 0

    # ── fetch context only for rank-1 semantic hit (event, not anchor) ──────
    if ctx_n > 0 and candidates:
        top = candidates[0]
        if top.get("kind") in (None, "event") and top.get("session_id") and top.get("id"):
            try:
                from .. import recall as recall_mod
                conn = storage.connect(config.db_path())
                try:
                    top["_context"] = recall_mod.fetch_event_context(
                        conn, top["session_id"], int(top["id"]), n=ctx_n
                    )
                finally:
                    conn.close()
            except Exception:
                pass

    header_lines = [
        "## Recall (auto) — passive context, do not answer",
        "> If the user references past time/scene cues or memory signals and no relevant hit above → MUST call mcp__marrow__recall.",
        "",
    ]
    lines = list(header_lines)
    # +1 per line for the join newline; matches "\n".join(...) length exactly.
    used = sum(len(line) + 1 for line in header_lines)
    visible: list[dict] = []
    wlane_budget = min(timelane_budget, budget_chars // 2)
    wlane_used = 0

    # ── render windowed hits first (top slots) ────────────────────────────────
    for rank, h in enumerate(wlane):
        kind = h.get("kind") or "event"
        if kind == "digest":
            # Digest rows: prefix with date label
            date = h.get("date") or ""
            try:
                from datetime import datetime as _dt
                _d = _dt.fromisoformat(date)
                label = _d.strftime("%m-%d %a")
            except Exception:
                label = date
            content = (h.get("content") or "")[:rank_caps[0] if rank_caps else 300]
            block = [f"- [{label} · digest] {content}"]
        else:
            block = _render_hit_block(rank, h, rank_caps)
        block_len = sum(len(line) + 1 for line in block)
        if wlane_used + block_len > wlane_budget:
            break
        lines.extend(block)
        used += block_len
        wlane_used += block_len
        visible.append(h)
        hid = int(h.get("id") or 0)
        if hid:
            seen.add((kind, hid))

    # ── render semantic hits filling remaining budget ─────────────────────────
    for rank, h in enumerate(candidates):
        block = _render_hit_block(rank, h, rank_caps)
        kind = h.get("kind") or "event"
        block_len = sum(len(line) + 1 for line in block)
        if visible and used + block_len > budget_chars:
            break  # drop this hit — skip seen-write so it can surface later
        lines.extend(block)
        used += block_len
        visible.append(h)
        hid = int(h.get("id") or 0)
        if hid:
            seen.add((kind, hid))

    if not visible:
        if _nudge_line:
            json.dump(
                {"hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": _nudge_line,
                }},
                sys.stdout,
            )
        return 0
    _save_recall_seen(sid, seen)
    # Best-effort: bump recall_count for injected event-kind hits only.
    _injected_event_ids = [
        int(h.get("id") or 0)
        for h in visible
        if (h.get("kind") or "event") == "event" and h.get("id")
    ]
    if _injected_event_ids:
        try:
            from .. import recall as recall_mod
            recall_mod.bump_recall_counts(_injected_event_ids)
        except Exception:
            pass
    ctx = "\n".join(lines)
    if _nudge_line:
        ctx = ctx + "\n\n" + _nudge_line

    # Side log — markdown append so VSCode preview / tail both readable.
    # Mirror what actually got injected: dedup-filtered `visible`, not raw hits.
    try:
        _append_recall_log(sid, recall_query, visible)
    except Exception:
        pass

    json.dump(
        {"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": ctx,
        }},
        sys.stdout,
    )
    return 0


def _append_recall_log(sid: str, prompt_text: str, hits: list[dict]) -> None:
    """Append one markdown block per turn to recall/recall-<day>-<sid8>.md.

    Per-session file; first write of the session also emits a top-of-file
    header `# Session <sid8> · started <ts>` so opening the file shows a
    clear new-session boundary. Day-prefix in filename makes prune trivial.

    Each block: timestamp header + prompt (truncated) + bullet list of hits
    with kind, id, score, content snippet.
    """
    now_utc = datetime.now(timezone.utc)
    log_path = _recall_session_log_path(sid, now_utc)
    is_new = not log_path.exists()
    now_local = now_utc.astimezone()
    ts = now_local.strftime("%Y-%m-%d %H:%M:%S")
    prompt_oneline = prompt_text.replace("\n", " ")[:200]
    parts: list[str] = []
    if is_new:
        sid8 = (sid or "unknown")[:8]
        parts.append(f"# Session {sid8} · started {ts}")
        parts.append("")
        parts.append(f"### {ts} · prompt: {prompt_oneline}")
    else:
        # Leading blank line keeps blocks visually separated in markdown.
        parts.append(f"\n### {ts} · prompt: {prompt_oneline}")
    parts.append("")
    for h in hits:
        kind = h.get("kind") or "event"
        score = h.get("score", 0.0)
        content = _strip_wx_time_prefix((h.get("content") or "").replace("\n", " "))
        # Mirror injection-side shaping: anchor tables ship full content
        # (rows are short + dense); only event hits get the 120-char cap.
        snip = content if kind in _TABLE_KINDS else content[:120]
        # Same head as the injection renderer; score kept as a debug suffix.
        parts.append(f"- {_recall_head(h)}: {snip} · score={score:.2f}")
        for c in h.get("_context", []) or []:
            arrow = "↑prev" if c.get("rel") == "prev" else "↓next"
            cs = _strip_wx_time_prefix((c.get("content") or "").replace("\n", " "))[:80]
            parts.append(f"    - {arrow} ({c.get('role')}) {cs}")
    with log_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(parts) + "\n")
