"""Per-view render functions for sub-pages.

Each renderer returns a complete markdown block (including markers).
Anchor formats:
- Structured views: `<!-- id:{id} -->` at line end (DESIGN L118).
- Narrative views (diary): `#### YYYY-MM-DD` heading is the row boundary; no inline anchor.
  Stacks `## YYYY` / `### MonthName` above for navigation.
"""
from __future__ import annotations

import calendar
import sqlite3
from pathlib import Path


def _year_month(date_str: str) -> tuple[str, str]:
    """`'2026-05-20'` → `('2026', 'May')`. Empty/short strings get sentinels."""
    if not date_str or len(date_str) < 7:
        return ("?", "?")
    return date_str[:4], calendar.month_name[int(date_str[5:7])]


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
        "SELECT date, content, mood FROM diary ORDER BY date ASC"
    ).fetchall()
    # No internal H1 — Obsidian shows the filename as the title; an in-file
    # H1 duplicates it. Same rule applies to every render fn below.
    # Hierarchy: ## YYYY → ### MonthName → #### YYYY-MM-DD [mood] → body.
    out = [_m0(key), ""]
    cur_year = None
    cur_month = None
    for r in rows:
        year, month = _year_month(r["date"])
        if year != cur_year:
            out.append(f"## {year}")
            out.append("")
            cur_year = year
            cur_month = None
        if month != cur_month:
            out.append(f"### {month}")
            out.append("")
            cur_month = month
        mood = f" [{r['mood']}]" if r["mood"] else ""
        out.append(f"#### {r['date']}{mood}")
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
    """Render format (H5 + paragraph, 2026-05-24):

        ##### [YYYY-MM-DD] subject
        description paragraph. <!-- id:N -->

    Two H5-heading flavours:
    - Standard (Us + dated Me): `##### [YYYY-MM-DD] subject`
    - Historical Me (year-only date AND title starts with `Age `):
      `##### [<title>]` — the title itself (e.g. `Age 0-10 | Hometown`)
      is the human-readable bracket; the raw year is not surfaced.
      Round-trip stays safe because reconcile reads the row's id from
      the inline `<!-- id:N -->` anchor and pulls date from DB.

    `title` column = subject. `theme` kept nullable but unused.
    Confirmed rows (pinned=1) are clean — no `Nh ago`, no `✅❌✏️` buttons.
    """
    key = "milestone"
    rows = conn.execute(
        "SELECT id, scope, date, title, description, pinned "
        "FROM milestones WHERE pinned=1 ORDER BY date"
    ).fetchall()
    us = [r for r in rows if r["scope"] == "us"]
    me = [r for r in rows if r["scope"] == "me"]

    def _is_age_row(r) -> bool:
        # Year-only date column (no `-MM-DD`) AND title prefixed with `Age `
        # → historical Me row from timeline.md backfill.
        date = (r["date"] or "").strip()
        title = (r["title"] or "").strip()
        return len(date) == 4 and date.isdigit() and title.startswith("Age ")

    def _section(label: str, entries: list) -> list[str]:
        lines = [f"## {label}", ""]
        if not entries:
            lines.append("_none yet_")
            lines.append("")
            return lines
        for r in entries:
            subject = r["title"] or "(untitled)"
            if _is_age_row(r):
                # Historical Me — title fills the bracket, year stays in DB only.
                lines.append(f"##### [{subject}]")
            else:
                lines.append(f"##### [{r['date']}] {subject}")
            desc = (r["description"] or "").strip()
            # Anchor sits inline at the tail of the description paragraph.
            # When description is empty, anchor goes on its own line — still
            # inside the H5 block so reconcile can parse the boundary.
            if desc:
                lines.append(f"{desc}{_anchor(r['id'])}")
            else:
                lines.append(_anchor(r["id"]).lstrip())
            lines.append("")
        return lines

    out = [_m0(key), ""]
    out += _section("Us", us)
    out += _section("Me", me)
    out.append(_m1(key))
    return "\n".join(out)


# -- Empty stubs: Profile / Stickers / Wallet -------------------------------
# Position-reserved per DESIGN L43-65. Profile content lands when entity
# render is wired; Wallet content lands with Phase 5 (stellan_wallet);
# Stickers gallery lands once auto-describe ingest ships.

def _stub_block(key: str, title: str, note: str) -> str:
    # `title` kept in the signature for callsite clarity but not rendered —
    # the filename already labels the page.
    del title
    return "\n".join([
        _m0(key),
        "",
        f"_{note}_",
        "",
        _m1(key),
    ])


def render_profile(conn: sqlite3.Connection) -> str:
    return _stub_block(
        "profile",
        "Profile",
        "Position reserved — entity-backed render lands with Phase 2 entity fix.",
    )


def render_stickers(conn: sqlite3.Connection) -> str:
    return ""


def render_wallet(conn: sqlite3.Connection) -> str:
    return _stub_block(
        "wallet",
        "Wallet",
        "Position reserved — bank-statement render lands with Phase 5 stellan_wallet.",
    )


# -- Memes (Personal: paw+fact / Public: meme+event+news+others) -----------

_MEME_PERSONAL = ("paw", "fact")
_MEME_PUBLIC = ("meme", "event", "news", "others")


def render_memes(conn: sqlite3.Connection) -> str:
    key = "memes"
    rows = conn.execute(
        "SELECT id, type, key, value "
        "FROM memes ORDER BY "
        "CASE type "
        "  WHEN 'paw' THEN 1 "
        "  WHEN 'fact' THEN 2 "
        "  WHEN 'meme' THEN 3 "
        "  WHEN 'event' THEN 4 "
        "  WHEN 'news' THEN 5 "
        "  ELSE 6 "
        "END, "
        "created_at ASC"
    ).fetchall()
    personal = [r for r in rows if r["type"] in _MEME_PERSONAL]
    public = [r for r in rows if r["type"] not in _MEME_PERSONAL]

    def _line(r) -> str:
        val = f" → {r['value']}" if r["value"] else ""
        return f"- [{r['type']}] **{r['key']}**{val}" + _anchor(r["id"])

    def _section(label: str, entries: list) -> list[str]:
        lines = [f"## {label}", ""]
        if entries:
            lines += [_line(r) for r in entries]
        else:
            lines.append("_none yet_")
        lines.append("")
        return lines

    out = [_m0(key), ""]
    out += _section("Personal", personal)
    out += _section("Public", public)
    out.append(_m1(key))
    return "\n".join(out)


# -- Study ------------------------------------------------------------------

def render_study_index(units: list[dict]) -> str:
    key = "study"
    out = [_m0(key), ""]
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
    out = [_m0(key), ""]
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

    out = [_m0(key), "", "## Active", ""]
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
        "FROM pit ORDER BY status, created_at ASC"
    ).fetchall()
    out = [_m0(key), ""]
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
    out = [_m0(key), ""]
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
    from .paths import paths as _paths
    key = "cheatsheet"
    home = Path.home()
    cc_dir = home / ".claude"
    marrow_dir = home / "CC-Lab" / "marrow"
    config_dir = _paths.marrow_db.parent
    ny_dir = _paths.ny_root

    out = [_m0(key), "",
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
