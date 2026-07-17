"""Wake-pipeline v2 injections in hooks (cortex window only):
- SessionStart arm line (fresh window)
- UserPromptSubmit wake-turn full-note inject
- UserPromptSubmit monitor-death rearm inject
"""
from __future__ import annotations

import io
import json

import pytest

from marrow import config, cortex_bridge, hooks, storage


def _stdin(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def _ctx(capsys):
    out = capsys.readouterr().out
    if not out.strip():
        return ""
    return json.loads(out)["hookSpecificOutput"].get("additionalContext", "")


def _enable(monkeypatch, tmp_path, extra=None):
    real = config.load

    def _patched():
        cfg = dict(real())
        cx = dict(cfg.get("cortex", {}))
        cx["enabled"] = True
        cx["home"] = str(tmp_path)
        if extra:
            cx.update(extra)
        cfg["cortex"] = cx
        return cfg

    monkeypatch.setattr(config, "load", _patched)


# ── Item 2: wake-turn full-note inject ────────────────────────────────────────

def test_wake_turn_injects_full_note(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    (tmp_path / "wakeup_note.md").write_text("read me and act", encoding="utf-8")
    _enable(monkeypatch, tmp_path, {"wake_marker": "[CORTEX-WAKE]"})
    _stdin(monkeypatch, {"session_id": "s1",
                         "prompt": "[CORTEX-WAKE] 2026-07-11 14:00 wake"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == "read me and act"


def _seed_epoch(tmp_path, gen, state_id):
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "wake_state.json").write_text(
        json.dumps({"gen": gen, "state_id": state_id}), encoding="utf-8")


def test_wake_turn_current_token_injects(tmp_path, monkeypatch, capsys):
    """A wake line carrying a token that matches the live epoch injects the note."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    (tmp_path / "wakeup_note.md").write_text("read me and act", encoding="utf-8")
    _enable(monkeypatch, tmp_path, {"wake_marker": "[CORTEX-WAKE]"})
    _seed_epoch(tmp_path, 7, "abcd1234")
    _stdin(monkeypatch, {"session_id": "s1",
                         "prompt": "[CORTEX-WAKE] 14:00 {g7:abcd1234}"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == "read me and act"


def test_wake_turn_stale_token_suppressed(tmp_path, monkeypatch, capsys):
    """A wake line whose token was superseded (newer gen) is NOT processed as a
    wake: no note injected."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    (tmp_path / "wakeup_note.md").write_text("read me and act", encoding="utf-8")
    _enable(monkeypatch, tmp_path, {"wake_marker": "[CORTEX-WAKE]"})
    _seed_epoch(tmp_path, 8, "abcd1234")  # live gen moved past the line's gen 7
    _stdin(monkeypatch, {"session_id": "s1",
                         "prompt": "[CORTEX-WAKE] 14:00 {g7:abcd1234}"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == ""  # suppressed


def test_wake_turn_legacy_tokenless_line_still_injects(tmp_path, monkeypatch, capsys):
    """A token-less (legacy) wake line is processed as before even when an epoch
    is recorded."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    (tmp_path / "wakeup_note.md").write_text("read me and act", encoding="utf-8")
    _enable(monkeypatch, tmp_path, {"wake_marker": "[CORTEX-WAKE]"})
    _seed_epoch(tmp_path, 8, "abcd1234")
    _stdin(monkeypatch, {"session_id": "s1", "prompt": "[CORTEX-WAKE] 14:00"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == "read me and act"


def test_wake_turn_missing_note_silent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _enable(monkeypatch, tmp_path, {"wake_marker": "[CORTEX-WAKE]"})
    _stdin(monkeypatch, {"session_id": "s1", "prompt": "[CORTEX-WAKE] wake"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == ""


def test_ordinary_chat_no_note_inject(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    (tmp_path / "wakeup_note.md").write_text("secret note", encoding="utf-8")
    _enable(monkeypatch, tmp_path, {"wake_marker": "[CORTEX-WAKE]"})
    _stdin(monkeypatch, {"session_id": "s1", "prompt": "今天过得怎么样"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert "secret note" not in _ctx(capsys)


# ── GAP 2: WAKE branch is line-start shaped, not substring ─────────────────────

def test_wake_marker_mid_sentence_not_swallowed(tmp_path, monkeypatch, capsys):
    """A REAL user prompt quoting the wake marker mid-sentence ("grep for
    [CORTEX-WAKE]") must NOT be swallowed by the wake branch: no note injected,
    and the user-wake reset fires (it is user speech). Previously the substring
    guard (`marker in prompt`) swallowed it, skipping reset + recall."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    (tmp_path / "wakeup_note.md").write_text("secret note", encoding="utf-8")
    _enable(monkeypatch, tmp_path, {"wake_marker": "[CORTEX-WAKE]"})
    called = {"reset": False}
    monkeypatch.setattr(cortex_bridge, "_cortex_user_wake_reset",
                        lambda inp: called.__setitem__("reset", True))
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": "/t/s.jsonl",
                         "prompt": "grep for [CORTEX-WAKE] in the log"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert "secret note" not in _ctx(capsys)  # wake note NOT injected
    assert called["reset"] is True            # treated as a real user message


def test_wake_bell_line_start_fires_wake_branch(tmp_path, monkeypatch, capsys):
    """A real wake bell (marker at line start, no epoch token) fires the wake
    branch — full note injected, no user-wake reset."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    (tmp_path / "wakeup_note.md").write_text("read me and act", encoding="utf-8")
    _enable(monkeypatch, tmp_path, {"wake_marker": "[CORTEX-WAKE]"})
    called = {"reset": False}
    monkeypatch.setattr(cortex_bridge, "_cortex_user_wake_reset",
                        lambda inp: called.__setitem__("reset", True))
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": "/t/s.jsonl",
                         "prompt": "[CORTEX-WAKE] 2026-07-14 14:00 wake"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == "read me and act"  # wake branch fired
    assert called["reset"] is False           # NOT a user message


def test_wake_bell_with_epoch_token_fires_wake_branch(tmp_path, monkeypatch, capsys):
    """The wake line may carry a ' {g<gen>:<sid>}' epoch suffix — the line-start
    shape check tolerates it and the wake branch still fires (current token)."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    (tmp_path / "wakeup_note.md").write_text("read me and act", encoding="utf-8")
    _enable(monkeypatch, tmp_path, {"wake_marker": "[CORTEX-WAKE]"})
    _seed_epoch(tmp_path, 7, "abcd1234")
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": "/t/s.jsonl",
                         "prompt": "[CORTEX-WAKE] 14:00 wake {g7:abcd1234}"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == "read me and act"


def test_wake_bell_wrapped_envelope_fires_wake_branch(tmp_path, monkeypatch, capsys):
    """Delivered by the ear Monitor the bell arrives wrapped:
    `<event>⏳ [CORTEX-WAKE] … {g7:abcd1234}</event>` — the envelope-aware
    line-start check still fires the wake branch."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    (tmp_path / "wakeup_note.md").write_text("read me and act", encoding="utf-8")
    _enable(monkeypatch, tmp_path, {"wake_marker": "[CORTEX-WAKE]"})
    _seed_epoch(tmp_path, 7, "abcd1234")
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": "/t/s.jsonl",
                         "prompt": "<event>⏳ [CORTEX-WAKE] 14:00 {g7:abcd1234}</event>"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == "read me and act"


# ── Phase 2.5 item 1: free-round tuck-in carries its note INLINE — the hook must
#    NOT also turn-inject the full note (07-14 double-note incident). ───────────

def _read_gen(tmp_path):
    d = json.loads((tmp_path / "state" / "wake_state.json").read_text())
    return d.get("gen")


def test_tuck_in_line_injects_menu_not_note(tmp_path, monkeypatch, capsys):
    """A [NEW ROUND] free-round line carries its diff-mode note inline (visible in
    the ear Monitor event); the hook must NOT re-inject the note (no duplicate),
    but DOES inject the C2 menu body covertly via additionalContext (never on
    screen)."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    (tmp_path / "wakeup_note.md").write_text("FROZEN note", encoding="utf-8")
    _enable(monkeypatch, tmp_path,
            {"wake_marker": "[CORTEX-WAKE]", "tuck_in_marker": "[NEW ROUND]"})
    _stdin(monkeypatch, {"session_id": "s1",
                         "prompt": "📮 note inline\n\nNow: 14:00\n⏳ [NEW ROUND] 15 min"})
    assert hooks.main(["user_prompt_submit"]) == 0
    ctx = _ctx(capsys)
    assert "FROZEN note" not in ctx     # note NOT re-injected (no double note)
    assert "3 choices" in ctx           # C2 menu injected covertly
    assert "lie_down" in ctx


def test_tuck_in_menu_blank_injects_nothing(tmp_path, monkeypatch, capsys):
    """[cortex].tuck_in_menu_text = "" -> marker-only round, hook injects nothing."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _enable(monkeypatch, tmp_path,
            {"tuck_in_marker": "[NEW ROUND]", "tuck_in_menu_text": ""})
    _stdin(monkeypatch, {"session_id": "s1",
                         "prompt": "note\n⏳ [NEW ROUND] 15 min"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == ""


def test_tuck_in_line_does_not_bump_gen(tmp_path, monkeypatch, capsys):
    """A tuck-in machine line must NOT count as a user message: no user-wake
    reset, so the cancellation epoch (gen) is untouched (ghost-bump guard)."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _enable(monkeypatch, tmp_path, {"tuck_in_marker": "[NEW ROUND]"})
    _seed_epoch(tmp_path, 42, "beef1234")
    _stdin(monkeypatch, {"session_id": "s1",
                         "transcript_path": "/t/s.jsonl",
                         "prompt": "📮 note inline\nNow: 14:00\n⏳ [NEW ROUND] 15 min"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _read_gen(tmp_path) == 42  # no user-wake reset -> gen unchanged


def test_marker_mention_mid_sentence_not_swallowed(tmp_path, monkeypatch, capsys):
    """P2-2 regression: a REAL user prompt quoting the tuck-in marker mid-sentence
    must NOT hit the de-dup early return — it is user speech, so the user-wake
    reset fires (gen bumps) and later hook processing is reached. Previously the
    substring guard (`marker in prompt`) swallowed it."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    called = {"reset": False}
    monkeypatch.setattr(cortex_bridge, "_cortex_user_wake_reset",
                        lambda inp: called.__setitem__("reset", True))
    _enable(monkeypatch, tmp_path, {"tuck_in_marker": "[NEW ROUND]"})
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": "/t/s.jsonl",
                         "prompt": "did the [NEW ROUND] path fire?"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert called["reset"] is True  # treated as a real user message

    # both-direction: a genuine machine block still hits the tuck-in branch (no
    # user-wake reset). The note is not re-injected; only the covert C2 menu is.
    called["reset"] = False
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": "/t/s.jsonl",
                         "prompt": "note above\n⏳ [NEW ROUND] 15 min since ..."})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert called["reset"] is False
    assert "note above" not in _ctx(capsys)  # note not re-injected


def test_wakeup_note_fresh_render_wins(tmp_path, monkeypatch):
    """render_module configured + subprocess succeeds => fresh stdout is used,
    not the frozen file."""
    (tmp_path / "wakeup_note.md").write_text("frozen", encoding="utf-8")
    _enable(monkeypatch, tmp_path, {"render_module": "cortex.note_render",
                                    "venv_python": "/x/py", "repo_root": "/x"})

    class _P:
        returncode = 0
        stdout = "FRESH note SID feed1234"
        stderr = ""
    monkeypatch.setattr(cortex_bridge.subprocess, "run", lambda *a, **k: _P())
    assert cortex_bridge.wakeup_note_text("/t/feed1234ab.jsonl") == "FRESH note SID feed1234"


def test_wakeup_note_fresh_render_mirrors_to_file(tmp_path, monkeypatch):
    """A successful fresh render overwrites wakeup_note.md so the on-disk copy
    equals the note actually injected."""
    note = tmp_path / "wakeup_note.md"
    note.write_text("stale frozen", encoding="utf-8")
    _enable(monkeypatch, tmp_path, {"render_module": "cortex.note_render",
                                    "venv_python": "/x/py", "repo_root": "/x"})

    class _P:
        returncode = 0
        stdout = "FRESH mirrored note"
        stderr = ""
    monkeypatch.setattr(cortex_bridge.subprocess, "run", lambda *a, **k: _P())
    cortex_bridge.wakeup_note_text("/t/x.jsonl")
    assert note.read_text(encoding="utf-8") == "FRESH mirrored note"


def test_wakeup_note_mirror_failure_never_breaks_injection(tmp_path, monkeypatch):
    """A mirror write failure (atomic_write raising) is swallowed inside
    _mirror_wakeup_note, so the injected text still returns."""
    _enable(monkeypatch, tmp_path, {"render_module": "cortex.note_render",
                                    "venv_python": "/x/py", "repo_root": "/x"})

    class _P:
        returncode = 0
        stdout = "FRESH note"
        stderr = ""
    monkeypatch.setattr(cortex_bridge.subprocess, "run", lambda *a, **k: _P())
    import marrow._atomic as _atomic
    monkeypatch.setattr(_atomic, "atomic_write",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    assert cortex_bridge.wakeup_note_text("/t/x.jsonl") == "FRESH note"


def test_wakeup_note_falls_back_on_render_failure(tmp_path, monkeypatch):
    """Subprocess failure / non-zero / empty => frozen file is returned."""
    (tmp_path / "wakeup_note.md").write_text("frozen fallback", encoding="utf-8")
    _enable(monkeypatch, tmp_path, {"render_module": "cortex.note_render",
                                    "venv_python": "/x/py", "repo_root": "/x"})

    def _boom(*a, **k):
        raise OSError("no such venv")
    monkeypatch.setattr(cortex_bridge.subprocess, "run", _boom)
    assert cortex_bridge.wakeup_note_text("/t/x.jsonl") == "frozen fallback"


def test_wakeup_note_no_render_module_uses_file(tmp_path, monkeypatch):
    """render_module unset => never spawns, static file only (feature disabled)."""
    (tmp_path / "wakeup_note.md").write_text("static only", encoding="utf-8")
    _enable(monkeypatch, tmp_path, {"venv_python": "/x/py", "repo_root": "/x"})

    def _fail(*a, **k):
        raise AssertionError("subprocess must not run when render_module unset")
    monkeypatch.setattr(cortex_bridge.subprocess, "run", _fail)
    assert cortex_bridge.wakeup_note_text("/t/x.jsonl") == "static only"


def test_non_cortex_session_no_wake_inject(tmp_path, monkeypatch, capsys):
    """No MARROW_CORTEX => the whole cortex branch is skipped."""
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    (tmp_path / "wakeup_note.md").write_text("note", encoding="utf-8")
    _enable(monkeypatch, tmp_path, {"wake_marker": "[CORTEX-WAKE]"})
    _stdin(monkeypatch, {"session_id": "s1", "prompt": "[CORTEX-WAKE] wake"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert "note" not in _ctx(capsys)


# ── Item 3: monitor-death rearm inject ────────────────────────────────────────

_DEATH = ('<task-notification>\n<summary>Monitor event: "ear"</summary>\n'
          '<event>[Monitor stopped — too much output.]</event>\n'
          '</task-notification>')


def test_monitor_death_injects_rearm(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _enable(monkeypatch, tmp_path,
            {"rearm_text": "rearm: tail {signal_log}"})
    _stdin(monkeypatch, {"session_id": "s1", "prompt": _DEATH})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == f"rearm: tail {tmp_path/'state'/'wake_signal.log'}"


def test_monitor_death_silent_on_normal_chat(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _enable(monkeypatch, tmp_path, {"rearm_text": "rearm {signal_log}"})
    _stdin(monkeypatch, {"session_id": "s1", "prompt": "Monitor 工具怎么用啊"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert "rearm" not in _ctx(capsys)


def test_monitor_death_blank_text_silent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _enable(monkeypatch, tmp_path, {"rearm_text": ""})
    _stdin(monkeypatch, {"session_id": "s1", "prompt": _DEATH})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == ""


# ── Item 1: SessionStart arm line (fresh cortex window) ───────────────────────

def _ss_db(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    storage.init_db(db).close()
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return db


def test_arm_line_injected_fresh_cortex_window(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _ss_db(tmp_path, monkeypatch)
    _enable(monkeypatch, tmp_path, {"arm_ear_text": "arm: tail {signal_log}"})
    jl = tmp_path / "s.jsonl"
    jl.write_text("", encoding="utf-8")
    _stdin(monkeypatch, {"session_id": "fresh1", "cwd": str(tmp_path),
                         "transcript_path": str(jl)})
    assert hooks.main(["session_start"]) == 0
    assert f"arm: tail {tmp_path/'state'/'wake_signal.log'}" in _ctx(capsys)


def test_arm_line_blank_silent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _ss_db(tmp_path, monkeypatch)
    _enable(monkeypatch, tmp_path, {"arm_ear_text": ""})
    jl = tmp_path / "s.jsonl"
    jl.write_text("", encoding="utf-8")
    _stdin(monkeypatch, {"session_id": "fresh2", "cwd": str(tmp_path),
                         "transcript_path": str(jl)})
    assert hooks.main(["session_start"]) == 0
    assert "arm:" not in _ctx(capsys)


def test_arm_line_skipped_non_cortex(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    _ss_db(tmp_path, monkeypatch)
    _enable(monkeypatch, tmp_path, {"arm_ear_text": "arm: tail {signal_log}"})
    jl = tmp_path / "s.jsonl"
    jl.write_text("", encoding="utf-8")
    _stdin(monkeypatch, {"session_id": "fresh3", "cwd": str(tmp_path),
                         "transcript_path": str(jl)})
    assert hooks.main(["session_start"]) == 0
    assert "arm:" not in _ctx(capsys)


# ── Resume: resume_ear_text inject + no arm regression ────────────────────────

def _mark_resume(db, sid):
    """Seed a prior lifecycle:start row so SessionStart classifies sid a resume."""
    conn = storage.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'session_lifecycle:start', 'ppid=1,source=cc')",
            (sid,),
        )
    conn.close()


def _write_wake_state(tmp_path, transcript):
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "wake_state.json").write_text(
        json.dumps({"awake": True, "transcript": str(transcript)}),
        encoding="utf-8")


def test_resume_resident_injects_resume_ear_text(tmp_path, monkeypatch, capsys):
    """Resident resume (wake_state transcript == this session) → re-arm guidance
    + orphan cleanup, never the fresh-window arm line."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    db = _ss_db(tmp_path, monkeypatch)
    _enable(monkeypatch, tmp_path,
            {"arm_ear_text": "arm: tail {signal_log}",
             "resume_ear_text": "resume: retail {signal_log}",
             "retired_ear_text": "retired: read only"})
    called = {"n": 0}
    monkeypatch.setattr(cortex_bridge, "kill_orphan_ear_tails",
                        lambda: called.__setitem__("n", called["n"] + 1) or 0)
    _mark_resume(db, "res1")
    jl = tmp_path / "s.jsonl"
    jl.write_text("", encoding="utf-8")
    _write_wake_state(tmp_path, jl)
    _stdin(monkeypatch, {"session_id": "res1", "cwd": str(tmp_path),
                         "transcript_path": str(jl)})
    assert hooks.main(["session_start"]) == 0
    ctx = _ctx(capsys)
    assert f"resume: retail {tmp_path/'state'/'wake_signal.log'}" in ctx
    assert "arm: tail" not in ctx
    assert "retired:" not in ctx
    assert called["n"] == 1  # orphan cleanup ran in the resident case


def test_resume_retired_injects_retired_text_no_cleanup(tmp_path, monkeypatch, capsys):
    """Retired resume (wake_state transcript points at a DIFFERENT session) →
    read-only guidance, NO orphan cleanup (must not kill resident's tail)."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    db = _ss_db(tmp_path, monkeypatch)
    _enable(monkeypatch, tmp_path,
            {"resume_ear_text": "resume: retail {signal_log}",
             "retired_ear_text": "retired: read only"})
    called = {"n": 0}
    monkeypatch.setattr(cortex_bridge, "kill_orphan_ear_tails",
                        lambda: called.__setitem__("n", called["n"] + 1) or 0)
    _mark_resume(db, "res3")
    jl = tmp_path / "old.jsonl"
    jl.write_text("", encoding="utf-8")
    # Resident pointer is a NEWER transcript, not this one.
    _write_wake_state(tmp_path, tmp_path / "newer.jsonl")
    _stdin(monkeypatch, {"session_id": "res3", "cwd": str(tmp_path),
                         "transcript_path": str(jl)})
    assert hooks.main(["session_start"]) == 0
    ctx = _ctx(capsys)
    assert "retired: read only" in ctx
    assert "resume: retail" not in ctx
    assert called["n"] == 0  # orphan cleanup must NOT run for a retired window


def test_is_resident_session_branch_decision(tmp_path, monkeypatch):
    """Deterministic match/no-match decision off wake_state.transcript."""
    _enable(monkeypatch, tmp_path, {})
    # Match.
    _write_wake_state(tmp_path, tmp_path / "a.jsonl")
    assert cortex_bridge.is_resident_session(str(tmp_path / "a.jsonl")) is True
    # No match → retired.
    assert cortex_bridge.is_resident_session(str(tmp_path / "b.jsonl")) is False
    # Empty/missing pointer defaults to resident.
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "wake_state.json").write_text(
        json.dumps({"awake": True}), encoding="utf-8")
    assert cortex_bridge.is_resident_session(str(tmp_path / "a.jsonl")) is True


def test_fresh_window_still_arms_not_resume(tmp_path, monkeypatch, capsys):
    """Regression: a fresh window keeps injecting arm_ear_text, never resume."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _ss_db(tmp_path, monkeypatch)
    _enable(monkeypatch, tmp_path,
            {"arm_ear_text": "arm: tail {signal_log}",
             "resume_ear_text": "resume: retail {signal_log}"})
    jl = tmp_path / "s.jsonl"
    jl.write_text("", encoding="utf-8")
    _stdin(monkeypatch, {"session_id": "freshR", "cwd": str(tmp_path),
                         "transcript_path": str(jl)})
    assert hooks.main(["session_start"]) == 0
    ctx = _ctx(capsys)
    assert f"arm: tail {tmp_path/'state'/'wake_signal.log'}" in ctx
    assert "resume: retail" not in ctx


def test_resume_blank_text_silent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    db = _ss_db(tmp_path, monkeypatch)
    _enable(monkeypatch, tmp_path, {"resume_ear_text": ""})
    monkeypatch.setattr(cortex_bridge, "kill_orphan_ear_tails", lambda: 0)
    _mark_resume(db, "res2")
    jl = tmp_path / "s.jsonl"
    jl.write_text("", encoding="utf-8")
    _stdin(monkeypatch, {"session_id": "res2", "cwd": str(tmp_path),
                         "transcript_path": str(jl)})
    assert hooks.main(["session_start"]) == 0
    assert "resume:" not in _ctx(capsys)


# ── Resume no-completion-record notice is NOT monitor death ───────────────────

_NO_COMPLETION = (
    '<task-notification>\n<task-id>bxybfk5js</task-id>\n'
    '<tool-use-id>toolu_x</tool-use-id>\n<status>stopped</status>\n'
    '<summary>No completion record was found for this background shell command '
    'from the previous session. It may have been stopped (via the UI, Monitor '
    'timeout, or agent teardown — these leave no transcript marker), or it may '
    'have been running when the previous Claude Code process exited.</summary>\n'
    '</task-notification>'
)


def test_no_completion_record_not_monitor_death():
    assert cortex_bridge.is_monitor_death(_NO_COMPLETION) is False


def test_no_completion_record_no_rearm_inject(tmp_path, monkeypatch, capsys):
    """The resume notice must not trigger the mid-window rearm flow."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _enable(monkeypatch, tmp_path, {"rearm_text": "rearm: tail {signal_log}"})
    _stdin(monkeypatch, {"session_id": "s1", "prompt": _NO_COMPLETION})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert "rearm" not in _ctx(capsys)
