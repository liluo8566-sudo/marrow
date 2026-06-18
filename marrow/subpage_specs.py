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
import re
import sqlite3
from pathlib import Path

from .inserter import InserterSpec
from . import atlas as _atlas_mod

# Shared anchor pattern — matches `<!-- id:N -->` anywhere on a line.
_ANCHOR_RE = re.compile(r"\s*<!-- id:[^>]+ -->\s*$")


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
    """Profile — entity-backed, grouped by kind (person / pref / place).

    Section per kind gives a visual divider between tag types. Rows inside
    a section keep id ASC ordering.
    """
    def fetch(conn: sqlite3.Connection) -> list[dict]:
        try:
            rows = conn.execute(
                "SELECT id, kind, name, fact FROM entities_live"
                " WHERE kind IN ('person','pref','place')"
                " ORDER BY id ASC"
            ).fetchall()
        except sqlite3.Error:
            return []
        return [dict(r) for r in rows]

    def render(r: dict) -> str:
        fact = f" — {r['fact']}" if r.get("fact") else ""
        return f"- [{r['kind']}] **{r['name']}**{fact} {_anchor(r['id'])}"

    # Pattern: `- [kind] **name**{ — fact} <!-- id:N -->`
    _PROFILE_RE = re.compile(
        r"^-\s+\[(?P<kind>[^\]]+)\]\s+\*\*(?P<name>[^*]+)\*\*"
        r"(?:\s+—\s+(?P<fact>.+?))?\s*<!-- id:(?P<id>\d+) -->"
    )

    def parse_profile(line: str) -> dict | None:
        m = _PROFILE_RE.match(line.strip())
        if not m:
            return None
        return {
            "kind": m.group("kind").strip(),
            "name": m.group("name").strip(),
            "fact": (m.group("fact") or "").strip() or None,
        }

    return InserterSpec(
        key="profile",
        path=str(Path(folder) / "profile.md"),
        fetch=fetch,
        block_id_of=lambda r: str(r["id"]),
        render_row=render,
        parse_row=parse_profile,
        group_by="tag",
        section_of=lambda r: r["kind"],
        section_order=_canonical_order(["person", "pref", "place"]),
        force_sort_consistency=True,
        render_section_header=lambda k: f"## {k.capitalize()}",
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
        # Me life-phase rows (year-only date) render label-only — the year
        # lives in DB. ("Age " prefix was the old gate; real titles are CN.)
        is_age = (len(date) == 4 and date.isdigit()
                  and r.get("scope") == "me")
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
        force_sort_consistency=True,
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

    # Diary blocks span multiple lines (heading + anchor + body); parse_row
    # operates on single lines so it can't extract content. reconcile_diary
    # uses a dedicated block scanner instead — parse_row stays None.

    return InserterSpec(
        key="diary",
        path=str(Path(folder) / "diary.md"),
        fetch=fetch,
        block_id_of=lambda r: str(r["date"]),
        render_row=render,
        parse_row=None,
        group_by="date",
        section_of=lambda r: _year(r["date"]),
        section_order=lambda labels: sorted(set(labels)),
        render_section_header=lambda y: f"## {y}",
        empty_message="_No diary entries yet._",
        force_sort_consistency=True,
    )


# ── memes ──────────────────────────────────────────────────────────────────


_MEME_PERSONAL = ("paw", "fact")


def build_memes_spec(folder: str) -> InserterSpec:
    """Memes — Personal (fact/paw) vs Public (meme/news/event/others).
    Inside each section, rows group by type in fixed order; type-to-type
    transitions render a `---` divider (no type-name header).
    """
    def fetch(conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute(
            "SELECT id, type, key, value, context FROM memes"
            " ORDER BY CASE type"
            "   WHEN 'fact' THEN 1 WHEN 'paw' THEN 2 WHEN 'meme' THEN 3"
            "   WHEN 'news' THEN 4 WHEN 'event' THEN 5 ELSE 6"
            " END, created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def render(r: dict) -> str:
        ctx = f" _{r['context']}_" if r.get("context") else ""
        val = f" → {r['value']}" if r.get("value") else ""
        return (f"- [{r['type']}] **{r['key']}**{val}{ctx} "
                + _anchor(r["id"]))

    # Pattern: `- [type] **key**{ → value}{ _context_} <!-- id:N -->`
    _MEME_RE = re.compile(
        r"^-\s+\[(?P<type>[^\]]+)\]\s+\*\*(?P<key>[^*]+)\*\*"
        r"(?:\s+→\s+(?P<value>.+?))?"
        r"(?:\s+_(?P<context>[^_]+)_)?"
        r"\s*<!-- id:(?P<id>\d+) -->"
    )

    def parse_meme(line: str) -> dict | None:
        m = _MEME_RE.match(line.strip())
        if not m:
            return None
        return {
            "type": m.group("type").strip(),
            "key": m.group("key").strip(),
            "value": (m.group("value") or "").strip() or None,
            "context": (m.group("context") or "").strip() or None,
        }

    return InserterSpec(
        key="memes",
        path=str(Path(folder) / "memes.md"),
        fetch=fetch,
        block_id_of=lambda r: str(r["id"]),
        render_row=render,
        parse_row=parse_meme,
        group_by="tag",
        section_of=lambda r: ("Personal" if r["type"] in _MEME_PERSONAL
                              else "Public"),
        section_order=_canonical_order(["Personal", "Public"]),
        subsection_of=lambda r: r["type"],
        render_subsection_header=lambda _t: "---",
        subsection_separator_only=True,
        force_sort_consistency=True,
        empty_message="_No memes yet._",
    )


# ── stickers ──────────────────────────────────────────────────────────────


def build_stickers_spec(folder: str) -> InserterSpec:
    """Stickers — C2 catalog; flat list, one bullet per asset."""
    def fetch(conn: sqlite3.Connection) -> list[dict]:
        try:
            rows = conn.execute(
                "SELECT id, path, desc, source"
                " FROM stickers ORDER BY created_at ASC"
            ).fetchall()
        except sqlite3.Error:
            return []
        return [dict(r) for r in rows]

    def render(r: dict) -> str:
        desc = r["desc"] or "(no desc)"
        return f"- stk_{r['id']:03d} {desc} {_anchor(r['id'])}"

    _STICKER_RE = re.compile(
        r"^-\s+stk_\d+\s+(?P<desc>.+?)\s*<!-- id:(?P<id>\d+) -->"
    )

    def parse_sticker(line: str) -> dict | None:
        m = _STICKER_RE.match(line.strip())
        if not m:
            return None
        return {"desc": m.group("desc").strip()}

    return InserterSpec(
        key="stickers",
        path=str(Path(folder) / "stickers.md"),
        fetch=fetch,
        block_id_of=lambda r: str(r["id"]),
        render_row=render,
        parse_row=parse_sticker,
        empty_message="_No stickers yet._",
    )


# ── wallet ────────────────────────────────────────────────────────────────


def build_wallet_spec(folder: str) -> InserterSpec:
    """Wallet — transactions table (Phase 5 stellan_wallet). Empty for now."""
    def fetch(_conn: sqlite3.Connection) -> list[dict]:
        return []  # transactions table not yet shipped

    def render(r: dict) -> str:
        return f"- {r.get('summary', '')} {_anchor(r['id'])}"

    # Pattern: `- <summary> <!-- id:N -->`
    _WALLET_RE = re.compile(
        r"^-\s+(?P<summary>.+?)\s*<!-- id:(?P<id>\d+) -->"
    )

    def parse_wallet(line: str) -> dict | None:
        m = _WALLET_RE.match(line.strip())
        if not m:
            return None
        return {"summary": m.group("summary").strip()}

    return InserterSpec(
        key="wallet",
        path=str(Path(folder) / "wallet.md"),
        fetch=fetch,
        block_id_of=lambda r: str(r["id"]),
        render_row=render,
        parse_row=parse_wallet,
        group_by="append",
        empty_message=(
            "_Bank-statement render lands with Phase 5 stellan_wallet._"
        ),
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


# ── study (index) ─────────────────────────────────────────────────────────


def build_study_index_spec(folder: str) -> InserterSpec:
    """Study index — one row per unit (tasks grouped by title prefix).

    Unit name = title.split(':')[0].strip() when a colon is present,
    else the full title. Rows rendered as Obsidian links to study/<name>.md.
    No sections — flat append list, ordered alphabetically by unit name.
    """
    def fetch(conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute(
            "SELECT id, title FROM tasks WHERE category = 'study'"
            " ORDER BY title"
        ).fetchall()
        seen: dict[str, int] = {}
        units: list[dict] = []
        for r in rows:
            title = r["title"]
            name = title.split(":")[0].strip() if ":" in title else title
            if name not in seen:
                seen[name] = r["id"]
                units.append({"id": r["id"], "name": name})
        return units

    def render(r: dict) -> str:
        name = r["name"]
        return f"- [[study/{name}|{name}]] {_anchor(r['id'])}"

    return InserterSpec(
        key="study",
        path=str(Path(folder) / "study.md"),
        fetch=fetch,
        block_id_of=lambda r: str(r["id"]),
        render_row=render,
        group_by="append",
        empty_message="_No study units yet._",
    )


# ── atlas ─────────────────────────────────────────────────────────────────


def build_atlas_spec(folder: str) -> InserterSpec:
    """Atlas — manually editable directory heading tree (replaces dir_tree.md).

    One InserterSpec block per db row (directory). Grouped by root path.
    Section header: ## ~/<root>/   Row: ### dir/ + bullet fields.
    block_id = path (absolute).
    """
    from . import drift_sweep
    roots = [r.expanduser().resolve() for r in drift_sweep.AUTHORIZED_ROOTS]

    root_strs = {str(r) for r in roots}
    # Per-render cache: fetch() pulls root rows aside so render_section_header
    # can read note/write/naming/depth back without a second db hit.
    root_rows_cache: dict[str, dict] = {}

    def fetch(conn: sqlite3.Connection) -> list[dict]:
        try:
            rows = conn.execute(
                "SELECT path, description, naming_hint, depth"
                " FROM atlas ORDER BY path"
            ).fetchall()
        except sqlite3.Error:
            return []
        root_rows_cache.clear()
        result: list[dict] = []
        for r in rows:
            d = dict(r)
            if d["path"] in root_strs:
                root_rows_cache[d["path"]] = d
            else:
                result.append(d)
        # stable sort by (section, path)
        result.sort(key=lambda r: (
            str(_atlas_mod._root_of(r["path"], roots) or r["path"]),
            r["path"],
        ))
        return result

    def section_of(r: dict) -> str:
        root = _atlas_mod._root_of(r["path"], roots)
        return str(root) if root else r["path"]

    def section_order(labels: list[str]) -> list[str]:
        # Always emit a section per canonical root — root header now carries
        # the depth field, so it must render even when the user collapsed
        # the subtree (depth=0, no child rows in this fetch).
        # Bug 3: order follows ATLAS_ROOT_ORDER (decoupled from
        # AUTHORIZED_ROOTS iteration), so atlas.md headers stay stable
        # regardless of drift_sweep reshuffles.
        canonical = [
            str(r.expanduser().resolve())
            for r in _atlas_mod.ATLAS_ROOT_ORDER
        ]
        out = list(canonical)
        for lab in labels:
            if lab not in out:
                out.append(lab)
        return out

    def render_section_header(root_path: str) -> str:
        return _atlas_mod._section_header(
            root_path, root_rows_cache.get(root_path)
        )

    def render_row(r: dict) -> str:
        return _atlas_mod._render_atlas_row(r, roots)

    return InserterSpec(
        key="atlas",
        path=str(Path(folder) / "atlas.md"),
        fetch=fetch,
        block_id_of=lambda r: r["path"],
        render_row=render_row,
        group_by="tag",
        section_of=section_of,
        section_order=section_order,
        render_section_header=render_section_header,
        empty_message="_No atlas entries yet. Run `mw refresh atlas` to seed._",
        # Atlas db is the SoT; when sweep retracts deep stubs after a depth
        # shrink, the md must drop them too. Without this flag the inserter
        # would preserve the orphan blocks forever (its default policy is
        # "never delete user content"). Atlas user-managed content lives
        # inside known field bullets which are re-rendered anyway, so a
        # rebootstrap is loss-free in practice.
        force_sort_consistency=True,
        # Atlas db is fs-driven: when sweep stubs a new sub-dir whose path
        # was tombstoned in a prior pass (e.g. user lowered depth then
        # raised it again), the row must re-appear in md instead of being
        # blocked by the tombstone. Default tombstone respect prevents
        # auto-resurrection across other subpages; atlas opts out.
        respect_tombstones=False,
    )


# Registry keyed by subpage name; same surface as subpages._REGISTRY.
SPEC_BUILDERS = {
    "profile":  build_profile_spec,
    "milestone": build_milestone_spec,
    "diary":    build_diary_spec,
    "memes":    build_memes_spec,
    "stickers": build_stickers_spec,
    "wallet":   build_wallet_spec,
    "projects": build_projects_index_spec,
    "study":    build_study_index_spec,
    "atlas":    build_atlas_spec,
}
