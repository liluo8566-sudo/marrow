"""mw watcher — bootstrap / kickstart / stop / status."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from marrow import cli, config


@pytest.fixture()
def fake_launch(tmp_path, monkeypatch):
    """Stub launchctl + filesystem so the test never touches the real plist."""
    plist_target = tmp_path / "com.marrow.watcher.plist"
    monkeypatch.setattr(cli, "_LAUNCH_AGENTS", tmp_path)
    monkeypatch.setattr(cli, "_watcher_plist_target",
                        lambda: plist_target)
    # Point the src plist at a real file we control.
    src = tmp_path / "src.plist"
    src.write_text("<plist/>")
    monkeypatch.setattr(cli, "_WATCHER_PLIST_SRC", src)

    calls: list[tuple] = []

    def fake_run(args, **kw):
        calls.append(tuple(args))
        if args[:2] == ["id", "-u"]:
            return subprocess.CompletedProcess(args, 0, "501\n", "")
        if args[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(
                args, 0, "state = running\npid = 12345\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data" / "logs").mkdir(parents=True)
    return calls, plist_target


def test_watcher_start_bootstraps_and_kickstarts(fake_launch, capsys):
    calls, target = fake_launch
    rc = cli.main(["watcher", "start"])
    assert rc == 0
    cmds = [c for c in calls if c[0] == "launchctl"]
    actions = [c[1] for c in cmds]
    assert "bootout" in actions
    assert "bootstrap" in actions
    assert "kickstart" in actions
    out = capsys.readouterr().out
    assert "bootstrapped" in out
    assert "kickstarted" in out
    assert target.exists(), "plist should be copied to LaunchAgents"


def test_watcher_stop_calls_bootout(fake_launch, capsys):
    calls, _ = fake_launch
    rc = cli.main(["watcher", "stop"])
    assert rc == 0
    assert any(c[0] == "launchctl" and c[1] == "bootout" for c in calls)
    assert "unloaded" in capsys.readouterr().out


def test_watcher_status_prints_state(fake_launch, capsys):
    rc = cli.main(["watcher", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "state=running" in out
    assert "pid=12345" in out


def test_watcher_status_tails_log(fake_launch, capsys, tmp_path):
    log = tmp_path / "data" / "logs" / "watcher.log"
    log.write_text("\n".join(f"line {i}" for i in range(20)) + "\n")
    cli.main(["watcher", "status"])
    out = capsys.readouterr().out
    assert "line 19" in out
    assert "line 15" in out  # last 5 = lines 15..19
    assert "line 14" not in out


def test_watcher_status_no_log(fake_launch, capsys):
    cli.main(["watcher", "status"])
    out = capsys.readouterr().out
    assert "no log yet" in out
