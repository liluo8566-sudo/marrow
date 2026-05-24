"""Per-view render functions for sub-pages.

Each renderer returns a complete markdown block (including markers).
Anchor formats:
- Structured views: `<!-- id:{id} -->` at line end (DESIGN L118).
- Narrative views (diary, goose-bites): `## YYYY-MM-DD` heading is the row
  boundary (DESIGN L119); no inline anchor needed.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


_MARKER_START = "<!-- marrow:{key}:start -->"
_MARKER_END = "<!-- marrow:{key}:end -->"


def _m0(key: str) -> str:
    return _MARKER_START.format(key=key)


def _m1(key: str) -> str:
    return _MARKER_END.format(key=key)


def _anchor(row_id: int) -> str:
    return f" <!-- id:{row_id} -->"


# -- Diary (narrative, month-grouped) ---------------------------------------

def render_diary(conn: sqlite3.Connection) -> str:
    key = "diary"
    rows = conn.execute(
        "SELECT date, content, mood FROM diary ORDER BY date DESC"
    ).fetchall()
    out = [_m0(key), "# Diary", ""]
    cur_month = None
    for r in rows:
        month = r["date"][:7]
        if month != cur_month:
            if cur_month is not None:
                out.append("")
            out.append(f"## {month}")
            out.append("")
            cur_month = month
        mood = f" [{r['mood']}]" if r["mood"] else ""
        out.append(f"## {r['date']}{mood}")
        out.append("")
        out.append(r["content"].strip() if r["content"] else "")
        out.append("")
    if not rows:
        out.append("_No diary entries yet._")
        out.append("")
    out.append(_m1(key))
    return "\n".join(out)


# -- Milestone (structured, ## Us / ## Me) ----------------------------------

def render_milestone(conn: sqlite3.Connection) -> str:
    key = "milestone"
    rows = conn.execute(
        "SELECT id, scope, date, title, description, theme, pinned "
        "FROM milestones ORDER BY date"
    ).fetchall()
    us = [r for r in rows if r["scope"] == "us"]
    me = [r for r in rows if r["scope"] == "me"]

    def _section(label: str, entries: list) -> list[str]:
        lines = [f"## {label}", ""]
        if not entries:
            lines.append("_none yet_")
            lines.append("")
            return lines
        for r in entries:
            pin = " (pinned)" if r["pinned"] else ""
            theme = f" [{r['theme']}]" if r["theme"] else ""
            desc = f" — {r['description']}" if r["description"] else ""
            lines.append(
                f"- {r['date']} **{r['title']}**{theme}{pin}{desc}"
                + _anchor(r["id"])
            )
        lines.append("")
        return lines

    out = [_m0(key), "# Milestones", ""]
    out += _section("Us", us)
    out += _section("Me", me)
    out.append(_m1(key))
    return "\n".join(out)


# -- Memes (memes table + sticker thumbnails) ------------------------------

def render_memes(conn: sqlite3.Connection) -> str:
    key = "memes"
    memes_rows = conn.execute(
        "SELECT id, type, key, value, context, use_count "
        "FROM memes ORDER BY use_count DESC, created_at DESC"
    ).fetchall()
    sticker_rows = conn.execute(
        "SELECT s.id, s.key, s.asset_path, s.mime_type, m.key AS meme_key "
        "FROM stickers s LEFT JOIN memes m ON m.id = s.meme_id "
        "ORDER BY s.use_count DESC, s.created_at DESC"
    ).fetchall()

    out = [_m0(key), "# Memes", ""]
    out.append("## Phrases")
    out.append("")
    if memes_rows:
        for r in memes_rows:
            ctx = f" _{r['context']}_" if r["context"] else ""
            val = f" → {r['value']}" if r["value"] else ""
            out.append(
                f"- [{r['type']}] **{r['key']}**{val}{ctx}" + _anchor(r["id"])
            )
    else:
        out.append("_No memes yet._")
    out.append("")
    out.append("## Stickers")
    out.append("")
    if sticker_rows:
        for r in sticker_rows:
            mk = f" ({r['meme_key']})" if r["meme_key"] else ""
            out.append(f"- ![[{r['asset_path']}]]{mk}" + _anchor(r["id"]))
    else:
        out.append("_No stickers yet._")
    out.append("")
    out.append(_m1(key))
    return "\n".join(out)


# -- Goose-bites (narrative, best-of-the-day) ------------------------------

def render_goose(conn: sqlite3.Connection) -> str:
    key = "goose"
    rows = conn.execute(
        "SELECT id, date, bites, best FROM goose_bites ORDER BY date DESC"
    ).fetchall()
    out = [_m0(key), "# (Tieguo) Best of the Day", ""]
    cur_month = None
    for r in rows:
        month = r["date"][:7]
        if month != cur_month:
            if cur_month is not None:
                out.append("")
            out.append(f"## {month}")
            out.append("")
            cur_month = month
        best = " [best]" if r["best"] else ""
        out.append(f"## {r['date']}{best}")
        out.append("")
        out.append(r["bites"].strip() if r["bites"] else "")
        out.append("")
    if not rows:
        out.append("_No goose-bites yet._")
        out.append("")
    out.append(_m1(key))
    return "\n".join(out)


# -- Study ------------------------------------------------------------------

def render_study_index(units: list[dict]) -> str:
    key = "study"
    out = [_m0(key), "# Study", ""]
    if not units:
        out.append("_No study units yet._")
        out.append("")
    else:
        for u in units:
            out.append(f"- [[study/{u['name']}|{u['name']}]]")
        out.append("")
    out.append(_m1(key))
    return "\n".join(out)


def render_study_unit(name: str, tasks: list[dict]) -> str:
    key = f"study-{name}"
    out = [_m0(key), f"# Study — {name}", ""]
    if not tasks:
        out.append("_No active tasks for this unit._")
        out.append("")
    else:
        for t in tasks:
            due = f" [Due {t['due']}]" if t.get("due") else ""
            nxt = f" — {t['next_step']}" if t.get("next_step") else ""
            status = f" ({t['status']})" if t["status"] != "active" else ""
            out.append(
                f"- **{t['title']}**{status}{nxt}{due}" + _anchor(t["id"])
            )
    out.append("")
    out.append(_m1(key))
    return "\n".join(out)


# -- Projects ---------------------------------------------------------------

def render_projects_index(conn: sqlite3.Connection) -> str:
    key = "projects"
    rows = conn.execute(
        "SELECT id, title, status, next_step, due "
        "FROM tasks WHERE category = 'project' "
        "ORDER BY status, (due IS NULL), due, created_at"
    ).fetchall()
    active = [r for r in rows if r["status"] == "active"]
    done = [r for r in rows if r["status"] != "active"]

    out = [_m0(key), "# Projects", "", "## Active", ""]
    if active:
        for r in active:
            nxt = f" — {r['next_step']}" if r["next_step"] else ""
            due = f" [Due {r['due']}]" if r["due"] else ""
            out.append(
                f"- [[projects/{r['title']}|{r['title']}]]"
                f"{nxt}{due}" + _anchor(r["id"])
            )
    else:
        out.append("_None active._")
    out += ["", "## Done", ""]
    if done:
        for r in done:
            out.append(f"- {r['title']} ({r['status']})" + _anchor(r["id"]))
    else:
        out.append("_None done._")
    out += ["", "[[projects/pit|Pit (backlog)]]", "", _m1(key)]
    return "\n".join(out)


def render_pit(conn: sqlite3.Connection) -> str:
    key = "pit"
    rows = conn.execute(
        "SELECT id, title, description, status, related_files "
        "FROM pit ORDER BY status, created_at DESC"
    ).fetchall()
    out = [_m0(key), "# Pit — Deferred Backlog", ""]
    if rows:
        for r in rows:
            desc = f" — {r['description']}" if r["description"] else ""
            files = f" [{r['related_files']}]" if r["related_files"] else ""
            out.append(
                f"- [{r['status']}] **{r['title']}**{desc}{files}"
                + _anchor(r["id"])
            )
    else:
        out.append("_Pit is empty._")
    out += ["", _m1(key)]
    return "\n".join(out)


def render_project_page(thread: dict) -> str:
    name = thread["title"]
    key = f"project-{name}"
    out = [_m0(key), f"# {name}", ""]
    nxt = thread.get("next_step") or "_no next step recorded_"
    due = f"\n**Due:** {thread['due']}" if thread.get("due") else ""
    summ = thread.get("last_session_summary") or "_no session summary yet_"
    ctx = thread.get("context_pointers") or ""
    outcome = thread.get("outcome_log") or ""
    out.append(f"**Status:** {thread['status']}{due}")
    out.append(f"**Next step:** {nxt}" + _anchor(thread["id"]))
    out += ["", "## Session summary", summ, ""]
    if ctx:
        out += ["## Context pointers", ctx, ""]
    if outcome:
        out += ["## Outcome log", outcome, ""]
    out.append(_m1(key))
    return "\n".join(out)


# -- Cheatsheet (disk-rendered, read-only) ----------------------------------

def render_cheatsheet(conn: sqlite3.Connection) -> str:
    """Disk-rendered. Reads scripts, hooks, skills, aliases + dir map."""
    key = "cheatsheet"
    home = Path.home()
    cc_dir = home / ".claude"
    marrow_dir = home / "cc-lab" / "marrow"
    config_dir = home / ".config" / "marrow"
    ny_dir = home / "Desktop" / "NY"

    out = [_m0(key), "# Cheatsheet", "",
           "> Read-only — disk is source of truth. Hand-edits are overwritten.", ""]

    out += ["## Skills", ""]
    skills_dir = cc_dir / "skills"
    if skills_dir.exists():
        for f in sorted(skills_dir.glob("*.md")):
            out.append(f"- `{f.stem}`")
    else:
        out.append("_skills dir not found_")
    out.append("")

    out += ["## Hooks", ""]
    hooks_dir = cc_dir / "hooks"
    if hooks_dir.exists():
        for f in sorted(hooks_dir.iterdir()):
            if f.suffix in (".py", ".sh") and f.is_file():
                out.append(f"- `{f.name}`")
    else:
        out.append("_hooks dir not found_")
    out.append("")

    out += ["## Aliases", ""]
    alias_lines = []
    for rc in (home / ".zshrc", home / ".zsh_aliases", home / ".bashrc"):
        if not rc.exists():
            continue
        for ln in rc.read_text(errors="replace").splitlines():
            s = ln.strip()
            if (s.startswith("alias ") and "mw " in s) or s.startswith("alias mw"):
                alias_lines.append(f"- `{s}`")
    out += alias_lines if alias_lines else ["_No mw aliases found._"]
    out.append("")

    out += ["## Directory map", "",
            f"- Marrow code: `{marrow_dir}`",
            f"- Marrow data: `{config_dir}`",
            f"- NY vault: `{ny_dir}`",
            f"- CC config: `{cc_dir}`",
            "", _m1(key)]
    return "\n".join(out)
