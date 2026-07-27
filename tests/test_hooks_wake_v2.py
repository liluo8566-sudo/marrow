"""Wake-pipeline v2 injections in hooks (cortex window only):
- UserPromptSubmit wake-turn full-note inject
- SessionStart injects nothing (Monitor ear retired; wake is typed in)
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
# The visible bell is human text ([cortex].wake_bell_template, default "☀️ {hm}").
# Machine data lives in the wake_state receipt; recognition is receipt-exact then
# template-shape fallback. Legacy '[CORTEX-WAKE]' recognition was removed (5f7efe7).

def _seed_epoch(tmp_path, gen, state_id):
    (tmp_path / "state").mkdir(exist_ok=True)
    p = tmp_path / "state" / "wake_state.json"
    d = json.loads(p.read_text()) if p.exists() else {}
    d.update({"gen": gen, "state_id": state_id})
    p.write_text(json.dumps(d), encoding="utf-8")


def _seed_receipt(tmp_path, text="☀️ 14:00", gen=None, state_id=None):
    from datetime import datetime, timezone
    (tmp_path / "state").mkdir(exist_ok=True)
    p = tmp_path / "state" / "wake_state.json"
    d = json.loads(p.read_text()) if p.exists() else {}
    r = {"text": text, "ts": datetime.now(timezone.utc).isoformat()}
    if gen is not None:
        r["gen"] = gen
        r["state_id"] = state_id
    d["wake_receipt"] = r
    p.write_text(json.dumps(d), encoding="utf-8")


def test_wake_turn_injects_full_note(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    (tmp_path / "wakeup_note.md").write_text("## cli\nread me and act", encoding="utf-8")
    _enable(monkeypatch, tmp_path)
    _seed_receipt(tmp_path, text="☀️ 14:00")
    _stdin(monkeypatch, {"session_id": "s1", "prompt": "☀️ 14:00"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == "read me and act"


def test_wake_turn_current_token_injects(tmp_path, monkeypatch, capsys):
    """A receipt whose token matches the live epoch injects the note."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    (tmp_path / "wakeup_note.md").write_text("## cli\nread me and act", encoding="utf-8")
    _enable(monkeypatch, tmp_path)
    _seed_epoch(tmp_path, 7, "abcd1234")
    _seed_receipt(tmp_path, text="☀️ 14:00", gen=7, state_id="abcd1234")
    _stdin(monkeypatch, {"session_id": "s1", "prompt": "☀️ 14:00"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == "read me and act"


def test_wake_turn_stale_token_suppressed(tmp_path, monkeypatch, capsys):
    """A receipt whose token was superseded (newer gen) is NOT processed as a
    wake: no note injected."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    (tmp_path / "wakeup_note.md").write_text("## cli\nread me and act", encoding="utf-8")
    _enable(monkeypatch, tmp_path)
    _seed_epoch(tmp_path, 8, "abcd1234")  # live gen moved past the receipt's gen 7
    _seed_receipt(tmp_path, text="☀️ 14:00", gen=7, state_id="abcd1234")
    _stdin(monkeypatch, {"session_id": "s1", "prompt": "☀️ 14:00"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == ""  # suppressed


def test_wake_turn_tokenless_receipt_still_injects(tmp_path, monkeypatch, capsys):
    """A token-less receipt is processed as before even when an epoch is recorded."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    (tmp_path / "wakeup_note.md").write_text("## cli\nread me and act", encoding="utf-8")
    _enable(monkeypatch, tmp_path)
    _seed_epoch(tmp_path, 8, "abcd1234")
    _seed_receipt(tmp_path, text="☀️ 14:00")  # no gen/state_id on the receipt
    _stdin(monkeypatch, {"session_id": "s1", "prompt": "☀️ 14:00"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == "read me and act"


def test_wake_turn_missing_note_silent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _enable(monkeypatch, tmp_path)
    _seed_receipt(tmp_path, text="☀️ 14:00")
    _stdin(monkeypatch, {"session_id": "s1", "prompt": "☀️ 14:00"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == ""


def test_ordinary_chat_no_note_inject(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    (tmp_path / "wakeup_note.md").write_text("## cli\nsecret note", encoding="utf-8")
    _enable(monkeypatch, tmp_path)
    _seed_receipt(tmp_path, text="☀️ 14:00")
    _stdin(monkeypatch, {"session_id": "s1", "prompt": "今天过得怎么样"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert "secret note" not in _ctx(capsys)


# ── GAP 2: WAKE branch is exact/line-start shaped, not substring ───────────────

def test_wake_bell_mid_sentence_not_swallowed(tmp_path, monkeypatch, capsys):
    """A REAL user prompt merely quoting the bell text mid-sentence must NOT be
    swallowed by the wake branch: no note injected, and the user-wake reset fires
    (it is user speech). Receipt match is exact full-line, never a substring."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    (tmp_path / "wakeup_note.md").write_text("## cli\nsecret note", encoding="utf-8")
    _enable(monkeypatch, tmp_path)
    _seed_receipt(tmp_path, text="☀️ 14:00")
    called = {"reset": False}
    monkeypatch.setattr(cortex_bridge, "_cortex_user_wake_reset",
                        lambda inp: called.__setitem__("reset", True))
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": "/t/s.jsonl",
                         "prompt": "did ☀️ 14:00 fire in the log?"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert "secret note" not in _ctx(capsys)  # wake note NOT injected
    assert called["reset"] is True            # treated as a real user message


def test_wake_bell_shape_fallback_fires_wake_branch(tmp_path, monkeypatch, capsys):
    """A real wake bell with NO receipt on disk still fires the wake branch via
    the template-shape fallback (fail-open) — full note injected, no user-wake
    reset."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    (tmp_path / "wakeup_note.md").write_text("## cli\nread me and act", encoding="utf-8")
    _enable(monkeypatch, tmp_path)
    called = {"reset": False}
    monkeypatch.setattr(cortex_bridge, "_cortex_user_wake_reset",
                        lambda inp: called.__setitem__("reset", True))
    # No receipt on disk -> shape fallback matches the config template exactly
    # (default = "[☀️ {hm}]").
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": "/t/s.jsonl",
                         "prompt": "[☀️ 14:00]"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == "read me and act"  # wake branch fired
    assert called["reset"] is False           # NOT a user message


def test_wake_bell_receipt_fires_wake_branch(tmp_path, monkeypatch, capsys):
    """A receipt exact match with a current epoch token fires the wake branch."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    (tmp_path / "wakeup_note.md").write_text("## cli\nread me and act", encoding="utf-8")
    _enable(monkeypatch, tmp_path)
    _seed_epoch(tmp_path, 7, "abcd1234")
    _seed_receipt(tmp_path, text="☀️ 14:00", gen=7, state_id="abcd1234")
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": "/t/s.jsonl",
                         "prompt": "☀️ 14:00"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == "read me and act"


def test_wake_bell_wrapped_envelope_fires_wake_branch(tmp_path, monkeypatch, capsys):
    """Delivered by the ear Monitor the bell arrives wrapped:
    `<event>☀️ 14:00</event>` — the envelope-aware exact match still fires the
    wake branch."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    (tmp_path / "wakeup_note.md").write_text("## cli\nread me and act", encoding="utf-8")
    _enable(monkeypatch, tmp_path)
    _seed_epoch(tmp_path, 7, "abcd1234")
    _seed_receipt(tmp_path, text="☀️ 14:00", gen=7, state_id="abcd1234")
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": "/t/s.jsonl",
                         "prompt": "<event>☀️ 14:00</event>"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == "read me and act"


# ── Phase 2.5 item 1: free-round tuck-in carries its note INLINE — the hook must
#    NOT also turn-inject the full note (07-14 double-note incident). ───────────

def _read_gen(tmp_path):
    d = json.loads((tmp_path / "state" / "wake_state.json").read_text())
    return d.get("gen")


def test_tuck_in_line_with_nothing_staged_injects_nothing(tmp_path, monkeypatch, capsys):
    """A [NEW ROUND] marker turn with no staged payload injects nothing — and it
    never falls back to the frozen wakeup note (no double note, 07-14)."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    (tmp_path / "wakeup_note.md").write_text("## cli\nFROZEN note", encoding="utf-8")
    _enable(monkeypatch, tmp_path, {"tuck_in_marker": "[NEW ROUND]"})
    _stdin(monkeypatch, {"session_id": "s1",
                         "prompt": "⏳ [NEW ROUND] 15 min"})
    assert hooks.main(["user_prompt_submit"]) == 0
    ctx = _ctx(capsys)
    assert "FROZEN note" not in ctx     # never the frozen note
    assert ctx == ""


def test_tuck_in_line_injects_staged_note_covertly(tmp_path, monkeypatch, capsys):
    """Only the short ⏳ marker is typed into the window; the free-round note
    cortex staged is injected as additionalContext (invisible), then CONSUMED so
    a later marker turn can never replay it."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    staged = tmp_path / "free_round_note.md"
    staged.write_text("📮 小道消息\nNow: 14:00", encoding="utf-8")
    _enable(monkeypatch, tmp_path, {"tuck_in_marker": "[NEW ROUND]"})
    _stdin(monkeypatch, {"session_id": "s1", "prompt": "⏳ [NEW ROUND] 15 min"})
    assert hooks.main(["user_prompt_submit"]) == 0
    ctx = _ctx(capsys)
    assert "小道消息" in ctx and "Now: 14:00" in ctx
    assert not staged.exists()          # consume-once

    # Second marker turn with nothing staged -> nothing injected (no replay).
    _stdin(monkeypatch, {"session_id": "s1", "prompt": "⏳ [NEW ROUND] 15 min"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == ""


def test_tuck_in_staged_note_expired_is_dropped(tmp_path, monkeypatch, capsys):
    """A payload whose marker turn never arrived (window died between staging
    and the prompt) is dropped past receipt_ttl_min instead of surfacing on an
    unrelated later round."""
    import os
    import time
    monkeypatch.setenv("MARROW_CORTEX", "1")
    staged = tmp_path / "free_round_note.md"
    staged.write_text("STALE payload", encoding="utf-8")
    old = time.time() - 3600
    os.utime(staged, (old, old))
    _enable(monkeypatch, tmp_path, {"tuck_in_marker": "[NEW ROUND]",
                                    "receipt_ttl_min": 15})
    _stdin(monkeypatch, {"session_id": "s1", "prompt": "⏳ [NEW ROUND] 15 min"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == ""
    assert not staged.exists()          # dropped, not left to rot


# ── Item 4: FUSE / CTL covert body inject (marker on screen, body via hook) ────

def test_fuse_marker_injects_body_covertly(tmp_path, monkeypatch, capsys):
    """A ⚙️ [FUSE] marker turn (bare or ear-wrapped) injects the FUSE body via
    additionalContext — the body never rode the log line. {handoff} renders as
    this shell's own handoff path (cli here)."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _enable(monkeypatch, tmp_path, {})
    _stdin(monkeypatch, {"session_id": "s1",
                         "prompt": '<event>⚙️ [FUSE]</event>'})
    assert hooks.main(["user_prompt_submit"]) == 0
    ctx = _ctx(capsys)
    assert str(tmp_path / "handoff-cli.md") in ctx
    assert "{handoff}" not in ctx
    assert "lie_down(rotate=True)" in ctx


def test_fuse_blank_body_injects_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _enable(monkeypatch, tmp_path, {"fuse_prompt_text": ""})
    _stdin(monkeypatch, {"session_id": "s1", "prompt": "⚙️ [FUSE]"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == ""


def test_ctl_marker_renders_body_from_args_rotate(tmp_path, monkeypatch, capsys):
    """A ⚙️ [CTL] marker turn carrying mins/rotate args injects the sleep body
    rendered from those args (rotate=true -> handoff prefix + rotate=true arg)."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _enable(monkeypatch, tmp_path, {})
    _stdin(monkeypatch, {"session_id": "s1",
                         "prompt": "⚙️ [CTL] mins=30 rotate=true"})
    assert hooks.main(["user_prompt_submit"]) == 0
    ctx = _ctx(capsys)
    assert "lie_down(next_wake_min=30, rotate=true)" in ctx
    assert "write your handoff" in ctx


def test_ctl_marker_no_rotate_omits_rotate_arg(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _enable(monkeypatch, tmp_path, {})
    _stdin(monkeypatch, {"session_id": "s1",
                         "prompt": "⚙️ [CTL] mins=15 rotate=false"})
    assert hooks.main(["user_prompt_submit"]) == 0
    ctx = _ctx(capsys)
    assert "lie_down(next_wake_min=15)" in ctx
    assert "rotate=true" not in ctx


def test_ctl_marker_human_true_renders_human_override(tmp_path, monkeypatch, capsys):
    """P17: an explicit /ct-sleep minutes marker carries human=true — the
    rendered lie_down(...) call must include human_override=True so it pierces
    the clamp band end to end."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _enable(monkeypatch, tmp_path, {})
    _stdin(monkeypatch, {"session_id": "s1",
                         "prompt": "⚙️ [CTL] mins=10 rotate=false human=true"})
    assert hooks.main(["user_prompt_submit"]) == 0
    ctx = _ctx(capsys)
    assert "lie_down(next_wake_min=10, human_override=True)" in ctx


def test_ctl_marker_human_false_omits_human_override(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _enable(monkeypatch, tmp_path, {})
    _stdin(monkeypatch, {"session_id": "s1",
                         "prompt": "⚙️ [CTL] mins=30 rotate=true human=false"})
    assert hooks.main(["user_prompt_submit"]) == 0
    ctx = _ctx(capsys)
    assert "lie_down(next_wake_min=30, rotate=true)" in ctx
    assert "human_override" not in ctx


def test_ctl_marker_no_human_field_backward_compatible(tmp_path, monkeypatch, capsys):
    """A marker line with no human= field (older cortex build) still renders
    cleanly — no human_override arg, no crash."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _enable(monkeypatch, tmp_path, {})
    _stdin(monkeypatch, {"session_id": "s1",
                         "prompt": "⚙️ [CTL] mins=15 rotate=false"})
    assert hooks.main(["user_prompt_submit"]) == 0
    ctx = _ctx(capsys)
    assert "lie_down(next_wake_min=15)" in ctx
    assert "human_override" not in ctx


def test_fuse_ctl_markers_not_swallowed_mid_sentence(tmp_path, monkeypatch, capsys):
    """A real user prompt quoting [FUSE]/[CTL] mid-sentence is NOT swallowed —
    the covert branches are line-start shaped, so it falls through to user speech
    (user-wake reset fires)."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _enable(monkeypatch, tmp_path, {})
    called = {"reset": False}
    monkeypatch.setattr(cortex_bridge, "_cortex_user_wake_reset",
                        lambda inp: called.__setitem__("reset", True))
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": "/t/s.jsonl",
                         "prompt": "did the [FUSE] path or [CTL] path fire?"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert called["reset"] is True


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
    (tmp_path / "wakeup_note.md").write_text("## cli\nfrozen", encoding="utf-8")
    _enable(monkeypatch, tmp_path, {"render_module": "cortex.note_render",
                                    "venv_python": "/x/py", "repo_root": "/x"})

    class _P:
        returncode = 0
        stdout = "FRESH note SID feed1234"
        stderr = ""
    monkeypatch.setattr(cortex_bridge.subprocess, "run", lambda *a, **k: _P())
    assert cortex_bridge.wakeup_note_text("/t/feed1234ab.jsonl") == "FRESH note SID feed1234"


def test_wakeup_note_render_asks_the_renderer_to_mirror_this_shell(tmp_path, monkeypatch):
    """marrow no longer rewrites wakeup_note.md itself — it tells the renderer
    which shell it is and asks it to store that shell's section (--mirror), so a
    tg render can never overwrite the cli section."""
    monkeypatch.setenv("MARROW_CORTEX", "tg")
    _enable(monkeypatch, tmp_path, {"render_module": "cortex.note_render",
                                    "venv_python": "/x/py", "repo_root": "/x"})
    seen = {}

    class _P:
        returncode = 0
        stdout = "FRESH mirrored note"
        stderr = ""

    def _run(cmd, **k):
        seen["cmd"] = cmd
        return _P()
    monkeypatch.setattr(cortex_bridge.subprocess, "run", _run)
    assert cortex_bridge.wakeup_note_text("/t/x.jsonl") == "FRESH mirrored note"
    assert "--mirror" in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("--shell") + 1] == "tg"


def test_wakeup_note_falls_back_on_render_failure(tmp_path, monkeypatch):
    """Subprocess failure / non-zero / empty => frozen file is returned."""
    (tmp_path / "wakeup_note.md").write_text("## cli\nfrozen fallback", encoding="utf-8")
    _enable(monkeypatch, tmp_path, {"render_module": "cortex.note_render",
                                    "venv_python": "/x/py", "repo_root": "/x"})

    def _boom(*a, **k):
        raise OSError("no such venv")
    monkeypatch.setattr(cortex_bridge.subprocess, "run", _boom)
    assert cortex_bridge.wakeup_note_text("/t/x.jsonl") == "frozen fallback"


def test_wakeup_note_no_render_module_uses_file(tmp_path, monkeypatch):
    """render_module unset => never spawns, static file only (feature disabled)."""
    (tmp_path / "wakeup_note.md").write_text("## cli\nstatic only", encoding="utf-8")
    _enable(monkeypatch, tmp_path, {"venv_python": "/x/py", "repo_root": "/x"})

    def _fail(*a, **k):
        raise AssertionError("subprocess must not run when render_module unset")
    monkeypatch.setattr(cortex_bridge.subprocess, "run", _fail)
    assert cortex_bridge.wakeup_note_text("/t/x.jsonl") == "static only"


def test_non_cortex_session_no_wake_inject(tmp_path, monkeypatch, capsys):
    """No MARROW_CORTEX => the whole cortex branch is skipped."""
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    (tmp_path / "wakeup_note.md").write_text("## cli\nnote", encoding="utf-8")
    _enable(monkeypatch, tmp_path)
    _seed_receipt(tmp_path, text="☀️ 14:00")
    _stdin(monkeypatch, {"session_id": "s1", "prompt": "☀️ 14:00"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert "note" not in _ctx(capsys)


# ── Monitor burial: no arm/rearm/orphan-tail machinery survives ───────────────

_DEATH = ('<task-notification>\n<summary>Monitor event: "ear"</summary>\n'
          '<event>[Monitor stopped — too much output.]</event>\n'
          '</task-notification>')


def test_monitor_death_prompt_passes_through_untouched(tmp_path, monkeypatch, capsys):
    """A Monitor-stopped notification is no longer a special shape: the hook
    injects nothing for it (the ear it referred to no longer exists)."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _enable(monkeypatch, tmp_path, {})
    _stdin(monkeypatch, {"session_id": "s1", "prompt": _DEATH})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert "rearm" not in _ctx(capsys)


def test_rearm_helpers_removed():
    """The Monitor arm/rearm/orphan-tail machinery is gone from the bridge."""
    for name in ("arm_ear_text", "resume_ear_text", "retired_ear_text",
                 "rearm_text", "is_monitor_death", "is_resident_session",
                 "kill_orphan_ear_tails", "_ARM_EAR_TEXT", "_RESUME_EAR_TEXT",
                 "_RETIRED_EAR_TEXT", "_REARM_TEXT"):
        assert not hasattr(cortex_bridge, name), name


# ── SessionStart: cortex window gets no injection at all ──────────────────────

def _ss_db(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    storage.init_db(db).close()
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return db


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


def _ss_ctx(tmp_path, monkeypatch, capsys, sid, jl):
    _stdin(monkeypatch, {"session_id": sid, "cwd": str(tmp_path),
                         "transcript_path": str(jl)})
    assert hooks.main(["session_start"]) == 0
    return _ctx(capsys)


def test_fresh_cortex_window_injects_no_ear_copy(tmp_path, monkeypatch, capsys):
    """Fresh window: page-turn still runs, but no Monitor/arm copy is injected."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _ss_db(tmp_path, monkeypatch)
    _enable(monkeypatch, tmp_path, {})
    jl = tmp_path / "s.jsonl"
    jl.write_text("", encoding="utf-8")
    ctx = _ss_ctx(tmp_path, monkeypatch, capsys, "fresh1", jl)
    assert "wake_signal.log" not in ctx
    assert "persistent monitor" not in ctx


def test_resume_cortex_window_injects_no_ear_copy(tmp_path, monkeypatch, capsys):
    """Resident resume: no re-arm guidance, no orphan-tail cleanup."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    db = _ss_db(tmp_path, monkeypatch)
    _enable(monkeypatch, tmp_path, {})
    _mark_resume(db, "res1")
    jl = tmp_path / "s.jsonl"
    jl.write_text("", encoding="utf-8")
    _write_wake_state(tmp_path, jl)
    ctx = _ss_ctx(tmp_path, monkeypatch, capsys, "res1", jl)
    assert "has been resumed" not in ctx
    assert "wake_signal.log" not in ctx


def test_retired_cortex_window_injects_no_ear_copy(tmp_path, monkeypatch, capsys):
    """Retired resume (wake_state points at a newer transcript): still nothing."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    db = _ss_db(tmp_path, monkeypatch)
    _enable(monkeypatch, tmp_path, {})
    _mark_resume(db, "res3")
    jl = tmp_path / "old.jsonl"
    jl.write_text("", encoding="utf-8")
    _write_wake_state(tmp_path, tmp_path / "newer.jsonl")
    ctx = _ss_ctx(tmp_path, monkeypatch, capsys, "res3", jl)
    assert "archived session" not in ctx
    assert "wake_signal.log" not in ctx


def test_page_turn_still_runs_on_fresh_window(tmp_path, monkeypatch, capsys):
    """The surviving SessionStart side effect: the handoff page-turn call."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _ss_db(tmp_path, monkeypatch)
    _enable(monkeypatch, tmp_path, {})
    seen = {"n": 0}
    monkeypatch.setattr(cortex_bridge, "_cortex_handoff_page_turn_if_stale",
                        lambda: seen.__setitem__("n", seen["n"] + 1))
    jl = tmp_path / "s.jsonl"
    jl.write_text("", encoding="utf-8")
    _ss_ctx(tmp_path, monkeypatch, capsys, "freshP", jl)
    assert seen["n"] == 1
