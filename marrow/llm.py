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
from . import storage

_ISOLATION = ["--setting-sources", "", "--strict-mcp-config"]

# Lowercase prefixes of policy-refusal prose (defense-in-depth; primary
# signal is stop_reason=="refusal"). Keep short — match intent, not wording.
_REFUSAL_FINGERPRINTS = (
    "i'm not able to",
    "i am not able to",
    "i can't assist",
    "i cannot assist",
    "i can't help",
    "i cannot help",
    "i'm unable to",
    "i am unable to",
    "i won't be able to",
    "i will not be able to",
    "i'm going to decline",
    "i must decline",
)

# ollama is chronically down on this host; while muted it is dropped from the
# chain entirely so a transient claude miss does not rotate into a guaranteed
# "unreachable" critical-alert storm. Code path (_run_ollama) kept intact —
# flip to False to bring it back. Provider-level retry absorbs the kind of
# transient claude miss that previously needed the fallback.
_MUTE_OLLAMA = True
_RETRIES = 1  # per provider, before rotating


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
        self.chain = [
            nm for k in ("default", "fallback", "emergency")
            if (nm := llm.get(k))
            and not (_MUTE_OLLAMA
                     and (llm.get(nm) or {}).get("kind") == "ollama")
        ]
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
            for attempt in range(_RETRIES + 1):
                try:
                    return self._run(spec, model, body)
                except Exception as e:
                    last = e
                    if attempt < _RETRIES:
                        continue  # transient miss — one retry, same provider
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
        timeout = spec.get("timeout_s", 120)
        cmd = [_claude_bin(), "--output-format", "stream-json",
               "--input-format", "stream-json", "--verbose",
               "--model", model, *_ISOLATION]
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
        raw = "\n".join(lines)
        result = self._parse_claude(raw, "stream-json")
        self._log_usage(self._extract_usage(raw, "stream-json"), model, "stream-json")
        return result

    def _run_claude_p(self, spec: dict, model: str, prompt: str) -> str:
        cmd = [_claude_bin(), "-p", prompt, "--model", model,
               *_ISOLATION, "--output-format", "json"]
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
        result = self._parse_claude(out, "json")
        self._log_usage(self._extract_usage(out, "json"), model, "json")
        return result

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
        # Refusal sentinel (P0): stop_reason=="refusal" is the primary signal;
        # fingerprint scan is defense-in-depth (is_error may be false on refusal).
        if rec.get("stop_reason") == "refusal":
            raise LLMError(
                f"claude_cli refusal (stop_reason): {str(rec.get('result'))[:120]}")
        text = rec.get("result")
        if not text:
            raise LLMError("claude_cli: empty result")
        low = text.lower().lstrip()
        if any(low.startswith(fp) for fp in _REFUSAL_FINGERPRINTS):
            raise LLMError(f"claude_cli refusal (fingerprint): {text[:120]}")
        return text

    @staticmethod
    def _extract_usage(out: str, fmt: str) -> dict | None:
        """Parse usage/modelUsage from the result event. Returns None on any failure."""
        try:
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
                    return None
            else:
                j = json.loads(out)
                recs = [x for x in j if x.get("type") == "result"] \
                    if isinstance(j, list) else [j]
                rec = recs[0] if recs else None
                if rec is None:
                    return None
            usage = rec.get("usage") or rec.get("modelUsage")
            if not usage or not isinstance(usage, dict):
                return None
            return usage
        except Exception:
            return None

    def _log_usage(self, usage: dict | None, model: str, fmt: str) -> None:
        """Write a cost-monitor row to audit_log. Best-effort: never raises."""
        if not usage:
            return
        try:
            input_tok = usage.get("input_tokens", 0)
            output_tok = usage.get("output_tokens", 0)
            cache_read = usage.get("cache_read_input_tokens", 0)
            cache_write = usage.get("cache_creation_input_tokens", 0)
            summary = (
                f"model={model} fmt={fmt} "
                f"in={input_tok} out={output_tok} "
                f"cache_read={cache_read} cache_write={cache_write}"
            )
            conn = storage.connect()
            with conn:
                conn.execute(
                    "INSERT INTO audit_log (target_table, action, summary)"
                    " VALUES (?, ?, ?)",
                    ("llm_usage", "llm_call_cost", summary),
                )
            conn.close()
        except Exception:
            pass

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
