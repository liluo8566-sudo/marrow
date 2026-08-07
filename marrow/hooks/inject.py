"""UserPromptSubmit turn_inject: kickout context, usage threshold."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from .. import config, cortex_bridge, replay, storage
from ._shared import _read_input
from .state import _outbound_notes

def _in_time_window(now_min: int, start: str, end: str) -> bool:
    """Minute-of-day membership; wraps past midnight when end <= start."""
    def _to_min(hhmm: str) -> int:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    s, e = _to_min(start), _to_min(end)
    if s <= e:
        return s <= now_min < e
    return now_min >= s or now_min < e


def _kickout_context(channel: str, now: datetime, transcript_path: str | None = None) -> str:
    """B8 anti-late-night deterministic nudge, config-first ([kickout] in
    config.toml — see config.default.toml for the live windows/text). Cortex
    is immune (env marker OR a manually registered resident window)."""
    if cortex_bridge.is_cortex_session(transcript_path):
        return ""
    kc = config.load().get("kickout", {}) or {}
    if not kc.get("enabled", True):
        return ""
    now_min = now.hour * 60 + now.minute
    if channel == "cli":
        if _in_time_window(now_min, kc.get("cli_wind_down_start", "21:30"),
                            kc.get("cli_wind_down_end", "22:00")):
            return kc.get("cli_wind_down_text", "")
        if _in_time_window(now_min, kc.get("cli_leave_start", "22:00"),
                            kc.get("cli_leave_end", "06:00")):
            return kc.get("cli_leave_text", "")
    elif channel in ("wx", "tg"):
        if _in_time_window(now_min, kc.get("im_quiet_start", "23:00"),
                            kc.get("im_quiet_end", "06:00")):
            return kc.get("im_quiet_text", "")
    return ""


def _window_tokens_from_transcript(tpath: str) -> int:
    """Context-window occupancy = the last assistant message's usage totals
    (input + cache read + cache creation + output) in the session jsonl. Mirrors
    cortex.transcript.window_tokens. 0 on any missing/unreadable transcript."""
    if not tpath:
        return 0
    try:
        lines = open(tpath, encoding="utf-8").read().splitlines()
    except OSError:
        return 0
    total = 0
    for line in lines:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        msg = o.get("message")
        u = msg.get("usage") if isinstance(msg, dict) else None
        if u:
            total = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                     + u.get("cache_creation_input_tokens", 0) + u.get("output_tokens", 0))
    return total


def _usage_threshold_context(sid: str, tpath: str) -> str:
    """In-window token threshold line (all sessions). `main` = window occupancy
    (last assistant turn's usage totals — same metric as statusline `total` and
    the rotate/fuse thresholds, NOT cumulative net-spend); `agent` = cumulative
    subagent_tokens. Fires once `main` crosses threshold_start, then
    again every threshold_step above the last-injected watermark. Watermark
    tracked per session under state/. Empty below the first threshold.

    Tier/watermark math uses `main_occ` alone — the threshold is a
    window-rotation signal, and agent tokens don't occupy the main window, so
    they must not drive triggering. `agent_net` appears only in the rendered
    line."""
    if not sid or not tpath:
        return ""
    try:
        from .. import usage
        cu = config.load().get("cortex_usage", {}) or {}
        start = int(cu.get("threshold_start", 100_000) or 0)
        step = int(cu.get("threshold_step", 50_000) or 0)
        if start <= 0 or step <= 0:
            return ""
        main_occ = _window_tokens_from_transcript(tpath)
        agent_net = usage.agent_tokens_from_transcript(tpath)
        if main_occ < start:
            return ""
        # Current tier = highest crossed threshold (start + k*step).
        tier = start + ((main_occ - start) // step) * step
        state_dir = config.DATA_DIR / "state" / "usage_watermark"
        state_dir.mkdir(parents=True, exist_ok=True)
        state_file = state_dir / sid
        last = 0
        try:
            last = int(state_file.read_text().strip())
        except (OSError, ValueError):
            last = 0
        if tier <= last:
            return ""
        line = usage.threshold_line(main_occ, agent_net)
        if not line:
            return ""
        state_file.write_text(str(tier))
        return line
    except Exception:
        return ""


def _vitals_fragment(sid: str) -> str:
    """Phone-vitals one-liner for per-turn context injection (config-gated).

    Reads cfg from [turn_inject].vitals_file. Returns "" when off, on read
    errors, or when the throttle gate blocks emission. On emit, writes
    state/vitals_inject/<sid> JSON so subsequent calls can gate correctly.
    """
    try:
        ti = config.load().get("turn_inject", {}) or {}
        vf = (ti.get("vitals_file") or "").strip()
        if not vf:
            return ""

        import math
        from pathlib import Path as _Path

        vpath = _Path(vf).expanduser()
        try:
            raw = json.loads(vpath.read_text(encoding="utf-8"))
        except Exception:
            return ""

        # Strip stray leading/trailing spaces from keys (some producers add them).
        snap = {k.strip(): v for k, v in raw.items()}

        interval_min = int(ti.get("vitals_interval_min", 60) or 60)
        stale_min = int(ti.get("vitals_stale_min", 90) or 90)
        zones = ti.get("vitals_zones") or []

        # Parse timestamp.
        ts_str = snap.get("ts", "")
        snap_dt: datetime | None = None
        try:
            snap_dt = datetime.fromisoformat(ts_str)
        except Exception:
            pass

        age_s: float = float("inf")
        if snap_dt is not None:
            age_s = (datetime.now(timezone.utc) - snap_dt.astimezone(timezone.utc)).total_seconds()

        stale = age_s > stale_min * 60

        # Resolve zone label.
        lat_s = snap.get("lat", "")
        lon_s = snap.get("lon", "")
        zone_label: str = ""
        try:
            lat = float(lat_s)
            lon = float(lon_s)
            best_dist = float("inf")
            for z in zones:
                zlat = float(z.get("lat", 0))
                zlon = float(z.get("lon", 0))
                r = float(z.get("radius_m", 300))
                # Equirectangular approx in metres.
                dlat = math.radians(lat - zlat) * 6_371_000
                dlon = math.radians(lon - zlon) * 6_371_000 * math.cos(math.radians(zlat))
                dist = math.sqrt(dlat ** 2 + dlon ** 2)
                if dist <= r and dist < best_dist:
                    best_dist = dist
                    zone_label = str(z.get("name", ""))
            if not zone_label:
                zone_label = f"外面({lat:.4f},{lon:.4f})"
        except Exception:
            zone_label = ""

        # Throttle state.
        state_dir = config.DATA_DIR / "state" / "vitals_inject"
        state_dir.mkdir(parents=True, exist_ok=True)
        state_file = state_dir / sid

        now_epoch = datetime.now(timezone.utc).timestamp()
        last_ts: float = 0.0
        last_zone: str = ""
        try:
            st = json.loads(state_file.read_text(encoding="utf-8"))
            last_ts = float(st.get("ts", 0))
            last_zone = str(st.get("zone", ""))
        except Exception:
            pass

        first_turn = last_ts == 0.0
        zone_changed = (zone_label != last_zone) and not stale
        interval_elapsed = (now_epoch - last_ts) >= interval_min * 60

        if not first_turn and not zone_changed and not interval_elapsed:
            return ""

        # Build the line.
        batt = snap.get("battery_pct", "")
        temp = snap.get("temperature", "")
        weather = snap.get("weather", "")
        steps = snap.get("steps_today", "")

        if stale:
            # Stale warning line.
            age_total = int(age_s)
            h, rem = divmod(age_total, 3600)
            m = rem // 60
            age_str = (f"{h}h{m}m" if h else f"{m}m") if (h or m) else f"{age_total}s"
            local_time = ""
            if snap_dt is not None:
                tz_local = config.get_tz()
                local_time = snap_dt.astimezone(tz_local).strftime("%H:%M")
            parts = [f"📍 ⚠️ 手机{age_str}没上报 · 最后: {zone_label}"]
            if batt:
                parts.append(f"🔋{batt}%")
            if local_time:
                parts.append(f"({local_time})")
            line = " ".join(parts)
        else:
            segments = [f"📍 {zone_label}"]
            if batt:
                segments.append(f"🔋{batt}%")
            if temp or weather:
                segments.append(f"{temp} {weather}".strip())
            if steps:
                segments.append(f"今日{steps}步")
            line = " · ".join(segments)

        # Write state.
        try:
            state_file.write_text(
                json.dumps({"ts": now_epoch, "zone": zone_label}),
                encoding="utf-8",
            )
        except Exception:
            pass

        return line
    except Exception:
        return ""


def turn_inject() -> int:
    """Inject current time + delta since last reply, plus the B8 kickout
    nudge (config [kickout]) and the throttled presence line (config
    [presence]).

    WX bridge injects its own time via system prompt — skip the time+delta
    stamp when MARROW_CHANNEL=wx, but the kickout nudge still applies there.
    CLI and TG both need the time stamp.
    """
    channel = (os.environ.get("MARROW_CHANNEL") or "").strip() or "cli"

    inp = _read_input()
    tpath = (inp.get("transcript_path") or "")
    if "/tasks/" in tpath:
        return 0

    sid = (inp.get("session_id") or "").strip()
    if not sid:
        return 0

    tz = config.get_tz()
    now = datetime.now(timezone.utc).astimezone(tz)
    kickout_ctx = _kickout_context(channel, now, tpath)
    # The ONLY replay outlet for a window session — the wakeup note carries none.
    def _replay_fragment() -> str:
        return replay.context(sid, channel, transcript_path=tpath)

    def _sched_fragment() -> str:
        try:
            from .. import schedule as _sched
            inj = _sched.check_and_inject(sid)
            return f"\n\n{inj}" if inj else ""
        except Exception:
            return ""

    def _tl_fragment() -> str:
        try:
            from .. import tl_sync as _tls
            conn = storage.connect(config.db_path())
            try:
                frag = _tls.render_update(conn, sid)
            finally:
                conn.close()
            return f"\n\n{frag}" if frag else ""
        except Exception:
            return ""

    def _presence_fragment() -> str:
        try:
            from .. import presence as _presence
            frag = _presence.render(sid)
            return f"\n\n{frag}" if frag else ""
        except Exception:
            return ""

    if channel == "wx":
        # WX bridge injects its own time — skip the time stamp only; the
        # schedule + tl fragments and kickout nudge still apply.
        wx_sched = _sched_fragment()
        wx_tl = _tl_fragment()
        wx_presence = _presence_fragment()
        wx_kick = f"\n\n{kickout_ctx}" if kickout_ctx else ""
        wx_replay = _replay_fragment()
        wx_replay = f"\n\n{wx_replay}" if wx_replay else ""
        wx_own = _outbound_notes(sid, channel)
        wx_own = f"\n\n{wx_own}" if wx_own else ""
        vit = _vitals_fragment(sid)
        vit_full = f"\n\n{vit}" if vit else ""
        wx_ctx = f"{vit_full}{wx_sched}{wx_tl}{wx_presence}{wx_kick}{wx_replay}{wx_own}".strip()
        if wx_ctx:
            json.dump(
                {"hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": wx_ctx,
                }},
                sys.stdout,
            )
        return 0

    now_str = now.strftime("%Y-%m-%d %a %H:%M")
    now_epoch = int(now.timestamp())

    state_dir = config.DATA_DIR / "state" / "turn_delta"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / sid

    delta = ""
    try:
        if state_file.exists():
            last = int(state_file.read_text().strip())
            d = now_epoch - last
            if d < 60:
                delta = f" · +{d}s since last reply"
            elif d < 3600:
                delta = f" · +{d // 60}m since last reply"
            else:
                delta = f" · +{d // 3600}h{(d % 3600) // 60}m since last reply"
    except Exception:
        pass

    try:
        state_file.write_text(str(now_epoch))
    except Exception:
        pass

    sched_ctx = _sched_fragment()
    tl_ctx = _tl_fragment()
    presence_ctx = _presence_fragment()

    # Absorbed global turn-inject: per-turn care directive (config-lives).
    care_ctx = ""
    try:
        care = (config.load().get("turn_inject", {}) or {}).get("care_text", "")
        care = (care or "").strip()
        if care:
            care_ctx = f"\n\n{care}"
    except Exception:
        pass

    kickout_full = f"\n\n{kickout_ctx}" if kickout_ctx else ""
    show_ctx = (cortex_bridge._cortex_show_context(tpath, inp.get("prompt"))
                if cortex_bridge.enabled() else "")
    show_full = f"\n\n{show_ctx}" if show_ctx else ""
    usage_ctx = _usage_threshold_context(sid, tpath)
    usage_full = f"\n\n{usage_ctx}" if usage_ctx else ""
    replay_ctx = _replay_fragment()
    replay_full = f"\n\n{replay_ctx}" if replay_ctx else ""
    own_ctx = _outbound_notes(sid, channel)
    own_full = f"\n\n{own_ctx}" if own_ctx else ""
    vit = _vitals_fragment(sid)
    vit_full = f"\n\n{vit}" if vit else ""
    ctx = (f"# Context — {now_str}{delta}{vit_full}{sched_ctx}{tl_ctx}{presence_ctx}{care_ctx}"
           f"{kickout_full}{show_full}{usage_full}{replay_full}{own_full}")
    json.dump(
        {"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": ctx,
        }},
        sys.stdout,
    )
    return 0
