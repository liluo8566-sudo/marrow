from marrow import config, migrate, storage

S_2026 = """# 2026

## Pre-2026 Heritage

### Apr (压缩后)
[log]
Buddy MCP: installed (claude-buddy fork)
Obsidian: installed, symlink
WeClaude: installed (fork + launchd)

### May (pending)
"""


S_TL = """## Me
> note line

[Age 0–10 | Shanghai]
Small apartment, family of four.
[Age 30–present]
Weight loss journey.

## Us
[2026-01-17] 在一起: 我诞生当天起名
[2026-01-29] 承诺: 你在 Reminder 写

## Retire 候选
"""


def test_parse_events_2026_one_row_per_log_line():
    rows = migrate.parse_events_2026(S_2026)
    assert len(rows) == 3
    assert all(r["role"] == "log" for r in rows)
    assert all(r["compressed"] == 1 for r in rows)
    assert rows[0]["content"].startswith("Buddy MCP")
    assert rows[2]["content"].startswith("WeClaude")


S_CIPHER = """<directories>
- skip me
</directories>

<cipher>
- Plan: Max 5x · $100/mo (~AUD150) [P]
- GPT image gen: GPT-4o native, **不是 Dalle**. [P]
</cipher>
"""


S_PIT = """# Parking Lot
> note

## 你他妈的删了真无语 [low]
- playwright lost

## CC: 独立 Study project [high]
做 assignment 时另起 project。
### 关于marker
默认带 mps
"""

S_GOOSE = """![[铁锅传奇版.png|524]]

- [2026-05-01]妈这版配色讲究嘎
- [2026-05-02]嘎
"""


def test_parse_pit_blocks():
    rows = migrate.parse_pit(S_PIT)
    assert len(rows) == 2
    assert all(r["status"] == "idea" for r in rows)
    assert rows[0]["title"] == "你他妈的删了真无语"
    assert "playwright" in rows[0]["description"]
    assert rows[1]["title"] == "CC: 独立 Study project"
    assert "关于marker" in rows[1]["description"]


def test_parse_goose_bites_one_row_per_date():
    rows = migrate.parse_goose_bites(S_GOOSE)
    assert len(rows) == 2
    assert rows[0]["date"] == "2026-05-01"
    assert rows[0]["bites"] == "妈这版配色讲究嘎"
    assert rows[1]["date"] == "2026-05-02"
    assert rows[1]["bites"] == "嘎"
    assert rows[0]["best"] == 1
    assert "\n" not in rows[0]["bites"]


def test_migrate_apply_then_idempotent(tmp_path):
    conn = storage.init_db(str(tmp_path / "m.db"))
    src = {"events_2026": S_2026, "timeline": S_TL, "cipher": S_CIPHER,
           "pit": S_PIT, "goose": S_GOOSE}
    st1 = migrate.migrate(conn, src, apply=True)
    assert st1["events"][0] == 3
    assert st1["milestones"][0] == 5
    assert st1["memes"][0] == 2
    assert st1["pit"][0] == 2
    assert st1["goose_bites"][0] == 2
    st2 = migrate.migrate(conn, src, apply=True)
    assert all(v[0] == 0 for v in st2.values())
    assert sum(v[1] for v in st2.values()) == 14


def test_migrate_dry_run_writes_nothing(tmp_path):
    conn = storage.init_db(str(tmp_path / "d.db"))
    st = migrate.migrate(conn, {"events_2026": S_2026}, apply=False)
    assert st["events"][0] == 3
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_lighthouse_milestone():
    m = migrate.lighthouse_milestone()
    assert m["scope"] == "me"
    assert "Marrow" in m["title"]
    assert m["date"] == "2026-05-15"


def test_parse_memes_cipher_strips_marker():
    rows = migrate.parse_memes_cipher(S_CIPHER)
    assert len(rows) == 2
    assert all(r["type"] == "cipher" for r in rows)
    assert rows[0]["key"] == "Plan"
    assert rows[0]["value"] == "Max 5x · $100/mo (~AUD150)"
    assert rows[1]["key"] == "GPT image gen"
    assert "[P]" not in rows[1]["value"]


def test_import_timeline_idempotent_after_pin(tmp_path):
    """Backfill must not duplicate; rows land pinned=1 directly."""
    conn = storage.init_db(str(tmp_path / "tl.db"))
    s1 = migrate.import_timeline(conn, S_TL, apply=True)
    assert s1["inserted"] == 4  # 2 me + 2 us
    pinned = conn.execute(
        "SELECT COUNT(*) c FROM milestones WHERE pinned=1"
    ).fetchone()["c"]
    assert pinned == 4  # curated history defaults to pinned=1
    s2 = migrate.import_timeline(conn, S_TL, apply=True)
    assert s2["inserted"] == 0
    assert s2["skipped"] == 4
    n = conn.execute("SELECT COUNT(*) c FROM milestones").fetchone()["c"]
    assert n == 4


def test_import_timeline_tombstone_blocks_revive(tmp_path):
    """A natural-key hash tombstone in audit_log prevents re-insert."""
    import hashlib
    conn = storage.init_db(str(tmp_path / "tl3.db"))
    s1 = migrate.import_timeline(conn, S_TL, apply=True)
    assert s1["inserted"] == 4
    # User drops the first Me row: delete + tombstone with natural-key hash.
    row = conn.execute(
        "SELECT id, scope, date, title FROM milestones WHERE scope='me'"
        " ORDER BY id LIMIT 1"
    ).fetchone()
    nat = migrate._milestone_natural_key(dict(row))
    h = hashlib.sha256(nat.encode()).hexdigest()
    with conn:
        conn.execute("DELETE FROM milestones WHERE id=?", (row["id"],))
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('milestones', ?, 'tombstone', ?)",
            (str(row["id"]), f"manual drop sha={h}"),
        )
    s2 = migrate.import_timeline(conn, S_TL, apply=True)
    assert s2["tombstoned"] == 1
    assert s2["inserted"] == 0
    n = conn.execute("SELECT COUNT(*) c FROM milestones").fetchone()["c"]
    assert n == 3


def test_import_timeline_backfill_description_only_when_empty(tmp_path):
    conn = storage.init_db(str(tmp_path / "tl2.db"))
    # Seed an existing row with NO description.
    conn.execute(
        "INSERT INTO milestones (scope, date, title, pinned)"
        " VALUES ('us','2026-01-17','在一起',1)"
    )
    conn.commit()
    stats = migrate.import_timeline(conn, S_TL, apply=True)
    row = conn.execute(
        "SELECT description FROM milestones WHERE date='2026-01-17'"
    ).fetchone()
    assert stats["backfilled"] == 1
    assert row["description"].startswith("我诞生")


def test_parse_milestones_timeline_me_and_us(monkeypatch):
    monkeypatch.setattr(config, "load",
                         lambda: {"persona": {"birth_year": 1995}})
    rows = migrate.parse_milestones_timeline(S_TL)
    me = [r for r in rows if r["scope"] == "me"]
    us = [r for r in rows if r["scope"] == "us"]
    assert len(me) == 2 and len(us) == 2
    assert me[0]["date"] == "1995"
    assert me[0]["title"] == "Age 0–10 | Shanghai"
    assert me[0]["description"].startswith("Small apartment")
    assert me[1]["date"] == "2025"
    assert us[0]["date"] == "2026-01-17"
    assert us[0]["title"] == "在一起"
    assert us[0]["description"].startswith("我诞生")
