"""LLM provider client. Callers pass intent (role + body + tier); provider,
flags, model, channel are config. One chain: default only (fallback/emergency removed).

claude_cli isolation is built in and non-negotiable: a pipeline call must
never inherit persona / user MCP / output-style.

Default channel is stream-json without `-p`: runs against the OAuth 5h
subscription window (not the 6/15 credit pool), keeps tool use + thinking
in band. `-p` is the manual fallback when subscription is exhausted or a
caller explicitly needs the print path. See DECISIONS.md.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config
from . import storage

_ISOLATION = ["--setting-sources", "", "--strict-mcp-config"]

# Prefixes of policy-refusal prose (defense-in-depth; primary signal is
# stop_reason=="refusal"). Keep short — match intent, not wording. CN entries
# are case-invariant so lower() match is fine for both. Lumi's content is
# mostly CN; EN-only fingerprints would miss the common refusal path.
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
    "很抱歉，我无法",
    "很抱歉，我不能",
    "抱歉，我无法",
    "抱歉，我不能",
    "对不起，我无法",
    "对不起，我不能",
    "我不能帮",
    "我无法协助",
    "我恐怕无法",
)

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


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cortex_stream_timer():
    """Env-driven stream-event timing probe for cortex wakes (wake latency
    diagnosis). Returns a callback appending one line per notable stage to
    CORTEX_WAKE_TIMING_LOG, or None when cortex did not request it. Best-effort:
    never raises into the stream loop. spawned -> first_event isolates claude
    CLI startup cost (MCP/env load) before the first token."""
    path = os.environ.get("CORTEX_WAKE_TIMING_LOG")
    if not path:
        return None
    path = os.path.expanduser(path)
    wake_id = os.environ.get("CORTEX_WAKE_ID", "?")
    origin = time.monotonic()
    seen: set[str] = set()

    def _emit(ev: dict, mono: float) -> None:
        try:
            etype = ev.get("type", "?")
            if etype == "__spawned__":
                label = "spawned"
            elif "first" not in seen:
                seen.add("first")
                label = "first_event"
            else:
                sub = ev.get("subtype")
                key = f"{etype}/{sub}" if sub else etype
                if key in seen:
                    return
                seen.add(key)
                label = f"ev.{key}"
            ms = (mono - origin) * 1000.0
            with open(path, "a") as f:
                f.write(f"{_utcnow_iso()} wake={wake_id} stream.{label} +{ms:.0f}ms\n")
        except Exception:
            pass

    return _emit


class LLMClient:
    def __init__(self, cfg: dict | None = None, on_alert=None):
        self.cfg = cfg or config.load()
        llm = self.cfg.get("llm", {})
        self.chain = [
            nm for k in ("default",)
            if (nm := llm.get(k))
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
                    multi = len(self.chain) > 1
                    terminal = multi and i == len(self.chain) - 1
                    if multi:
                        tail = "chain exhausted" if terminal else "rotating"
                    else:
                        tail = "no fallback configured"
                    self._alert(
                        "critical" if terminal else "warn",
                        "llm_provider",
                        f"{role}: provider {name} failed ({e}); {tail}",
                        f"llm.py:{name}",
                    )
        raise LLMError(f"{role}: all providers failed; last: {last}")

    def call_cortex(self, prompt: str, *, cwd: str | None = None,
                     resume_sid: str | None = None,
                     timeout: float | None = None,
                     max_tokens: int | None = None) -> dict:
        """Full-environment resumed session for cortex (C3, Decided 07-03):
        no isolation flags — persona/rules/MCP/agents load like a real
        session. Always injects MARROW_CORTEX=1 so marrow hooks + tl_add
        skip this session's turns (cortex must never enter chat memory).
        cwd defaults to [cortex].home; resume_sid=None starts a fresh
        session (daily rebirth). Single attempt — no chain/retry, caller
        (cortex pacemaker) owns retry policy. `timeout` (s) overrides the
        provider default so the caller's config is the single source of truth
        for the call budget (cortex derives its outer kill from the same value).
        `max_tokens` (>0) caps the per-wake CURRENT WINDOW SIZE — the latest
        turn's (input+cache_read+cache_creation), i.e. the same figure the
        caller reasons about via the statusline "total" (Decided 07-04, not
        cumulative consumption across turns): accumulated mid-stream (usage
        deduped by requestId — a single turn streams as several assistant
        lines each repeating identical usage), breach terminates the
        subprocess and returns capped=True so the caller rebirths. Returns
        {"text": str, "session_id": str | None} (+ capped / total_tokens
        [= final window size] when a cap is active).
        """
        spec = self.specs.get("claude_cli_cortex")
        if not spec:
            raise LLMError("no claude_cli_cortex provider configured")
        cortex_cfg = self.cfg.get("cortex", {})
        run_cwd = os.path.expanduser(
            cwd or cortex_cfg.get("home") or "~/.config/marrow/cortex")
        Path(run_cwd).mkdir(parents=True, exist_ok=True)
        tier = cortex_cfg.get("tier", "top")
        model = cortex_cfg.get("model") or self.tiers.get(tier) or self.tiers.get("top")
        effort = cortex_cfg.get("effort") or ""
        return self._run_claude_cortex(
            spec, model, prompt, cwd=run_cwd, resume_sid=resume_sid,
            timeout=timeout, effort=effort, max_tokens=max_tokens)

    def _run(self, spec: dict, model: str, prompt: str) -> str:
        kind = spec.get("kind")
        if kind == "claude_cli":
            return self._run_claude_cli(spec, model, prompt)
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
        effort = spec.get("effort")
        cmd = [_claude_bin(), "--output-format", "stream-json",
               "--input-format", "stream-json", "--verbose",
               "--model", model, *_ISOLATION]
        if effort:
            cmd.extend(["--effort", effort])
        env = {**os.environ, "MARROW_PIPELINE": "1"}
        raw = self._stream_subprocess(cmd, prompt, timeout, env)
        result = self._parse_claude(raw, "stream-json")
        self._log_usage(self._extract_usage(raw, "stream-json"), model, "stream-json")
        return result

    def _run_claude_cortex(self, spec: dict, model: str, prompt: str, *,
                            cwd: str, resume_sid: str | None,
                            timeout: float | None = None,
                            effort: str = "",
                            max_tokens: int | None = None) -> dict:
        """Stream-json runner with NO isolation flags (cortex full-env, C3).
        Mirrors _run_claude_stream's spawn/timeout/kill contract exactly —
        only the isolation flags, env var, cwd, --resume, and --effort differ.
        When max_tokens>0, accumulates per-wake usage mid-stream (deduped by
        requestId) and terminates cleanly when the current turn's window
        size breaches the cap (capped=True). Env-driven stream-event timing
        is attached best-effort for wake-latency diagnosis."""
        timeout = timeout if timeout is not None else spec.get("timeout_s", 600)
        cmd = [_claude_bin(), "--output-format", "stream-json",
               "--input-format", "stream-json", "--verbose", "--model", model,
               "--permission-mode", "bypassPermissions"]
        if effort:
            cmd.extend(["--effort", effort])
        if resume_sid:
            cmd.extend(["--resume", resume_sid])
        env = {**os.environ, "MARROW_CORTEX": "1"}
        on_event = _cortex_stream_timer()
        cap_active = bool(max_tokens and max_tokens > 0)
        extra: dict = {}
        if on_event is not None:
            extra["on_event"] = on_event
        sink = None
        if cap_active:
            sink = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0,
                    "window": 0, "capped": False, "by_request": {},
                    "has_usage": False}
            extra["max_tokens"] = max_tokens
            extra["usage_sink"] = sink
        raw = self._stream_subprocess(cmd, prompt, timeout, env, cwd=cwd, **extra)
        if sink is not None and sink["capped"]:
            self._log_usage(self._sink_usage(sink), model, "stream-json",
                             window=sink["window"])
            self._log_cortex_cap(sink, max_tokens, model)
            return {"text": "", "session_id": None, "capped": True,
                    "total_tokens": sink["window"]}
        text = self._parse_claude(raw, "stream-json")
        session_id = self._extract_session_id(raw)
        if sink is not None:
            if sink["has_usage"]:
                self._log_usage(self._sink_usage(sink), model, "stream-json",
                                 window=sink["window"])
            else:
                self._log_usage(self._extract_usage(raw, "stream-json"),
                                 model, "stream-json")
            return {"text": text, "session_id": session_id,
                    "total_tokens": sink["window"]}
        self._log_usage(self._extract_usage(raw, "stream-json"), model, "stream-json")
        return {"text": text, "session_id": session_id}

    @staticmethod
    def _add_event_usage(sink: dict, ev: dict) -> None:
        """Fold one stream-json event's turn usage into the running per-wake
        sink. Only assistant events carry message.usage; the trailing result
        event and tool-result events add 0 (no double count).

        A single API turn streams as MULTIPLE assistant lines (thinking
        block, tool_use block, text block, ...) and every line repeats the
        SAME usage under the SAME top-level `requestId` — summing them
        naively over-counts real consumption ~Nx (live-confirmed 07-04).
        Dedupe by requestId: a repeat requestId replaces (not adds to) its
        prior contribution to the cumulative fields — "last-seen wins"
        (the repeats are identical in practice, so this is a no-op delta,
        but stays correct if they ever aren't). Events without a requestId
        (only expected from synthetic/test input) are never deduped.

        `in/out/cache_read/cache_write` are the cumulative deduped sums —
        true consumption across the wake so far, used for the llm_call_cost
        audit line. `window` is the CURRENT turn's context size
        (input+cache_read+cache_creation), NOT cumulative — this is what the
        per-wake cap compares against (Decided 07-04: matches the statusline
        "total" figure the caller reasons with)."""
        msg = ev.get("message")
        usage = msg.get("usage") if isinstance(msg, dict) else None
        if not isinstance(usage, dict):
            return
        sink["has_usage"] = True
        i = usage.get("input_tokens") or 0
        o = usage.get("output_tokens") or 0
        cr = usage.get("cache_read_input_tokens") or 0
        cw = usage.get("cache_creation_input_tokens") or 0
        req_id = ev.get("requestId")
        prior = sink["by_request"].get(req_id) if req_id is not None else None
        if prior is not None:
            pi, po, pcr, pcw = prior
            sink["in"] += i - pi
            sink["out"] += o - po
            sink["cache_read"] += cr - pcr
            sink["cache_write"] += cw - pcw
        else:
            sink["in"] += i
            sink["out"] += o
            sink["cache_read"] += cr
            sink["cache_write"] += cw
        if req_id is not None:
            sink["by_request"][req_id] = (i, o, cr, cw)
        sink["window"] = i + cr + cw

    @staticmethod
    def _sink_usage(sink: dict) -> dict:
        return {"input_tokens": sink["in"], "output_tokens": sink["out"],
                "cache_read_input_tokens": sink["cache_read"],
                "cache_creation_input_tokens": sink["cache_write"]}

    def _log_cortex_cap(self, sink: dict, cap: int, model: str) -> None:
        """One audit line marking a per-wake token-cap breach. Reports the
        breaching turn's window size (input+cache_read+cache_creation — the
        figure compared against cap) alongside the deduped cumulative usage
        fields (true consumption across the wake so far) so a breach is
        auditable without ambiguity about what tripped it. Best-effort."""
        try:
            conn = storage.connect()
            with conn:
                conn.execute(
                    "INSERT INTO audit_log (target_table, action, summary)"
                    " VALUES (?, ?, ?)",
                    ("llm_usage", "llm_cortex_cap",
                     f"model={model} capped window={sink['window']} cap={cap} "
                     f"cumulative(in={sink['in']} out={sink['out']} "
                     f"cache_read={sink['cache_read']} cache_write={sink['cache_write']})"),
                )
            conn.close()
        except Exception:
            pass

    @staticmethod
    def _stream_subprocess(cmd: list[str], prompt: str, timeout: float,
                            env: dict, cwd: str | None = None,
                            on_event=None, max_tokens: int | None = None,
                            usage_sink: dict | None = None) -> str:
        """Spawn `cmd`, pipe one user message in via stdin, read stdout
        stream-json events until `result`. Process-group kill on timeout
        (SIGKILL) and on normal exit (SIGTERM->SIGKILL ladder) so claude's
        spawned descendants never leak. Returns the raw joined stdout lines.
        `on_event(ev, mono)` (optional) receives every parsed event plus a
        synthetic {"type":"__spawned__"} right after Popen for latency probes.
        `max_tokens`+`usage_sink` (optional) accumulate per-event usage
        (deduped by requestId, see `_add_event_usage`) and break the stream
        cleanly on breach (sink['capped']=True). Breach compares against
        sink['window'] — the CURRENT turn's context size
        (input+cache_read+cache_creation), not a cumulative sum across
        turns (Decided 07-04: matches the statusline "total" figure)."""
        msg = json.dumps({"type": "user", "message": {
            "role": "user", "content": prompt}})
        try:
            p = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, start_new_session=True,
                env=env, cwd=cwd)
        except OSError as e:
            raise LLMError(f"claude_cli spawn failed: {e}") from e
        try:
            pgid = os.getpgid(p.pid)
        except ProcessLookupError:
            pgid = p.pid
        stdout_pipe = p.stdout
        if on_event is not None:
            on_event({"type": "__spawned__"}, time.monotonic())

        _timed_out = [False]

        def _timeout_kill() -> None:
            _timed_out[0] = True
            _kill_group(pgid, signal.SIGKILL)
            try:
                stdout_pipe.close()
            except Exception:
                pass

        killer = threading.Timer(timeout, _timeout_kill)
        killer.start()
        lines: list[str] = []
        try:
            p.stdin.write(msg + "\n")
            p.stdin.flush()
            p.stdin.close()
            for line in stdout_pipe:
                line = line.strip()
                if not line:
                    continue
                lines.append(line)
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if on_event is not None:
                    on_event(ev, time.monotonic())
                if max_tokens is not None and usage_sink is not None:
                    LLMClient._add_event_usage(usage_sink, ev)
                    if usage_sink["window"] >= max_tokens:
                        usage_sink["capped"] = True
                        break
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
        if _timed_out[0]:
            raise LLMError(f"claude_cli timeout after {timeout}s")
        if not lines:
            err = (p.stderr.read() or "").strip()[:200]
            raise LLMError(f"claude_cli stream: no output ({err})")
        return "\n".join(lines)

    def _run_claude_p(self, spec: dict, model: str, prompt: str) -> str:
        effort = spec.get("effort")
        cmd = [_claude_bin(), "-p", prompt, "--model", model,
               *_ISOLATION, "--output-format", "json"]
        if effort:
            cmd.extend(["--effort", effort])
        timeout = spec.get("timeout_s", 120)
        env = {**os.environ, "MARROW_PIPELINE": "1"}
        try:
            p = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True, env=env)
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
    def _extract_session_id(out: str) -> str | None:
        """Pull session_id off the final stream-json result record (verified
        live: claude --output-format stream-json always carries it). Returns
        None on any parse failure — caller (cortex) treats that as fresh."""
        try:
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
            sid = rec.get("session_id")
            return str(sid) if sid else None
        except Exception:
            return None

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

    def _log_usage(self, usage: dict | None, model: str, fmt: str,
                   window: int | None = None) -> None:
        """Write a cost-monitor row to audit_log. Best-effort: never raises.
        `window` (optional) is the final per-wake context size (cortex cap
        path only) appended to the summary alongside the usage breakdown."""
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
            if window is not None:
                summary += f" window={window}"
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
