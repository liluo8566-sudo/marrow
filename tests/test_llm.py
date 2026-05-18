import json
import os
import time

import pytest

from marrow.llm import LLMClient, LLMError

CFG = {
    "llm": {
        "default": "claude_cli",
        "emergency": "ollama",
        "claude_cli": {"kind": "claude_cli", "mode": "json", "timeout_s": 5},
        "ollama": {"kind": "ollama", "model": "m", "timeout_s": 5},
    },
    "tiers": {"cheap": "claude-haiku-4-5-20251001"},
}


def _json_out(result, is_error=False):
    return json.dumps([
        {"type": "system", "subtype": "init"},
        {"type": "result", "result": result, "is_error": is_error},
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


def test_chain_rotates_to_emergency_and_alerts(monkeypatch):
    alerts = []
    c = LLMClient(CFG, on_alert=lambda *a: alerts.append(a))

    def boom_cli(spec, model, prompt):
        raise LLMError("cli down")

    monkeypatch.setattr(c, "_run_claude_cli", boom_cli)
    monkeypatch.setattr(c, "_run_ollama", lambda spec, prompt: "from-ollama")
    assert c.call("diary", "body", tier="cheap") == "from-ollama"
    assert alerts and alerts[0][0] == "warn"
    assert "rotating" in alerts[0][2]


def test_whole_chain_fails_raises_and_critical_alert(monkeypatch):
    alerts = []
    c = LLMClient(CFG, on_alert=lambda *a: alerts.append(a))
    monkeypatch.setattr(c, "_run", lambda *a: (_ for _ in ()).throw(LLMError("x")))
    with pytest.raises(LLMError, match="all providers failed"):
        c.call("lesson", "b")
    assert any(a[0] == "critical" for a in alerts)


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
