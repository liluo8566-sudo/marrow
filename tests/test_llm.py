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


def _fake_claude_usage_stream(tmp_path, events: list[dict], result="ok"):
    """A claude stand-in that emits the given raw stream-json events
    (assistant usage lines, verbatim) then a result event, for exercising
    the real (unmocked) _stream_subprocess cap-accumulation loop
    end-to-end."""
    lines = [json.dumps(ev) for ev in events]
    lines.append(json.dumps({"type": "result", "result": result, "is_error": False}))
    s = tmp_path / "fake_claude_usage"
    body = "\n".join(f"print({ln!r})" for ln in lines)
    s.write_text(f"#!/usr/bin/env python3\n{body}\n")
    s.chmod(0o755)
    return str(s)


def _new_sink():
    return {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0,
            "window": 0, "capped": False, "by_request": {}, "has_usage": False}


def test_stream_subprocess_dedupes_repeated_requestid_lines(tmp_path):
    """Regression (07-04 live incident): one API turn streams as multiple
    assistant lines (thinking/tool_use/text) each repeating identical usage
    under the same requestId. Corrected true numbers for the two live turns:
    turn1 in=3 cache_creation=24159 out=290 (x2 duplicate lines); turn2 in=3
    cache_read=24159 cache_creation=1572 out=417 (x3 duplicate lines). True
    cumulative: in=6 out=707 cache_read=24159 cache_write=25731; final
    window (last turn's in+cache_read+cache_creation) = 25734."""
    turn1_usage = {"input_tokens": 3, "output_tokens": 290,
                   "cache_creation_input_tokens": 24159}
    turn2_usage = {"input_tokens": 3, "output_tokens": 417,
                   "cache_read_input_tokens": 24159, "cache_creation_input_tokens": 1572}
    events = (
        [{"type": "assistant", "requestId": "req-1", "message": {"usage": turn1_usage}}] * 2
        + [{"type": "assistant", "requestId": "req-2", "message": {"usage": turn2_usage}}] * 3
    )
    bin_ = _fake_claude_usage_stream(tmp_path, events)
    sink = _new_sink()
    LLMClient._stream_subprocess(
        [bin_], "hi", 10, dict(os.environ), max_tokens=150000, usage_sink=sink)
    assert sink["capped"] is False
    assert (sink["in"], sink["out"], sink["cache_read"], sink["cache_write"]) \
        == (6, 707, 24159, 25731)
    assert sink["window"] == 25734


# --- rate_limit_event -> ct_rate_limit kv snapshot (HANDOVER queue item 2) ---
# Cortex bulletin (bulletin.py, cortex commit 9ee25e5) reads exactly:
# five_hour_pct, five_hour_reset_at, seven_day_pct, seven_day_reset_at,
# window_tokens. rate_limit_event carries NO percentage (verified 07-04
# live probe: status/resetsAt/rateLimitType/overageStatus/overageResetsAt/
# isUsingOverage only) — *_pct keys are never written from this source,
# only *_reset_at (+ extra raw fields under their own snake_case keys).
# window_tokens comes from the cap-tracking sink, not this event.

def _kv(db_connect, key):
    row = db_connect.execute(
        "SELECT value FROM ct_rate_limit WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def test_snapshot_rate_limit_writes_reset_at_and_extra_fields(tmp_path, monkeypatch):
    from marrow import storage as stor

    db = str(tmp_path / "test.db")
    _real_connect = stor.connect
    stor.init_db(db)
    monkeypatch.setattr("marrow.llm.storage.connect",
                        lambda path=None: _real_connect(db))

    # Live-verified shape (07-04 probe): top-level type=="rate_limit_event",
    # payload nested under "rate_limit_info".
    ev = {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "resetsAt": 1783150800,
        "rateLimitType": "five_hour", "overageStatus": "allowed",
        "overageResetsAt": 1785542400, "isUsingOverage": False,
    }, "uuid": "x", "session_id": "y"}
    from marrow.llm import _snapshot_rate_limit
    _snapshot_rate_limit(ev)

    read_conn = _real_connect(db)
    assert _kv(read_conn, "five_hour_reset_at") == "2026-07-04T07:40:00+00:00"
    assert _kv(read_conn, "five_hour_status") == "allowed"
    assert _kv(read_conn, "five_hour_is_using_overage") == "False"
    assert _kv(read_conn, "five_hour_pct") is None  # never available, omitted
    read_conn.close()


def test_snapshot_rate_limit_latest_wins_on_repeat(tmp_path, monkeypatch):
    from marrow import storage as stor

    db = str(tmp_path / "test.db")
    _real_connect = stor.connect
    stor.init_db(db)
    monkeypatch.setattr("marrow.llm.storage.connect",
                        lambda path=None: _real_connect(db))
    from marrow.llm import _snapshot_rate_limit

    _snapshot_rate_limit({"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "rateLimitType": "five_hour"}})
    _snapshot_rate_limit({"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "rateLimitType": "five_hour"}})

    read_conn = _real_connect(db)
    rows = read_conn.execute(
        "SELECT value FROM ct_rate_limit WHERE key='five_hour_status'").fetchall()
    read_conn.close()
    assert len(rows) == 1  # overwrite, not append
    assert rows[0]["value"] == "rejected"


def test_snapshot_rate_limit_ignores_non_rate_limit_events(tmp_path, monkeypatch):
    from marrow import storage as stor

    db = str(tmp_path / "test.db")
    _real_connect = stor.connect
    stor.init_db(db)
    monkeypatch.setattr("marrow.llm.storage.connect",
                        lambda path=None: _real_connect(db))
    from marrow.llm import _snapshot_rate_limit

    _snapshot_rate_limit({"type": "assistant", "message": {"usage": {}}})
    read_conn = _real_connect(db)
    n = read_conn.execute("SELECT COUNT(*) c FROM ct_rate_limit").fetchone()["c"]
    read_conn.close()
    assert n == 0


def test_snapshot_rate_limit_missing_info_is_noop(tmp_path, monkeypatch):
    from marrow import storage as stor

    db = str(tmp_path / "test.db")
    _real_connect = stor.connect
    stor.init_db(db)
    monkeypatch.setattr("marrow.llm.storage.connect",
                        lambda path=None: _real_connect(db))
    from marrow.llm import _snapshot_rate_limit

    _snapshot_rate_limit({"type": "rate_limit_event"})  # malformed, no payload
    read_conn = _real_connect(db)
    n = read_conn.execute("SELECT COUNT(*) c FROM ct_rate_limit").fetchone()["c"]
    read_conn.close()
    assert n == 0


def test_stream_subprocess_snapshots_rate_limit_event(tmp_path, monkeypatch):
    """End-to-end: _stream_subprocess's own event loop (not a direct
    _snapshot_rate_limit call) picks up a real rate_limit_event frame."""
    from marrow import storage as stor

    db = str(tmp_path / "test.db")
    _real_connect = stor.connect
    stor.init_db(db)
    monkeypatch.setattr("marrow.llm.storage.connect",
                        lambda path=None: _real_connect(db))

    events = [{"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "rateLimitType": "five_hour"}}]
    bin_ = _fake_claude_usage_stream(tmp_path, events)
    LLMClient._stream_subprocess([bin_], "hi", 10, dict(os.environ))

    read_conn = _real_connect(db)
    assert _kv(read_conn, "five_hour_status") == "allowed"
    read_conn.close()


def test_snapshot_window_tokens_writes_contract_key(tmp_path, monkeypatch):
    from marrow import storage as stor

    db = str(tmp_path / "test.db")
    _real_connect = stor.connect
    stor.init_db(db)
    monkeypatch.setattr("marrow.llm.storage.connect",
                        lambda path=None: _real_connect(db))
    from marrow.llm import _snapshot_window_tokens

    _snapshot_window_tokens(25734)
    read_conn = _real_connect(db)
    assert _kv(read_conn, "window_tokens") == "25734"
    read_conn.close()


def test_run_claude_cortex_snapshots_window_tokens(monkeypatch, tmp_path):
    """_run_claude_cortex writes window_tokens from the cap sink on a
    normal (non-capped) completion, reusing the already-computed figure."""
    from marrow import storage as stor

    db = str(tmp_path / "test.db")
    _real_connect = stor.connect
    stor.init_db(db)
    monkeypatch.setattr("marrow.llm.storage.connect",
                        lambda path=None: _real_connect(db))

    turn_usage = {"input_tokens": 100, "output_tokens": 20,
                  "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 0}
    events = [{"type": "assistant", "requestId": "r1",
               "message": {"usage": turn_usage}}]
    bin_ = _fake_claude_usage_stream(tmp_path, events, result="hi")
    monkeypatch.setattr("marrow.llm._claude_bin", lambda: bin_)
    c = LLMClient({"llm": {"claude_cli_cortex": {"kind": "claude_cli", "timeout_s": 5}},
                   "tiers": {}, "cortex": {}})
    out = c._run_claude_cortex(
        {"kind": "claude_cli", "timeout_s": 5}, "m", "hi",
        cwd=str(tmp_path), resume_sid=None, max_tokens=150000)
    assert out.get("capped") is not True
    read_conn = _real_connect(db)
    assert _kv(read_conn, "window_tokens") == "5100"
    read_conn.close()


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
    assert captured["env"]["MARROW_CHANNEL"] == "ct"
    assert "MARROW_PIPELINE" not in captured["env"]
    assert captured["cwd"] == str(tmp_path)
    assert "--model" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--model") + 1] == "claude-opus"
    assert "--permission-mode" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--permission-mode") + 1] == "bypassPermissions"


def test_call_cortex_model_override_bypasses_tier(monkeypatch, tmp_path):
    cfg = {**CORTEX_CFG, "cortex": {**CORTEX_CFG["cortex"],
                                     "model": "claude-opus-4-6"}}
    c = LLMClient(cfg)
    captured = {}

    def fake_stream(cmd, prompt, timeout, env, cwd=None):
        captured["cmd"] = cmd
        return _cortex_stream_out("hi there")

    monkeypatch.setattr(c, "_stream_subprocess", fake_stream)
    c.call_cortex("hello", cwd=str(tmp_path))
    assert captured["cmd"][captured["cmd"].index("--model") + 1] == "claude-opus-4-6"


def test_call_cortex_effort_flag_passed_when_set(monkeypatch, tmp_path):
    cfg = {**CORTEX_CFG, "cortex": {**CORTEX_CFG["cortex"], "effort": "medium"}}
    c = LLMClient(cfg)
    captured = {}

    def fake_stream(cmd, prompt, timeout, env, cwd=None):
        captured["cmd"] = cmd
        return _cortex_stream_out("hi there")

    monkeypatch.setattr(c, "_stream_subprocess", fake_stream)
    c.call_cortex("hello", cwd=str(tmp_path))
    assert "--effort" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--effort") + 1] == "medium"


def test_call_cortex_effort_flag_omitted_when_empty(monkeypatch, tmp_path):
    c = LLMClient(CORTEX_CFG)
    captured = {}

    def fake_stream(cmd, prompt, timeout, env, cwd=None):
        captured["cmd"] = cmd
        return _cortex_stream_out("hi there")

    monkeypatch.setattr(c, "_stream_subprocess", fake_stream)
    c.call_cortex("hello", cwd=str(tmp_path))
    assert "--effort" not in captured["cmd"]


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


def test_call_cortex_timeout_override(monkeypatch, tmp_path):
    """Caller-supplied timeout overrides the provider default so the cortex
    config is the single source of truth for the call budget."""
    c = LLMClient(CORTEX_CFG)
    captured = {}

    def fake_stream(cmd, prompt, timeout, env, cwd=None):
        captured["timeout"] = timeout
        return _cortex_stream_out("ok")

    monkeypatch.setattr(c, "_stream_subprocess", fake_stream)
    c.call_cortex("hello", cwd=str(tmp_path), timeout=123)
    assert captured["timeout"] == 123


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


# --- Cortex per-wake token cap (accumulator + dedupe + window breach) ---

def _assistant(request_id="req-1", **usage):
    ev = {"type": "assistant", "message": {"usage": usage}}
    if request_id is not None:
        ev["requestId"] = request_id
    return ev


def test_add_event_usage_sums_four_fields_and_window():
    sink = _new_sink()
    LLMClient._add_event_usage(sink, _assistant(
        input_tokens=100, output_tokens=50,
        cache_read_input_tokens=10, cache_creation_input_tokens=5))
    assert (sink["in"], sink["out"], sink["cache_read"], sink["cache_write"]) \
        == (100, 50, 10, 5)
    assert sink["window"] == 115  # in+cache_read+cache_creation this turn


def test_add_event_usage_ignores_non_assistant_no_double_count():
    sink = _new_sink()
    LLMClient._add_event_usage(sink, _assistant(output_tokens=100))
    # result event: top-level usage, no message.usage -> must not add
    LLMClient._add_event_usage(sink, {"type": "result", "usage": {"input_tokens": 999}})
    # tool-result / user event -> no usage -> must not add
    LLMClient._add_event_usage(sink, {"type": "user", "message": {"content": "x"}})
    assert (sink["in"], sink["out"], sink["cache_read"], sink["cache_write"]) \
        == (0, 100, 0, 0)


def test_add_event_usage_dedupes_repeated_requestid():
    """A single API turn streams as several assistant lines (thinking/
    tool_use/text) all repeating the identical usage under the same
    requestId — the accumulator must count it once, not N times."""
    sink = _new_sink()
    usage = dict(input_tokens=100, output_tokens=50,
                 cache_read_input_tokens=10, cache_creation_input_tokens=5)
    for _ in range(3):  # three duplicate lines, same turn
        LLMClient._add_event_usage(sink, _assistant(request_id="req-1", **usage))
    assert (sink["in"], sink["out"], sink["cache_read"], sink["cache_write"]) \
        == (100, 50, 10, 5)  # counted once, not tripled
    assert sink["window"] == 115


def test_add_event_usage_repeated_small_turns_do_not_breach_window_cap():
    """Window semantics (Decided 07-04): the cap compares the LATEST turn's
    window, not a cumulative sum. Many small distinct turns whose cumulative
    total exceeds the cap must NOT breach as long as each turn's own window
    stays under it."""
    sink = _new_sink()
    cap = 150
    breached = False
    for n in range(5):  # 5 turns x 60 input tokens = 300 cumulative, over cap
        LLMClient._add_event_usage(sink, _assistant(
            request_id=f"req-{n}", input_tokens=60))
        if sink["window"] >= cap:
            breached = True
            break
    assert not breached
    assert sink["window"] == 60  # each turn's own window, not cumulative
    assert sink["in"] == 300  # cumulative (audit) sum still correct


def test_add_event_usage_fat_single_turn_breaches_window_cap():
    """A single turn whose own window (input+cache_read+cache_creation)
    exceeds the cap breaches immediately — even on turn 1 of a resumed
    session replaying a large cache."""
    sink = _new_sink()
    cap = 150000
    LLMClient._add_event_usage(sink, _assistant(
        input_tokens=16, output_tokens=97,
        cache_read_input_tokens=74049, cache_creation_input_tokens=77643))
    assert sink["window"] == 151708  # in+cache_read+cache_creation
    assert sink["window"] >= cap


def test_sink_usage_maps_to_audit_fields():
    sink = {"in": 1, "out": 2, "cache_read": 3, "cache_write": 4,
            "window": 8, "capped": False, "by_request": {}, "has_usage": True}
    assert LLMClient._sink_usage(sink) == {
        "input_tokens": 1, "output_tokens": 2,
        "cache_read_input_tokens": 3, "cache_creation_input_tokens": 4}


def test_run_claude_cortex_cap_breach_returns_capped(monkeypatch, tmp_path):
    c = LLMClient(CORTEX_CFG)
    monkeypatch.setattr(c, "_log_usage", lambda *a, **k: None)
    cap_rows = []
    monkeypatch.setattr(c, "_log_cortex_cap",
                        lambda sink, cap, model: cap_rows.append((sink["window"], cap)))

    def fake_stream(cmd, prompt, timeout, env, cwd=None,
                    on_event=None, max_tokens=None, usage_sink=None):
        LLMClient._add_event_usage(usage_sink, _assistant(input_tokens=max_tokens))
        if usage_sink["window"] >= max_tokens:
            usage_sink["capped"] = True
        return ""  # killed mid-stream, no result event

    monkeypatch.setattr(c, "_stream_subprocess", fake_stream)
    out = c.call_cortex("hi", cwd=str(tmp_path), max_tokens=1000)
    assert out["capped"] is True
    assert out["session_id"] is None
    assert out["total_tokens"] == 1000
    assert cap_rows == [(1000, 1000)]


def test_run_claude_cortex_cap_active_reports_window(monkeypatch, tmp_path):
    c = LLMClient(CORTEX_CFG)
    monkeypatch.setattr(c, "_log_usage", lambda *a, **k: None)

    def fake_stream(cmd, prompt, timeout, env, cwd=None,
                    on_event=None, max_tokens=None, usage_sink=None):
        LLMClient._add_event_usage(
            usage_sink, _assistant(input_tokens=100, output_tokens=50))
        return _cortex_stream_out("done")

    monkeypatch.setattr(c, "_stream_subprocess", fake_stream)
    out = c.call_cortex("hi", cwd=str(tmp_path), max_tokens=1000)
    assert out["text"] == "done"
    assert out["session_id"] == "sess-abc"
    assert out["total_tokens"] == 100  # window = in+cache_read+cache_creation


def test_call_cortex_cap_zero_disables_and_keeps_plain_shape(monkeypatch, tmp_path):
    # max_tokens=0 -> cap inactive -> legacy return shape (no total_tokens)
    c = LLMClient(CORTEX_CFG)

    def fake_stream(cmd, prompt, timeout, env, cwd=None):
        return _cortex_stream_out("ok")

    monkeypatch.setattr(c, "_stream_subprocess", fake_stream)
    out = c.call_cortex("hi", cwd=str(tmp_path), max_tokens=0)
    assert out == {"text": "ok", "session_id": "sess-abc"}
