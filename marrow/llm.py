"""LLM provider client. Callers pass intent (role + body + tier); provider,
flags, model, channel are config. One chain: default -> emergency, generic
over an ordered list so a fallback link is a config edit, not code.

claude_cli isolation is built in and non-negotiable: a pipeline call must
never inherit persona / user MCP / output-style.

WHY no-`-p` stream-json = the OAuth 5h subscription window (not the 6/15
credit pool), what each flag does, and when `-p` is the fallback:
see docs/adr/0003-subscription-window-stream-json.md. Do not re-derive.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import urllib.error
import urllib.request

from . import config

_ISOLATION = ["--setting-sources", "", "--strict-mcp-config"]


class LLMError(Exception):
    pass


def _claude_bin() -> str:
    b = shutil.which("claude")
    if not b:
        raise LLMError("claude CLI not found on PATH")
    return b


def _kill_group(pgid: int, sig: int) -> None:
    """Kill the whole process group so claude's spawned descendants die
    with it, not just the direct child (orphan leak on timeout)."""
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


class LLMClient:
    def __init__(self, cfg: dict | None = None, on_alert=None):
        self.cfg = cfg or config.load()
        llm = self.cfg.get("llm", {})
        self.chain = [llm[k] for k in ("default", "fallback", "emergency")
                      if llm.get(k)]
        self.specs = llm
        self.tiers = self.cfg.get("tiers", {})
        self._on_alert = on_alert

    def _alert(self, severity, atype, message, source):
        if self._on_alert:
            try:
                self._on_alert(severity, atype, message, source)
            except Exception:
                pass

    def call(self, role: str, body: str, *, tier: str = "cheap") -> str:
        model = self.tiers.get(tier) or self.tiers.get("cheap")
        last = None
        for i, name in enumerate(self.chain):
            spec = self.specs.get(name)
            if not spec:
                continue
            try:
                return self._run(spec, model, body)
            except Exception as e:
                last = e
                terminal = i == len(self.chain) - 1
                self._alert(
                    "critical" if terminal else "warn",
                    "llm_provider",
                    f"{role}: provider {name} failed ({e}); "
                    + ("chain exhausted" if terminal else "rotating"),
                    f"llm.py:{name}",
                )
        raise LLMError(f"{role}: all providers failed; last: {last}")

    def _run(self, spec: dict, model: str, prompt: str) -> str:
        kind = spec.get("kind")
        if kind == "claude_cli":
            return self._run_claude_cli(spec, model, prompt)
        if kind == "ollama":
            return self._run_ollama(spec, prompt)
        raise LLMError(f"unknown provider kind: {kind}")

    def _run_claude_cli(self, spec: dict, model: str, prompt: str) -> str:
        # Default = subscription-window stream-json (no -p): pipe one user
        # message into an interactive claude over stdin, read events off
        # stdout until `result`. Verified to ride the OAuth five-hour window
        # (rate_limit_event five_hour, isUsingOverage:false), not the 6/15
        # credit pool. mode="p" is the legacy headless fallback.
        if spec.get("mode") == "p":
            return self._run_claude_p(spec, model, prompt)
        return self._run_claude_stream(spec, model, prompt)

    def _run_claude_stream(self, spec: dict, model: str, prompt: str) -> str:
        # -p + --tools "": stream-json I/O is documented as --print-only, and
        # without tool isolation claude acts on the digest material (paths,
        # imperatives) as an agent instead of compressing it -> empty/no
        # result. A bare PONG never triggers tools, which is why ADR-0003's
        # probe missed this. Both flags are load-bearing; see
        # docs/notes/2026-05-19_claude-cli-stream-json.md.
        timeout = spec.get("timeout_s", 120)
        cmd = [_claude_bin(), "-p",
               "--output-format", "stream-json",
               "--input-format", "stream-json", "--verbose",
               "--model", model, "--tools", "", *_ISOLATION]
        msg = json.dumps({"type": "user", "message": {
            "role": "user", "content": prompt}})
        try:
            p = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, start_new_session=True)
        except OSError as e:
            raise LLMError(f"claude_cli spawn failed: {e}") from e
        try:
            pgid = os.getpgid(p.pid)
        except ProcessLookupError:
            pgid = p.pid
        killer = threading.Timer(
            timeout, lambda: _kill_group(pgid, signal.SIGKILL))
        killer.start()
        lines: list[str] = []
        try:
            p.stdin.write(msg + "\n")
            p.stdin.flush()
            p.stdin.close()
            for line in p.stdout:
                line = line.strip()
                if not line:
                    continue
                lines.append(line)
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "result":
                    break
        finally:
            killer.cancel()
            if p.poll() is None:
                _kill_group(pgid, signal.SIGTERM)
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _kill_group(pgid, signal.SIGKILL)
            else:
                _kill_group(pgid, signal.SIGKILL)
        if not lines:
            err = (p.stderr.read() or "").strip()[:200]
            raise LLMError(f"claude_cli stream: no output ({err})")
        return self._parse_claude("\n".join(lines), "stream-json")

    def _run_claude_p(self, spec: dict, model: str, prompt: str) -> str:
        cmd = [_claude_bin(), "-p", prompt, "--model", model,
               "--tools", "", *_ISOLATION, "--output-format", "json"]
        timeout = spec.get("timeout_s", 120)
        try:
            p = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True)
        except OSError as e:
            raise LLMError(f"claude_cli spawn failed: {e}") from e
        try:
            pgid = os.getpgid(p.pid)
        except ProcessLookupError:
            pgid = p.pid
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as e:
            _kill_group(pgid, signal.SIGKILL)
            p.wait()
            raise LLMError(f"claude_cli timeout {timeout}s") from e
        finally:
            _kill_group(pgid, signal.SIGKILL)
        if p.returncode != 0:
            raise LLMError(
                f"claude_cli rc{p.returncode}: {err.strip()[:200]}")
        return self._parse_claude(out, "json")

    @staticmethod
    def _parse_claude(out: str, fmt: str) -> str:
        if fmt == "stream-json":
            rec = None
            for line in out.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "result":
                    rec = ev
            if rec is None:
                raise LLMError("claude_cli stream-json: no result event")
        else:
            j = json.loads(out)
            recs = [x for x in j if x.get("type") == "result"] \
                if isinstance(j, list) else [j]
            if not recs:
                raise LLMError("claude_cli json: no result event")
            rec = recs[0]
        if rec.get("is_error"):
            raise LLMError(f"claude_cli is_error: {str(rec.get('result'))[:200]}")
        text = rec.get("result")
        if not text:
            raise LLMError("claude_cli: empty result")
        return text

    def _run_ollama(self, spec: dict, prompt: str) -> str:
        payload = json.dumps({
            "model": spec.get("model", "qwen2.5:7b"),
            "prompt": prompt,
            "stream": False,
        }).encode()
        req = urllib.request.Request(
            "http://localhost:11434/api/generate",
            data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=spec.get("timeout_s", 180)) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LLMError(f"ollama unreachable: {e}") from e
        text = data.get("response")
        if not text:
            raise LLMError("ollama: empty response")
        return text
