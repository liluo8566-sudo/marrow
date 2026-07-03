import json
import os
import time

import pytest

from marrow.llm import LLMClient, LLMError

CFG = {
    "llm": {
        "default": "claude_cli",
        "claude_cli": {"kind": "claude_cli", "mode": "json", "timeout_s": 5},
    },
    "tiers": {"cheap": "claude-haiku-4-5-20251001"},
}


def _json_out(result, is_error=False, stop_reason=None, usage=None):
    rec = {"type": "result", "result": result, "is_error": is_error}
    if stop_reason is not None:
        rec["stop_reason"] = stop_reason
    if usage is not None:
        rec["usage"] = usage
    return json.dumps([{"type": "system", "subtype": "init"}, rec])


def _stream_out(result, stop_reason=None, usage=None):
    rec = {"type": "result", "result": result, "is_error": False}
    if stop_reason is not None:
        rec["stop_reason"] = stop_reason
    if usage is not None:
        rec["usage"] = usage
    return "\n".join([
        json.dumps({"type": "system"}),
        json.dumps(rec),
    ])


def test_parse_json_result():
    assert LLMClient._parse_claude(_json_out("hi"), "json") == "hi"


def test_parse_stream_json_takes_last_result():
    out = "\n".join([
        json.dumps({"type": "system"}),
        json.dumps({"type": "result", "result": "old"}),
        json.dumps({"type": "result", "result": "final"}),
    ])
    assert LLMClient._parse_claude(out, "stream-json") == "final"


def test_parse_is_error_raises():
    with pytest.raises(LLMError, match="is_error"):
        LLMClient._parse_claude(_json_out("boom", is_error=True), "json")


def test_parse_empty_raises():
    with pytest.raises(LLMError, match="empty result"):
        LLMClient._parse_claude(_json_out(""), "json")


def test_retry_absorbs_transient_miss_no_alert(monkeypatch):
    alerts = []
    c = LLMClient(CFG, on_alert=lambda *a: alerts.append(a))
    calls = []

    def flaky(spec, model, prompt):
        calls.append(1)
        if len(calls) == 1:
            raise LLMError("transient")
        return "ok-2nd"

    monkeypatch.setattr(c, "_run_claude_cli", flaky)
    assert c.call("diary", "body", tier="cheap") == "ok-2nd"
    assert len(calls) == 2  # one retry, same provider
    assert alerts == []  # transient miss never alerts


def test_claude_only_failure_is_warn(monkeypatch):
    alerts = []
    c = LLMClient(CFG, on_alert=lambda *a: alerts.append(a))
    monkeypatch.setattr(
        c, "_run_claude_cli",
        lambda s, m, p: (_ for _ in ()).throw(LLMError("cli down")))
    with pytest.raises(LLMError, match="all providers failed"):
        c.call("diary", "body", tier="cheap")
    assert alerts and alerts[-1][0] == "warn"
    assert "no fallback configured" in alerts[-1][2]




def test_p_timeout_kills_process_group(tmp_path, monkeypatch):
    bin_, pidfile = _fake_claude(tmp_path)
    monkeypatch.setattr("marrow.llm._claude_bin", lambda: bin_)
    c = LLMClient(CFG)
    with pytest.raises(LLMError, match="timeout"):
        c._run_claude_p({"timeout_s": 1}, "m", "hi")
    gc = _grandchild_pid(pidfile)
    assert _wait_dead(gc), f"orphan grandchild {gc} survived -p timeout"


def test_tier_falls_back_to_cheap(monkeypatch):
    c = LLMClient(CFG)
    captured = {}
    monkeypatch.setattr(c, "_run",
                        lambda spec, model, p: captured.setdefault("m", model) or "ok")
    c.call("r", "b", tier="nonexistent")
    assert captured["m"] == "claude-haiku-4-5-20251001"


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _wait_dead(pid, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


def _fake_claude(tmp_path):
    """A claude stand-in: parent holds the stdout pipe (so killing it ends
    the stream read at once), and spawns a detached long-lived grandchild
    that records its pid. The grandchild only dies if the whole process
    group is killed, not just the parent."""
    pidfile = tmp_path / "gc.pid"
    gc = (f'import os,time;open({str(pidfile)!r},"w")'
          '.write(str(os.getpid()));time.sleep(300)')
    s = tmp_path / "fake_claude"
    s.write_text(
        "#!/usr/bin/env python3\n"
        "import sys,subprocess,time\n"
        f"subprocess.Popen([sys.executable,'-c',{gc!r}],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL)\n"
        "time.sleep(300)\n"
    )
    s.chmod(0o755)
    return str(s), pidfile


def _grandchild_pid(pidfile):
    for _ in range(80):
        if pidfile.exists() and pidfile.read_text().strip():
            return int(pidfile.read_text())
        time.sleep(0.1)
    raise AssertionError("grandchild never started")


def test_stream_timeout_kills_process_group(tmp_path, monkeypatch):
    bin_, pidfile = _fake_claude(tmp_path)
    monkeypatch.setattr("marrow.llm._claude_bin", lambda: bin_)
    c = LLMClient(CFG)
    with pytest.raises(LLMError):
        c._run_claude_stream({"timeout_s": 1}, "m", "hi")
    gc = _grandchild_pid(pidfile)
    assert _wait_dead(gc), f"orphan grandchild {gc} survived stream timeout"


# --- Refusal sentinel tests (P0) ---

def test_refusal_stop_reason_raises_json():
    refusal_text = "I'm not able to help with that request."
    with pytest.raises(LLMError, match="refusal"):
        LLMClient._parse_claude(
            _json_out(refusal_text, stop_reason="refusal"), "json")


def test_refusal_stop_reason_raises_stream():
    refusal_text = "I'm unable to assist with that."
    with pytest.raises(LLMError, match="refusal"):
        LLMClient._parse_claude(
            _stream_out(refusal_text, stop_reason="refusal"), "stream-json")


def test_refusal_fingerprint_raises_without_stop_reason():
    # is_error=False, no stop_reason — the real hole this sentinel closes
    refusal_text = "I'm unable to assist with this request. It violates policy."
    with pytest.raises(LLMError, match="refusal"):
        LLMClient._parse_claude(_json_out(refusal_text), "json")


def test_refusal_fingerprint_case_insensitive_leading_whitespace():
    refusal_text = "  I cannot assist with that.\nMore text."
    with pytest.raises(LLMError, match="refusal"):
        LLMClient._parse_claude(_json_out(refusal_text), "json")


def test_normal_text_starting_with_i_not_flagged():
    # "I think" / "I found" etc. must not false-positive
    safe = "I think the answer is 42."
    assert LLMClient._parse_claude(_json_out(safe), "json") == safe


def test_refusal_never_returned_as_success_via_call(monkeypatch):
    # End-to-end: refusal triggers LLMError through call(), chain exhausts
    c = LLMClient(CFG)
    refusal_out = _json_out(
        "I'm not able to help with that.", stop_reason="refusal")

    def fake_run(spec, model, prompt):
        return LLMClient._parse_claude(refusal_out, "json")

    monkeypatch.setattr(c, "_run_claude_cli", fake_run)
    with pytest.raises(LLMError):
        c.call("diary", "body")


# --- Cost monitor tests ---

def test_extract_usage_json():
    usage = {"input_tokens": 100, "output_tokens": 50}
    out = _json_out("ok", usage=usage)
    result = LLMClient._extract_usage(out, "json")
    assert result == usage


def test_extract_usage_stream():
    usage = {"input_tokens": 200, "output_tokens": 80, "cache_read_input_tokens": 10}
    out = _stream_out("ok", usage=usage)
    result = LLMClient._extract_usage(out, "stream-json")
    assert result == usage


def test_extract_usage_missing_returns_none():
    out = _json_out("ok")
    assert LLMClient._extract_usage(out, "json") is None


def test_extract_usage_garbage_returns_none():
    assert LLMClient._extract_usage("not json at all", "json") is None


def test_log_usage_writes_audit_row(tmp_path, monkeypatch):
    from marrow import storage as stor

    db = str(tmp_path / "test.db")
    _real_connect = stor.connect  # capture before any patch

    stor.init_db(db)  # creates all tables incl. audit_log
    monkeypatch.setattr("marrow.llm.storage.connect",
                        lambda path=None: _real_connect(db))

    c = LLMClient(CFG)
    usage = {"input_tokens": 1000, "output_tokens": 300,
             "cache_read_input_tokens": 50, "cache_creation_input_tokens": 20}
    c._log_usage(usage, "claude-haiku", "stream-json")

    read_conn = _real_connect(db)
    rows = read_conn.execute(
        "SELECT action, summary FROM audit_log WHERE target_table='llm_usage'"
    ).fetchall()
    read_conn.close()
    assert len(rows) == 1
    action, summary = rows[0]
    assert action == "llm_call_cost"
    assert "in=1000" in summary
    assert "out=300" in summary
    assert "cache_read=50" in summary


def test_log_usage_none_does_not_write(tmp_path, monkeypatch):
    # _log_usage(None, ...) is a no-op — no DB interaction
    written = []
    c = LLMClient(CFG)
    monkeypatch.setattr("marrow.llm.storage.connect",
                        lambda path=None: written.append(1) or (_ for _ in ()).throw(
                            AssertionError("should not connect")))
    c._log_usage(None, "m", "json")  # must not raise
    assert written == []


# --- Cortex full-env runner (C3) ---

CORTEX_CFG = {
    "llm": {
        "default": "claude_cli",
        "claude_cli": {"kind": "claude_cli", "mode": "stream", "timeout_s": 5},
        "claude_cli_cortex": {"kind": "claude_cli_cortex", "timeout_s": 5},
    },
    "tiers": {"cheap": "claude-haiku", "top": "claude-opus"},
    "cortex": {"home": "/tmp/does-not-matter-mocked", "tier": "top"},
}


def _cortex_stream_out(result, session_id="sess-abc"):
    rec = {"type": "result", "result": result, "is_error": False,
           "session_id": session_id}
    return "\n".join([
        json.dumps({"type": "system", "session_id": session_id}),
        json.dumps(rec),
    ])


def test_extract_session_id():
    out = _cortex_stream_out("ok", session_id="sess-xyz")
    assert LLMClient._extract_session_id(out) == "sess-xyz"


def test_extract_session_id_missing_returns_none():
    out = json.dumps({"type": "result", "result": "ok"})
    assert LLMClient._extract_session_id(out) is None


def test_extract_session_id_garbage_returns_none():
    assert LLMClient._extract_session_id("not json") is None


def test_call_cortex_no_isolation_flags(monkeypatch, tmp_path):
    c = LLMClient(CORTEX_CFG)
    captured = {}

    def fake_stream(cmd, prompt, timeout, env, cwd=None):
        captured["cmd"] = cmd
        captured["env"] = env
        captured["cwd"] = cwd
        return _cortex_stream_out("hi there")

    monkeypatch.setattr(c, "_stream_subprocess", fake_stream)
    out = c.call_cortex("hello", cwd=str(tmp_path))
    assert out == {"text": "hi there", "session_id": "sess-abc"}
    assert "--setting-sources" not in captured["cmd"]
    assert "--strict-mcp-config" not in captured["cmd"]
    assert captured["env"]["MARROW_CORTEX"] == "1"
    assert "MARROW_PIPELINE" not in captured["env"]
    assert captured["cwd"] == str(tmp_path)
    assert "--model" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--model") + 1] == "claude-opus"
    assert "--permission-mode" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--permission-mode") + 1] == "bypassPermissions"


def test_call_cortex_default_timeout_is_600(monkeypatch, tmp_path):
    cfg = {**CORTEX_CFG, "llm": {**CORTEX_CFG["llm"],
           "claude_cli_cortex": {"kind": "claude_cli_cortex"}}}
    c = LLMClient(cfg)
    captured = {}

    def fake_stream(cmd, prompt, timeout, env, cwd=None):
        captured["timeout"] = timeout
        return _cortex_stream_out("ok")

    monkeypatch.setattr(c, "_stream_subprocess", fake_stream)
    c.call_cortex("hello", cwd=str(tmp_path))
    assert captured["timeout"] == 600


def test_call_cortex_resume_sid_passes_resume_flag(monkeypatch, tmp_path):
    c = LLMClient(CORTEX_CFG)
    captured = {}

    def fake_stream(cmd, prompt, timeout, env, cwd=None):
        captured["cmd"] = cmd
        return _cortex_stream_out("ok")

    monkeypatch.setattr(c, "_stream_subprocess", fake_stream)
    c.call_cortex("hello", cwd=str(tmp_path), resume_sid="prior-sid")
    assert "--resume" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--resume") + 1] == "prior-sid"


def test_call_cortex_fresh_omits_resume_flag(monkeypatch, tmp_path):
    c = LLMClient(CORTEX_CFG)
    captured = {}

    def fake_stream(cmd, prompt, timeout, env, cwd=None):
        captured["cmd"] = cmd
        return _cortex_stream_out("ok")

    monkeypatch.setattr(c, "_stream_subprocess", fake_stream)
    c.call_cortex("hello", cwd=str(tmp_path))
    assert "--resume" not in captured["cmd"]


def test_call_cortex_defaults_cwd_and_creates_dir(monkeypatch, tmp_path):
    cfg = {**CORTEX_CFG, "cortex": {"home": str(tmp_path / "cortex_home"),
                                     "tier": "top"}}
    c = LLMClient(cfg)
    captured = {}

    def fake_stream(cmd, prompt, timeout, env, cwd=None):
        captured["cwd"] = cwd
        return _cortex_stream_out("ok")

    monkeypatch.setattr(c, "_stream_subprocess", fake_stream)
    c.call_cortex("hello")
    assert captured["cwd"] == str(tmp_path / "cortex_home")
    assert (tmp_path / "cortex_home").is_dir()


def test_call_cortex_missing_provider_raises():
    c = LLMClient({"llm": {"default": "claude_cli",
                            "claude_cli": {"kind": "claude_cli"}},
                    "tiers": {"cheap": "haiku"}})
    with pytest.raises(LLMError, match="claude_cli_cortex"):
        c.call_cortex("hello")


def test_isolation_flags_still_present_on_default_stream(monkeypatch):
    """Untouched-path guard: the existing pipeline stream runner must keep
    the isolation flags + MARROW_PIPELINE after the shared-helper refactor."""
    c = LLMClient(CFG)
    captured = {}

    def fake_stream(cmd, prompt, timeout, env, cwd=None):
        captured["cmd"] = cmd
        captured["env"] = env
        captured["cwd"] = cwd
        return _stream_out("ok")

    monkeypatch.setattr(c, "_stream_subprocess", fake_stream)
    c._run_claude_stream({"timeout_s": 5, "mode": "stream"}, "m", "hi")
    assert "--setting-sources" in captured["cmd"]
    assert "--strict-mcp-config" in captured["cmd"]
    assert captured["env"]["MARROW_PIPELINE"] == "1"
    assert captured["cwd"] is None


def test_log_usage_db_failure_does_not_raise(monkeypatch):
    c = LLMClient(CFG)
    # storage.connect raises — _log_usage must swallow it silently
    monkeypatch.setattr("marrow.llm.storage.connect",
                        lambda path=None: (_ for _ in ()).throw(OSError("db gone")))
    c._log_usage({"input_tokens": 1}, "m", "json")  # must not raise
