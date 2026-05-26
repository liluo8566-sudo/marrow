"""InserterSpec builders per subpage (Plan M Phase B).

One builder per subpage key; each returns a fully wired InserterSpec
covering: row fetch, block_id derivation, per-row render, group_by
section logic. The inserter consumes these and writes md in
hand-edit-preserving mode.

Profile / Stickers / Wallet sit empty until their content tables fill
out (Phase 2 entity, Phase 5 wallet). The spec still ships so the md
file exists and gets bootstrapped as a placeholder.

Cheatsheet has no inserter spec — disk is its SoT, kept on the legacy
read-only render path.
"""
from __future__ import annotations

import calendar
import sqlite3
from pathlib import Path

from .inserter import InserterSpec


def _year(date_str: str) -> str:
    return date_str[:4] if date_str and len(date_str) >= 4 else "?"


def _month_name(date_str: str) -> str:
    """`'2026-05-20'` → `'May'`. Empty/short strings get sentinel."""
    if not date_str or len(date_str) < 7:
        return ""
    try:
        return calendar.month_name[int(date_str[5:7])]
    except (ValueError, IndexError):
        return ""


def _anchor(row_id: int | str) -> str:
    return f"<!-- id:{row_id} -->"


def _canonical_order(canonical: list[str]):
    """Return a section_order that puts canonical labels first, then any extras."""
    def _ordered(labels):
        seen = set(labels)
        out = [lab for lab in canonical if lab in seen]
        for lab in labels:
            if lab not in out:
                out.append(lab)
        return out
    return _ordered


# ── profile ────────────────────────────────────────────────────────────────


def build_profile_spec(folder: str) -> InserterSpec:
    """Profile — entity-backed, currently empty until Phase 2 entity render.

    group_by = append. Each entity row → one bullet block.
    """
    def fetch(conn: sqlite3.Connection) -> list[dict]:
        try:
            rows = conn.execute(
                "SELECT id, kind, name, fact FROM entities_live"
                " WHERE kind IN ('person','pref','place')"
                " ORDER BY mention_count DESC, id ASC"
            ).fetchall()
        except sqlite3.Error:
            return []
        return [dict(r) for r in rows]

    def render(r: dict) -> str:
        fact = f" — {r['fact']}" if r.get("fact") else ""
        return f"- [{r['kind']}] **{r['name']}**{fact} {_anchor(r['id'])}"

    return InserterSpec(
        key="profile",
        path=str(Path(folder) / "profile.md"),
        fetch=fetch,
        block_id_of=lambda r: str(r["id"]),
        render_row=render,
        group_by="append",
        empty_message=(
            "_Profile entries land here once Phase 2 entity render is wired._"
        ),
    )


# ── milestone ──────────────────────────────────────────────────────────────


def build_milestone_spec(folder: str) -> InserterSpec:
    """Milestone — scope = us/me, two sections, H5 + paragraph format.

    Reconcile path remains in reconcile.reconcile_milestones — md edits flow
    back to DB independently of the inserter. The inserter only emits new
    rows (pinned=1) the user hasn't seen yet.
    """
    def fetch(conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute(
            "SELECT id, scope, date, title, description, pinned"
            " FROM milestones WHERE pinned=1 ORDER BY scope, date"
        ).fetchall()
        return [dict(r) for r in rows]

    def render(r: dict) -> str:
        title = r["title"] or "(untitled)"
        date = (r["date"] or "").strip()
        is_age = (len(date) == 4 and date.isdigit()
                  and title.startswith("Age "))
        head = f"##### [{title}]" if is_age else f"##### [{date}] {title}"
        desc = (r["description"] or "").strip()
        anchor = _anchor(r["id"])
        if desc:
            return f"{head}\n{desc} {anchor}"
        return f"{head}\n{anchor}"

    return InserterSpec(
        key="milestone",
        path=str(Path(folder) / "milestone.md"),
        fetch=fetch,
        block_id_of=lambda r: str(r["id"]),
        render_row=render,
        group_by="tag",
        section_of=lambda r: "Us" if r["scope"] == "us" else "Me",
        section_order=_canonical_order(["Us", "Me"]),
        empty_message="_No milestones yet._",
    )


# ── diary ──────────────────────────────────────────────────────────────────


def build_diary_spec(folder: str) -> InserterSpec:
    """Diary — one block per date, month + year section headings.

    block_id = date string. Year sections ordered ascending so the file
    reads oldest → newest (matches legacy render). Month subsection lives
    inside each year as a level-3 heading prefixing the H4 day blocks.

    The "section" granularity for inserter purposes is the year — month
    + day boundaries are emitted inside each block, but a new year does
    not exist as a header until the first entry of that year arrives.
    """
    def fetch(conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute(
            "SELECT date, content, mood FROM diary ORDER BY date ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def render(r: dict) -> str:
        mood = f" [{r['mood']}]" if r.get("mood") else ""
        body = (r["content"] or "").strip()
        anchor = _anchor(r["date"])
        # Single multi-line block with the H4 date heading. Mood lives in
        # the heading tag for legacy parity. Anchor sits on the next line
        # so parse_blocks can scope cleanly to this day.
        return f"#### {r['date']}{mood}\n{anchor}\n\n{body}"

    return InserterSpec(
        key="diary",
        path=str(Path(folder) / "diary.md"),
        fetch=fetch,
        block_id_of=lambda r: str(r["date"]),
        render_row=render,
        group_by="date",
        section_of=lambda r: _year(r["date"]),
        section_order=lambda labels: sorted(set(labels)),
        render_section_header=lambda y: f"## {y}",
        empty_message="_No diary entries yet._",
    )


# ── memes ──────────────────────────────────────────────────────────────────


_MEME_PERSONAL = ("paw", "fact")


def build_memes_spec(folder: str) -> InserterSpec:
    """Memes — Personal (paw/fact) vs Public (meme/event/news/others)."""
    def fetch(conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute(
            "SELECT id, type, key, value, context FROM memes"
            " ORDER BY CASE type"
            "   WHEN 'paw' THEN 1 WHEN 'fact' THEN 2"
            "   WHEN 'meme' THEN 3 WHEN 'event' THEN 4"
            "   WHEN 'news' THEN 5 ELSE 6"
            " END, created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def render(r: dict) -> str:
        ctx = f" _{r['context']}_" if r.get("context") else ""
        val = f" → {r['value']}" if r.get("value") else ""
        return (f"- [{r['type']}] **{r['key']}**{val}{ctx} "
                + _anchor(r["id"]))

    return InserterSpec(
        key="memes",
        path=str(Path(folder) / "memes.md"),
        fetch=fetch,
        block_id_of=lambda r: str(r["id"]),
        render_row=render,
        group_by="tag",
        section_of=lambda r: ("Personal" if r["type"] in _MEME_PERSONAL
                              else "Public"),
        section_order=_canonical_order(["Personal", "Public"]),
        empty_message="_No memes yet._",
    )


# ── stickers ──────────────────────────────────────────────────────────────


def build_stickers_spec(folder: str) -> InserterSpec:
    """Stickers — gallery; one bullet per asset, grouped by linked meme key
    when present. Empty until auto-describe ingest ships.
    """
    def fetch(conn: sqlite3.Connection) -> list[dict]:
        try:
            rows = conn.execute(
                "SELECT s.id, s.key, s.asset_path, s.mime_type, m.key as meme_key"
                " FROM stickers s LEFT JOIN memes m ON s.meme_id = m.id"
                " ORDER BY s.created_at ASC"
            ).fetchall()
        except sqlite3.Error:
            return []
        return [dict(r) for r in rows]

    def render(r: dict) -> str:
        mime = f" ({r['mime_type']})" if r.get("mime_type") else ""
        return f"- **{r['key']}** `{r['asset_path']}`{mime} {_anchor(r['id'])}"

    return InserterSpec(
        key="stickers",
        path=str(Path(folder) / "stickers.md"),
        fetch=fetch,
        block_id_of=lambda r: str(r["id"]),
        render_row=render,
        group_by="tag",
        section_of=lambda r: r.get("meme_key") or "Other",
        section_order=lambda labels: sorted(set(labels)),
        empty_message=(
            "_Sticker gallery lands once auto-describe ingest ships._"
        ),
    )


# ── wallet ────────────────────────────────────────────────────────────────


def build_wallet_spec(folder: str) -> InserterSpec:
    """Wallet — transactions table (Phase 5 stellan_wallet). Empty for now."""
    def fetch(_conn: sqlite3.Connection) -> list[dict]:
        return []  # transactions table not yet shipped

    def render(r: dict) -> str:
        return f"- {r.get('summary', '')} {_anchor(r['id'])}"

    return InserterSpec(
        key="wallet",
        path=str(Path(folder) / "wallet.md"),
        fetch=fetch,
        block_id_of=lambda r: str(r["id"]),
        render_row=render,
        group_by="append",
        empty_message=(
            "_Bank-statement render lands with Phase 5 stellan_wallet._"
        ),
    )


# ── goose-bites ────────────────────────────────────────────────────────────


def build_goose_spec(folder: str) -> InserterSpec:
    """Goose-bites — one block per date (best quote of the day)."""
    def fetch(conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute(
            "SELECT id, date, bites FROM goose_bites ORDER BY date ASC"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            bites = (d.get("bites") or "").strip()
            if "\n" in bites:
                bites = next((ln for ln in bites.splitlines() if ln.strip()),
                             bites)
            d["bites"] = bites
            out.append(d)
        return out

    def render(r: dict) -> str:
        return f"- [{r['date']}]{r['bites']} {_anchor(r['id'])}"

    return InserterSpec(
        key="goose",
        path=str(Path(folder) / "goose-bites.md"),
        fetch=fetch,
        block_id_of=lambda r: str(r["id"]),
        render_row=render,
        group_by="date",
        section_of=lambda r: _year(r["date"]),
        section_order=lambda labels: sorted(set(labels)),
        render_section_header=lambda y: f"## {y}",
        subsection_of=lambda r: _month_name(r["date"]),
        render_subsection_header=lambda m: f"### {m}",
        empty_message="_No goose-bites yet._",
    )


# ── projects (index placeholder) ───────────────────────────────────────────


def build_projects_index_spec(folder: str) -> InserterSpec:
    """Projects index — flat row per [Project] task, status sectioned.

    Phase E (wt-md-e) takes this further (file-per-project + frontmatter).
    For Phase B we only wire the index in inserter mode so hand-edits to
    the project list survive auto-writes.
    """
    def fetch(conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute(
            "SELECT id, title, status, next_step, due FROM tasks"
            " WHERE category = 'project'"
            " ORDER BY status, (due IS NULL), due, created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def render(r: dict) -> str:
        nxt = f" — {r['next_step']}" if r.get("next_step") else ""
        due = f" [Due {r['due']}]" if r.get("due") else ""
        title = r["title"]
        if r["status"] == "active":
            link = f"[[projects/{title}|{title}]]"
        else:
            link = f"{title} ({r['status']})"
        return f"- {link}{nxt}{due} {_anchor(r['id'])}"

    return InserterSpec(
        key="projects",
        path=str(Path(folder) / "projects.md"),
        fetch=fetch,
        block_id_of=lambda r: str(r["id"]),
        render_row=render,
        group_by="tag",
        section_of=lambda r: "Active" if r["status"] == "active" else "Done",
        section_order=_canonical_order(["Active", "Done"]),
        empty_message="_No projects yet._",
    )


# Registry keyed by subpage name; same surface as subpages._REGISTRY.
SPEC_BUILDERS = {
    "profile":  build_profile_spec,
    "milestone": build_milestone_spec,
    "diary":    build_diary_spec,
    "memes":    build_memes_spec,
    "stickers": build_stickers_spec,
    "wallet":   build_wallet_spec,
    "goose":    build_goose_spec,
    "projects": build_projects_index_spec,
}
