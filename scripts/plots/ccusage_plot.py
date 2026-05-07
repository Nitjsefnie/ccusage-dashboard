#!/usr/bin/env python3
"""Plot Claude Code usage data by reading local conversation logs directly."""

__version__ = "1.3.0"

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib.dates as mdates
import matplotlib.gridspec as gridspec
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

# -- Theme colors --
BG_DARK = "#1a1a2e"
BG_AXES = "#16213e"
BORDER = "#2a2a4a"
TEXT = "#e0e0e0"
TEXT_DIM = "#8888aa"
GRID = "#2a2a4a"

COLORS = {
    "inputTokens": "#00d4aa",
    "outputTokens": "#ff8c42",
    "cacheCreateTokens": "#aa55ff",
    "cacheReadTokens": "#ff3366",
    "totalTokens": "#00d4ff",
    "costUSD": "#ffdd00",
}

PROJECTS_DIR = Path("/tmp/analyst.BCYKic3p/r2")

# -- Burn rate constants --
COLOR_LIMIT_HIT = "#ff3366"
COLOR_WINDOW = "#ffffff"

BURN_TOKEN_STYLES = {
    "output":       {"color": "#ee4444", "lw": 1.5, "alpha": 0.85, "label": "Output"},
    "input":        {"color": "#44dd66", "lw": 1.5, "alpha": 0.85, "label": "Input"},
    "cache_create": {"color": "#dd66aa", "lw": 1.5, "alpha": 0.85, "label": "Cache Create"},
    "cache_read":   {"color": "#44bbbb", "lw": 1.5, "alpha": 0.85, "label": "Cache Read"},
}

MODEL_COLORS = {
    "opus-4-7": "#ff2222",
    "opus-4-6": "#ff8800",
    "opus-4-5": "#ffdd00",
    "sonnet-4-6": "#00bbff",
    "sonnet-4-5": "#8866ff",
    "haiku-4-5": "#88cc44",
}

WINDOW_GAP_S = 5 * 3600
SESSION_GAP_S = 1800
EMA_ALPHA = 0.15
BUCKET_MINUTES = 30
BUCKET_THRESHOLD = 20

# Chart definitions: (title, key, is_currency)
CHARTS = [
    ("Input Tokens", "inputTokens", False),
    ("Output Tokens", "outputTokens", False),
    ("Cache Create Tokens", "cacheCreateTokens", False),
    ("Cache Read Tokens", "cacheReadTokens", False),
    ("Total Tokens", "totalTokens", False),
    ("Cost (USD)", "costUSD", True),
]


def human_format(value, is_currency=False):
    prefix = "$" if is_currency else ""
    for suffix, threshold, fmt in [
        ("B", 1e9, ".2f"),
        ("M", 1e6, ".2f"),
        ("K", 1e3, ".1f"),
    ]:
        if abs(value) >= threshold:
            formatted = f"{value / threshold:{fmt}}"
            if "." in formatted:
                formatted = formatted.rstrip("0").rstrip(".")
            return f"{prefix}{formatted}{suffix}"
    if is_currency:
        return f"${value:,.2f}"
    return f"{int(value)}"


def make_formatter(is_currency):
    return ticker.FuncFormatter(lambda v, _: human_format(v, is_currency))


def parse_period(period_str):
    m = re.fullmatch(r"(\d+)\s*([hdwm])", period_str.strip().lower())
    if not m:
        print(
            f"Error: invalid period '{period_str}'. Use e.g. 6h, 3d, 1w, 2m",
            file=sys.stderr,
        )
        sys.exit(1)
    value, unit = int(m.group(1)), m.group(2)
    if unit == "h":
        return timedelta(hours=value)
    elif unit == "d":
        return timedelta(days=value)
    elif unit == "w":
        return timedelta(weeks=value)
    elif unit == "m":
        return timedelta(days=value * 30)


def parse_datetime(dt_str, tz=None):
    """Parse 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' into a timezone-aware datetime."""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(dt_str.strip(), fmt)
            if tz:
                return dt.replace(tzinfo=tz)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    print(
        f"Error: invalid date '{dt_str}'. Use YYYY-MM-DD or 'YYYY-MM-DD HH:MM'",
        file=sys.stderr,
    )
    sys.exit(1)


# Approximate cost per token by model (USD)
# Tuple: (input, output, cache_create_5m, cache_create_1h, cache_read)
# 5-minute ephemeral cache writes are billed at 1.25x base input;
# 1-hour ephemeral cache writes are billed at 2x base input.
MODEL_PRICING = {
    "claude-opus-4-7":           (5 / 1e6, 25 / 1e6, 6.25 / 1e6, 10 / 1e6,  0.5 / 1e6),
    "claude-opus-4-6":           (5 / 1e6, 25 / 1e6, 6.25 / 1e6, 10 / 1e6,  0.5 / 1e6),
    "claude-opus-4-5-20251101":  (5 / 1e6, 25 / 1e6, 6.25 / 1e6, 10 / 1e6,  0.5 / 1e6),
    "claude-sonnet-4-6":         (3 / 1e6, 15 / 1e6, 3.75 / 1e6, 6 / 1e6,   0.3 / 1e6),
    "claude-sonnet-4-5-20250929":(3 / 1e6, 15 / 1e6, 3.75 / 1e6, 6 / 1e6,   0.3 / 1e6),
    "claude-haiku-4-5-20251001": (1 / 1e6, 5 / 1e6,  1.25 / 1e6, 2 / 1e6,   0.1 / 1e6),
}
# (input, output, cache_create_5m, cache_create_1h, cache_read)
DEFAULT_PRICING = (3 / 1e6, 15 / 1e6, 3.75 / 1e6, 6 / 1e6, 0.3 / 1e6)


def estimate_cost(model, input_t, output_t,
                  cache_create_5m_t, cache_create_1h_t, cache_read_t):
    """Cost split by cache TTL.

    The Anthropic API reports ephemeral cache writes in two buckets
    (5-minute and 1-hour) under `usage.cache_creation`. They are billed
    at different rates, so we keep them separate. Older callers that pass
    a single combined `cache_create` value should split it themselves;
    a back-compat shim is provided as `estimate_cost_legacy` below.
    """
    pricing = DEFAULT_PRICING
    for prefix, p in MODEL_PRICING.items():
        if model and model.startswith(prefix.rsplit("-", 1)[0]):
            pricing = p
            break
    pi, po, pcc5, pcc1h, pcr = pricing
    return (
        input_t * pi
        + output_t * po
        + cache_create_5m_t * pcc5
        + cache_create_1h_t * pcc1h
        + cache_read_t * pcr
    )


def estimate_cost_legacy(model, input_t, output_t, cache_create_t, cache_read_t):
    """Back-compat: assume all cache_create is 5-minute ephemeral."""
    return estimate_cost(model, input_t, output_t, cache_create_t, 0, cache_read_t)


def load_events(cutoff=None, end=None):
    """Read conversation JSONL files and extract assistant message usage data."""
    events = []
    if not PROJECTS_DIR.exists():
        print(f"Error: projects dir not found: {PROJECTS_DIR}", file=sys.stderr)
        sys.exit(1)

    jsonl_files = list(PROJECTS_DIR.rglob("*.jsonl"))
    print(f"Scanning {len(jsonl_files)} conversation files...", file=sys.stderr)

    # Cross-file dedup by record uuid. The SAME API call can appear in
    # multiple jsonls — most commonly a session's main `<uuid>.jsonl` plus
    # its `data/subagents/agent-*.jsonl` companion — with identical inner
    # `uuid` but different wrappers. Without this, subagent tokens/cost
    # double-count. Records without a `uuid` (legacy data) pass through.
    seen_uuids: set[str] = set()

    for path in jsonl_files:
        # Per-file dedup by requestId. Claude Code splits one logical API
        # response into N JSONL records (thinking + text + tool_use blocks,
        # streaming chunks). All N share the same `requestId`; input /
        # cache_create / cache_read are bit-identical across them; only
        # `output_tokens` may grow as streaming progresses (intermediate
        # records report partial counts, the final carries the aggregate).
        # Take max per usage field — correct for both the identical fields
        # and the streaming-output case.
        seen_request_events: dict[str, dict] = {}
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    if obj.get("type") != "assistant":
                        continue

                    rec_uuid = obj.get("uuid")
                    if rec_uuid:
                        if rec_uuid in seen_uuids:
                            continue
                        seen_uuids.add(rec_uuid)

                    ts_raw = obj.get("timestamp")
                    if not ts_raw:
                        continue
                    # timestamp can be ISO string or unix millis
                    if isinstance(ts_raw, (int, float)):
                        ts = datetime.fromtimestamp(ts_raw / 1000, tz=timezone.utc)
                    else:
                        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))

                    if cutoff and ts < cutoff:
                        continue
                    if end and ts > end:
                        continue

                    msg = obj.get("message", {})
                    usage = msg.get("usage", {})
                    if not usage:
                        continue

                    model = msg.get("model", "unknown")
                    input_t = usage.get("input_tokens", 0) or 0
                    output_t = usage.get("output_tokens", 0) or 0
                    cache_create = usage.get("cache_creation_input_tokens", 0) or 0
                    cache_read = usage.get("cache_read_input_tokens", 0) or 0
                    # Split cache_create into 5-minute / 1-hour ephemeral buckets.
                    # The API reports them separately under `usage.cache_creation`.
                    # Any leftover (cache_create - 5m - 1h) is treated as 5m by
                    # default — that's the historical/implicit TTL.
                    cc_detail = usage.get("cache_creation") or {}
                    cache_create_5m = cc_detail.get("ephemeral_5m_input_tokens", 0) or 0
                    cache_create_1h = cc_detail.get("ephemeral_1h_input_tokens", 0) or 0
                    leftover = max(0, cache_create - cache_create_5m - cache_create_1h)
                    cache_create_5m += leftover

                    req_id = obj.get("requestId", "")
                    if req_id and req_id in seen_request_events:
                        ev = seen_request_events[req_id]
                        ev["inputTokens"] = max(ev["inputTokens"], input_t)
                        ev["outputTokens"] = max(ev["outputTokens"], output_t)
                        ev["cacheCreateTokens"] = max(ev["cacheCreateTokens"], cache_create)
                        ev["cacheCreate5mTokens"] = max(ev["cacheCreate5mTokens"], cache_create_5m)
                        ev["cacheCreate1hTokens"] = max(ev["cacheCreate1hTokens"], cache_create_1h)
                        ev["cacheReadTokens"] = max(ev["cacheReadTokens"], cache_read)
                        ev["totalTokens"] = (
                            ev["inputTokens"] + ev["outputTokens"]
                            + ev["cacheCreateTokens"] + ev["cacheReadTokens"]
                        )
                        ev["costUSD"] = estimate_cost(
                            ev["model"],
                            ev["inputTokens"], ev["outputTokens"],
                            ev["cacheCreate5mTokens"], ev["cacheCreate1hTokens"],
                            ev["cacheReadTokens"],
                        )
                        continue

                    ev = {
                        "timestamp": ts,
                        "model": model,
                        "inputTokens": input_t,
                        "outputTokens": output_t,
                        "cacheCreateTokens": cache_create,
                        "cacheCreate5mTokens": cache_create_5m,
                        "cacheCreate1hTokens": cache_create_1h,
                        "cacheReadTokens": cache_read,
                        "totalTokens": input_t + output_t + cache_create + cache_read,
                        "costUSD": estimate_cost(
                            model, input_t, output_t,
                            cache_create_5m, cache_create_1h, cache_read,
                        ),
                    }
                    if req_id:
                        seen_request_events[req_id] = ev
                    events.append(ev)
        except (json.JSONDecodeError, KeyError, OSError):
            continue

    events.sort(key=lambda e: e["timestamp"])
    return events


def apply_theme():
    plt.rcParams.update(
        {
            "figure.facecolor": BG_DARK,
            "axes.facecolor": BG_AXES,
            "axes.edgecolor": BORDER,
            "text.color": TEXT,
            "xtick.color": TEXT_DIM,
            "ytick.color": TEXT_DIM,
            "grid.color": GRID,
            "grid.alpha": 0.4,
            "font.family": "monospace",
        }
    )


def style_axes(ax):
    ax.set_facecolor(BG_AXES)
    for spine in ax.spines.values():
        spine.set_color(BORDER)
        spine.set_linewidth(1.5)
    ax.tick_params(colors=TEXT_DIM, labelsize=9)


TZ_ALIASES = {
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "PT": "America/Los_Angeles",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "MT": "America/Denver",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "CT": "America/Chicago",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "ET": "America/New_York",
    "GMT": "UTC",
    "UTC": "UTC",
    "BST": "Europe/London",
    "CET": "Europe/Berlin",
    "CEST": "Europe/Berlin",
    "IDT": "Asia/Jerusalem",
    "IST": "Asia/Kolkata",
    "JST": "Asia/Tokyo",
    "AEST": "Australia/Sydney",
    "AEDT": "Australia/Sydney",
}


def _check_tzdata():
    """Ensure timezone data is available (needed on Windows)."""
    try:
        ZoneInfo("UTC")
    except Exception:
        print(
            "Error: timezone database not found. On Windows, install it with:\n"
            "  pip install tzdata",
            file=sys.stderr,
        )
        sys.exit(1)


def resolve_tz(tz_str):
    """Resolve a timezone string (alias or IANA name) to a ZoneInfo object."""
    if tz_str is None:
        return None
    _check_tzdata()
    key = tz_str.upper()
    iana_key = TZ_ALIASES.get(key, tz_str)
    try:
        return ZoneInfo(iana_key)
    except KeyError:
        print(
            f"Error: unknown timezone '{tz_str}'. Use e.g. PST, EST, UTC, Asia/Tokyo",
            file=sys.stderr,
        )
        sys.exit(1)

def _get_plan_from_credentials():
    """Fallback: read subscription type from .credentials.json (Windows)."""
    creds_path = Path.home() / ".claude" / ".credentials.json"
    if creds_path.exists():
        try:
            with open(creds_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                plan = None
                if "claudeAiOauth" in data:
                    plan = data["claudeAiOauth"].get("subscriptionType")
                if plan:
                    return str(plan).capitalize()
        except Exception:
            pass
    return None


def get_claude_info():
    """Get subscription type and version from the claude CLI, with credentials.json fallback."""
    plan = ""
    version = ""
    try:
        result = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        p = data.get("subscriptionType", "")
        if p:
            plan = str(p).capitalize()
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError):
        # CLI not available (common on Windows), fall back to credentials file
        creds_plan = _get_plan_from_credentials()
        if creds_plan:
            plan = creds_plan
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        version = result.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    return plan, version

HIGHLIGHT_COLOR = "#ffffff"
HIGHLIGHT_ALPHA = 0.06


def parse_highlight(highlight_str):
    """Parse '5-11' or '5:00-11:00' into (start_hour, end_hour) as floats."""
    m = re.fullmatch(
        r"(\d{1,2})(?::(\d{2}))?-(\d{1,2})(?::(\d{2}))?", highlight_str.strip()
    )
    if not m:
        print(
            f"Error: invalid highlight '{highlight_str}'. Use e.g. 5-11 or 5:00-11:30",
            file=sys.stderr,
        )
        sys.exit(1)
    sh = int(m.group(1)) + (int(m.group(2)) / 60 if m.group(2) else 0)
    eh = int(m.group(3)) + (int(m.group(4)) / 60 if m.group(4) else 0)
    return sh, eh


def add_highlight_bands(ax, timestamps, start_hour, end_hour, tz):
    """Add vertical shaded bands for each day's highlight window, clipped to current xlim."""
    if not timestamps:
        return
    display_tz = tz if tz else timezone.utc

    # Save current x-axis limits before adding spans
    xlim = ax.get_xlim()

    dates_seen = set()
    for ts in timestamps:
        dt = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(display_tz)
        dates_seen.add(dt.date())

    for d in sorted(dates_seen):
        band_start = datetime(
            d.year,
            d.month,
            d.day,
            int(start_hour),
            int((start_hour % 1) * 60),
            tzinfo=display_tz,
        )
        band_end = datetime(
            d.year,
            d.month,
            d.day,
            int(end_hour),
            int((end_hour % 1) * 60),
            tzinfo=display_tz,
        )
        ax.axvspan(
            band_start, band_end, alpha=HIGHLIGHT_ALPHA, color=HIGHLIGHT_COLOR, zorder=1
        )

    # Restore x-axis limits so highlight bands don't expand the view
    ax.set_xlim(xlim)


def short_model(model):
    return model.replace("claude-", "").split("-2")[0]


def build_sessions(events, session_gap_s=SESSION_GAP_S):
    if not events:
        return []
    chunks = []
    cur = [events[0]]
    for e in events[1:]:
        if (e["timestamp"] - cur[-1]["timestamp"]).total_seconds() > session_gap_s:
            chunks.append(cur)
            cur = [e]
        else:
            cur.append(e)
    chunks.append(cur)

    token_keys = ("input", "output", "cache_create", "cache_read")
    field_map = {
        "input": "inputTokens", "output": "outputTokens",
        "cache_create": "cacheCreateTokens", "cache_read": "cacheReadTokens",
    }
    result = []
    for s in chunks:
        if len(s) < 3:
            continue
        dur_h = max((s[-1]["timestamp"] - s[0]["timestamp"]).total_seconds(), 60) / 3600
        per_h = {}
        for key in token_keys:
            per_h[key] = sum(e[field_map[key]] for e in s) / dur_h

        models = defaultdict(int)
        for e in s:
            models[short_model(e["model"])] += 1
        primary = max(models, key=models.get)

        result.append({
            "start": s[0]["timestamp"],
            "end": s[-1]["timestamp"],
            "mid": s[0]["timestamp"] + (s[-1]["timestamp"] - s[0]["timestamp"]) / 2,
            "dur_h": dur_h,
            "reqs": len(s),
            "primary_model": primary,
            **{f"{k}_per_h": v for k, v in per_h.items()},
        })
    return result


def find_window_boundaries(events, window_gap_s=WINDOW_GAP_S):
    boundaries = []
    for i in range(1, len(events)):
        gap = (events[i]["timestamp"] - events[i - 1]["timestamp"]).total_seconds()
        if gap >= window_gap_s:
            boundaries.append(events[i]["timestamp"])
    return boundaries


def find_limit_hits(events):
    """Scan raw JSONL for rate limit error messages. Uses pre-loaded events' timestamps."""
    limit_hits = []
    # Cross-file dedup by record uuid; see load_events() for rationale.
    seen_uuids: set[str] = set()
    for path in PROJECTS_DIR.rglob("*.jsonl"):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    if obj.get("type") != "assistant" or not obj.get("isApiErrorMessage"):
                        continue
                    rec_uuid = obj.get("uuid")
                    if rec_uuid:
                        if rec_uuid in seen_uuids:
                            continue
                        seen_uuids.add(rec_uuid)
                    ts_raw = obj.get("timestamp")
                    if not ts_raw:
                        continue
                    if isinstance(ts_raw, (int, float)):
                        ts = datetime.fromtimestamp(ts_raw / 1000, tz=timezone.utc)
                    else:
                        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                    msg = obj.get("message", {})
                    for c in msg.get("content", []) if isinstance(msg.get("content"), list) else []:
                        if isinstance(c, dict) and c.get("type") == "text":
                            t = c.get("text", "").lower()
                            if "hit your limit" in t or "rate limit" in t:
                                limit_hits.append({"ts": ts, "text": c.get("text", "")})
        except (json.JSONDecodeError, KeyError, OSError):
            continue
    limit_hits.sort(key=lambda e: e["ts"])
    deduped = []
    for h in limit_hits:
        if not deduped or (h["ts"] - deduped[-1]["ts"]).total_seconds() > 60:
            deduped.append(h)
    return deduped


def build_buckets(events, sessions, bucket_min=BUCKET_MINUTES):
    field_map = {
        "input": "inputTokens", "output": "outputTokens",
        "cache_create": "cacheCreateTokens", "cache_read": "cacheReadTokens",
    }
    token_keys = ("input", "output", "cache_create", "cache_read")
    session_ranges = [(s["start"], s["end"]) for s in sessions]
    buckets = []
    for start, end in session_ranges:
        session_events = [e for e in events if start <= e["timestamp"] <= end]
        if len(session_events) < 3:
            continue
        bucket_s = bucket_min * 60
        t = start
        while t < end:
            t_end = min(t + timedelta(seconds=bucket_s), end)
            chunk = [e for e in session_events if t <= e["timestamp"] < t_end]
            if not chunk:
                t = t_end
                continue
            dur_h = max((t_end - t).total_seconds(), 60) / 3600
            bucket = {"mid": t + (t_end - t) / 2}
            for key in token_keys:
                bucket[f"{key}_per_h"] = sum(e[field_map[key]] for e in chunk) / dur_h
            buckets.append(bucket)
            t = t_end
    buckets.sort(key=lambda b: b["mid"])
    return buckets


def compute_ema(values, alpha=EMA_ALPHA):
    result = [values[0]]
    for v in values[1:]:
        result.append(alpha * v + (1 - alpha) * result[-1])
    return result


def detect_shifts(ema_values, sessions, lookback=10, threshold=2.0):
    shifts = []
    for i in range(lookback, len(ema_values)):
        baseline = sum(ema_values[i - lookback:i]) / lookback
        if baseline <= 0:
            continue
        ratio = ema_values[i] / baseline
        if ratio >= threshold or ratio <= 1 / threshold:
            shifts.append({
                "ts": sessions[i]["start"],
                "ratio": ratio,
                "direction": "up" if ratio > 1 else "down",
            })
    clustered = []
    for s in shifts:
        if not clustered or (s["ts"] - clustered[-1]["ts"]).total_seconds() > 86400:
            clustered.append(s)
    return clustered


def plot_burn_rate(ax, events, sessions, window_boundaries, limit_hits,
                   view_start=None, view_end=None):
    """Render the session burn rate panel onto the given axes."""
    token_keys = ["output", "input", "cache_create", "cache_read"]

    visible = sessions
    if view_start or view_end:
        visible = [s for s in sessions
                   if (not view_start or s["end"] >= view_start)
                   and (not view_end or s["start"] <= view_end)]
    if not visible:
        ax.set_visible(False)
        return

    all_emas = {}
    for key in token_keys:
        rates = [s[f"{key}_per_h"] for s in sessions]
        all_emas[key] = compute_ema(rates)

    session_emas = {}
    for key in token_keys:
        session_emas[key] = {id(s): all_emas[key][i] for i, s in enumerate(sessions)}

    shifts = detect_shifts(all_emas["output"], sessions)

    display_alpha = max(EMA_ALPHA, 2.0 / (len(visible) + 1))
    display_emas = {}
    for key in token_keys:
        rates = [s[f"{key}_per_h"] for s in visible]
        display_emas[key] = compute_ema(rates, alpha=display_alpha)

    timestamps = [s["mid"] for s in visible]
    out_rates = [s["output_per_h"] for s in visible]
    colors = [MODEL_COLORS.get(s["primary_model"], "#888888") for s in visible]

    xlim_start = view_start or visible[0]["start"] - timedelta(hours=2)
    xlim_end = view_end or visible[-1]["end"] + timedelta(hours=2)
    span_h = (xlim_end - xlim_start).total_seconds() / 3600

    if span_h <= 4:
        rate_mult, rate_unit = 1 / 60, "min"
    else:
        rate_mult, rate_unit = 1, "hour"

    out_rates = [r * rate_mult for r in out_rates]
    for key in token_keys:
        display_emas[key] = [v * rate_mult for v in display_emas[key]]

    # Window boundary markers
    for wb in window_boundaries:
        if wb < xlim_start or wb > xlim_end:
            continue
        ax.axvline(wb, color=COLOR_WINDOW, alpha=0.12, linewidth=1, linestyle=":", zorder=1)

    # Session dots
    sizes = [min(max(s["dur_h"] * 60, 25), 250) for s in visible]
    ax.scatter(timestamps, out_rates, s=sizes, c=colors, alpha=0.5,
               edgecolors="white", linewidths=0.3, zorder=6)

    # EMA lines
    for key in token_keys:
        style = BURN_TOKEN_STYLES[key]
        ax.plot(timestamps, display_emas[key], color=style["color"],
                alpha=style["alpha"], linewidth=style["lw"], zorder=8,
                label=style["label"])

    # Intra-session bucket lines (narrow views)
    if len(visible) <= BUCKET_THRESHOLD and events:
        buckets = build_buckets(events, visible)
        if len(buckets) > len(visible):
            bucket_ts = [b["mid"] for b in buckets]
            for key in token_keys:
                raw = [b[f"{key}_per_h"] * rate_mult for b in buckets]
                smoothed = compute_ema(raw, alpha=0.3)
                style = BURN_TOKEN_STYLES[key]
                ax.plot(bucket_ts, smoothed, color=style["color"],
                        alpha=0.25, linewidth=0.8, zorder=5, linestyle="-")

    # Rate limit hits
    visible_hits = [h for h in limit_hits if xlim_start <= h["ts"] <= xlim_end]
    for hit in visible_hits:
        ax.axvline(hit["ts"], color=COLOR_LIMIT_HIT, alpha=0.7, linewidth=2, zorder=9)

    # Behavioral shifts
    visible_shifts = [s for s in shifts if xlim_start <= s["ts"] <= xlim_end]
    for shift in visible_shifts:
        for i, s in enumerate(visible):
            if abs((s["mid"] - shift["ts"]).total_seconds()) < 7200:
                y_pos = session_emas["output"][id(s)] * rate_mult
                if shift["direction"] == "up":
                    arrow, fg, bg, edge = "↑", "#ff6666", "#3a1a1a", "#ff6666"
                else:
                    arrow, fg, bg, edge = "↓", "#44ff88", "#1a3a2a", "#44ff88"
                ax.annotate(
                    f"{arrow} {shift['ratio']:.1f}x",
                    xy=(shift["ts"], y_pos),
                    xytext=(0, -25), textcoords="offset points",
                    fontsize=7, color=fg, ha="center", va="top",
                    bbox=dict(boxstyle="round,pad=0.2", facecolor=bg,
                              edgecolor=edge, alpha=0.8),
                    zorder=11,
                )
                break

    # Axes styling
    all_visible_rates = []
    for key in token_keys:
        all_visible_rates.extend(display_emas[key])
    all_visible_rates.extend(out_rates)
    ax.set_yscale("log")
    y_bottom = max(min(all_visible_rates) * 0.3, 1)
    y_top = max(all_visible_rates) * 3
    ax.set_ylim(bottom=y_bottom, top=y_top)
    ax.set_xlim(xlim_start, xlim_end)

    for spine in ax.spines.values():
        spine.set_color(BORDER)
        spine.set_linewidth(1.5)
    ax.tick_params(colors=TEXT_DIM, labelsize=9)
    ax.yaxis.set_major_formatter(ticker.FuncFormatter(
        lambda v, _: human_format(v, False)))
    ax.set_ylabel(f"Tokens / {rate_unit} (EMA)", fontsize=11, color=TEXT_DIM)
    ax.grid(True, alpha=0.2, color=GRID, axis="y")
    ax.grid(True, alpha=0.1, color=GRID, axis="x")

    fmt_tz = timezone.utc
    if span_h <= 24:
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=3))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=fmt_tz))
    elif span_h <= 72:
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=6))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d %H:%M", tz=fmt_tz))
    elif span_h <= 168:
        ax.xaxis.set_major_locator(mdates.DayLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d", tz=fmt_tz))
    elif span_h <= 1440:
        ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=0))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d", tz=fmt_tz))
    else:
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y", tz=fmt_tz))
    ax.tick_params(axis="x", rotation=0, labelsize=8)

    # Legend
    legend_handles = []
    for key in token_keys:
        style = BURN_TOKEN_STYLES[key]
        legend_handles.append(plt.Line2D([0], [0], color=style["color"],
                                          linewidth=style["lw"],
                                          alpha=style["alpha"],
                                          label=f"{style['label']} (EMA)"))
    legend_handles.append(plt.Line2D([0], [0], color=COLOR_WINDOW, alpha=0.3,
                                      linewidth=1, linestyle=":",
                                      label="Window start (5h+ gap)"))
    if visible_hits:
        legend_handles.append(plt.Line2D([0], [0], color=COLOR_LIMIT_HIT,
                                          linewidth=2, label="Rate limit hit"))
    for model in sorted(set(s["primary_model"] for s in visible)):
        c = MODEL_COLORS.get(model, "#888888")
        legend_handles.append(plt.Line2D([0], [0], marker="o", color="none",
                                          markerfacecolor=c, markeredgecolor="white",
                                          markeredgewidth=0.3, markersize=8,
                                          alpha=0.6, label=model))
    for dur_label, dur_h in [("30m", 0.5), ("1h", 1), ("4h", 4)]:
        sz = min(max(dur_h * 60, 25), 250)
        legend_handles.append(plt.Line2D([0], [0], marker="o", color="none",
                                          markerfacecolor="#888888",
                                          markeredgecolor="white",
                                          markeredgewidth=0.3,
                                          markersize=sz ** 0.5,
                                          alpha=0.4, label=dur_label))
    ax.legend(handles=legend_handles, loc="lower center",
              bbox_to_anchor=(0.5, 1.03), fontsize=7, ncol=6,
              facecolor=BG_AXES, edgecolor=BORDER, labelcolor=TEXT,
              framealpha=0.9)

    t0 = visible[0]["start"].strftime("%b %d")
    t1 = visible[-1]["end"].strftime("%b %d, %Y")
    total_reqs = sum(s["reqs"] for s in visible)
    n_windows = sum(1 for wb in window_boundaries if xlim_start <= wb <= xlim_end) + 1
    ax.set_title(
        f"Session Burn Rate  |  {t0} – {t1} UTC"
        f"  |  {len(visible)} sessions, {n_windows} windows, {total_reqs:,} requests",
        fontsize=13, fontweight="bold", color=TEXT, pad=70,
    )


def plot_timeline(events, period_str, output_path, tz=None, highlight=None):
    apply_theme()

    if tz:
        timestamps = [e["timestamp"].astimezone(tz) for e in events]
        tz_label = str(tz)
        # Shorten IANA names for display
        for alias, iana in TZ_ALIASES.items():
            if iana == str(tz):
                tz_label = alias
                break
    else:
        timestamps = [e["timestamp"] for e in events]
        tz_label = "UTC"

    fig = plt.figure(figsize=(18, 26))
    gs_top = gridspec.GridSpec(4, 2, figure=fig,
                               top=0.94, bottom=0.27, hspace=0.35, wspace=0.3)
    gs_burn = gridspec.GridSpec(1, 1, figure=fig,
                                top=0.21, bottom=0.03)
    axes = [fig.add_subplot(gs_top[r, c]) for r in range(4) for c in range(2)]
    ax_burn = fig.add_subplot(gs_burn[0])

    total_cost = sum(e["costUSD"] for e in events)
    total_reqs = len(events)

    # Actual date range from data
    display_tz = tz if tz else timezone.utc
    first_ts = (
        timestamps[0]
        if timestamps[0].tzinfo
        else timestamps[0].replace(tzinfo=display_tz)
    )
    last_ts = (
        timestamps[-1]
        if timestamps[-1].tzinfo
        else timestamps[-1].replace(tzinfo=display_tz)
    )
    date_range_str = f"{first_ts.strftime('%b %d %H:%M')} \u2013 {last_ts.strftime('%b %d %H:%M')} {tz_label}"

    # Get plan and version info
    plan_name, claude_version = get_claude_info()

    title_parts = ["Claude Code Usage"]
    if plan_name:
        title_parts.append(f"Plan: {plan_name}")
    if claude_version:
        title_parts.append(f"v{claude_version.split()[0]}")
    fig.suptitle(
        "  |  ".join(title_parts),
        fontsize=18, fontweight="bold", color="#ffffff", y=0.99,
    )
    subtitle_parts = [
        date_range_str,
        f"{total_reqs} API calls",
        f"${total_cost:.2f} total",
    ]
    if highlight:
        subtitle_parts.append(
            f"Highlight: {int(highlight[0])}:00\u2013{int(highlight[1])}:00"
        )
    fig.text(
        0.5,
        0.96,
        "  |  ".join(subtitle_parts),
        ha="center",
        fontsize=11,
        color=TEXT_DIM,
    )

    # Determine time span and bin size
    span_h = (
        (timestamps[-1] - timestamps[0]).total_seconds() / 3600
        if len(timestamps) > 1
        else 1
    )
    # Target ~60 bars regardless of time range
    TARGET_BARS = 120
    bin_seconds = max(60, span_h * 3600 / TARGET_BARS)
    # Snap to a clean interval
    clean_intervals = [
        (60, "per 1min"), (120, "per 2min"), (300, "per 5min"),
        (600, "per 10min"), (900, "per 15min"), (1800, "per 30min"),
        (3600, "per 1h"), (7200, "per 2h"), (14400, "per 4h"),
        (21600, "per 6h"), (43200, "per 12h"), (86400, "per 1d"),
        (604800, "per 1w"), (2592000, "per 30d"),
    ]
    bin_delta = timedelta(seconds=clean_intervals[-1][0])
    bin_label = clean_intervals[-1][1]
    for secs, label in clean_intervals:
        if secs >= bin_seconds:
            bin_delta = timedelta(seconds=secs)
            bin_label = label
            break

    fmt_tz = tz if tz else timezone.utc

    for idx, (title, key, is_currency) in enumerate(CHARTS):
        ax = axes[idx]
        style_axes(ax)

        values = [e[key] for e in events]
        color = COLORS[key]

        # Bin events into time segments
        bin_starts = []
        bin_totals = []
        bin_start = timestamps[0]
        bin_sum = 0
        ts_idx = 0
        while bin_start <= timestamps[-1]:
            bin_end = bin_start + bin_delta
            while ts_idx < len(timestamps) and timestamps[ts_idx] < bin_end:
                bin_sum += values[ts_idx]
                ts_idx += 1
            bin_starts.append(bin_start)
            bin_totals.append(bin_sum)
            bin_sum = 0
            bin_start = bin_end

        # Bar width fills bin with small gap
        bar_width = bin_delta * 0.9
        ax.bar(
            bin_starts, bin_totals,
            width=bar_width, color=color, alpha=0.3, align="edge", zorder=3,
        )

        # Cumulative line on secondary y-axis
        cumulative = []
        running = 0
        for v in values:
            running += v
            cumulative.append(running)

        ax2 = ax.twinx()
        ax2.plot(timestamps, cumulative, color="#ffffff", alpha=0.15, linewidth=4, zorder=4)
        ax2.plot(timestamps, cumulative, color=color, alpha=1.0, linewidth=2, zorder=5)
        ax2.fill_between(timestamps, cumulative, alpha=0.04, color=color, zorder=2)
        ax2.yaxis.set_major_formatter(make_formatter(is_currency))
        ax2.tick_params(colors=TEXT_DIM, labelsize=8)
        ax2.spines["right"].set_color(BORDER)

        if cumulative:
            total_val = cumulative[-1]
            ax2.annotate(
                f"Total: {human_format(total_val, is_currency)}",
                xy=(timestamps[-1], total_val),
                xytext=(-10, 8),
                textcoords="offset points",
                fontsize=10,
                color=color,
                fontweight="bold",
                ha="right",
                va="bottom",
                bbox=dict(
                    boxstyle="round,pad=0.3",
                    facecolor=BG_AXES,
                    edgecolor=color,
                    alpha=0.8,
                ),
            )

        ax.set_title(title, fontsize=13, fontweight="bold", color=TEXT, pad=10)
        ax.yaxis.set_major_formatter(make_formatter(is_currency))
        ax.set_ylabel(bin_label, fontsize=8, color=TEXT_DIM)
        ax2.set_ylabel("cumulative", fontsize=8, color=TEXT_DIM)
        ax.grid(True, alpha=0.2, color=GRID)

        if highlight:
            add_highlight_bands(ax, timestamps, highlight[0], highlight[1], tz)

        # Adaptive x-axis
        if span_h <= 6:
            ax.xaxis.set_major_locator(mdates.HourLocator())
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=fmt_tz))
        elif span_h <= 24:
            ax.xaxis.set_major_locator(mdates.HourLocator(interval=3))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=fmt_tz))
        elif span_h <= 24 * 3:
            ax.xaxis.set_major_locator(mdates.HourLocator(interval=6))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d %H:%M", tz=fmt_tz))
        elif span_h <= 24 * 7:
            ax.xaxis.set_major_locator(mdates.DayLocator())
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d", tz=fmt_tz))
        elif span_h <= 24 * 60:
            ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=0))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d", tz=fmt_tz))
        else:
            ax.xaxis.set_major_locator(mdates.MonthLocator())
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y", tz=fmt_tz))
        ax.tick_params(axis="x", rotation=0, labelsize=8)

    # Cost by model panel
    ax_summary = axes[len(CHARTS)]
    style_axes(ax_summary)
    model_costs = {}
    model_reqs = {}
    for e in events:
        m = e["model"]
        model_costs[m] = model_costs.get(m, 0) + e["costUSD"]
        model_reqs[m] = model_reqs.get(m, 0) + 1

    models = sorted(model_costs.keys(), key=lambda m: model_costs[m], reverse=True)
    bar_colors = list(COLORS.values())
    y_pos = list(range(len(models)))
    costs = [model_costs[m] for m in models]
    short_names = [m.replace("claude-", "").split("-2")[0] for m in models]
    c = [bar_colors[i % len(bar_colors)] for i in range(len(models))]

    bars = ax_summary.barh(y_pos, costs, color=c, alpha=0.85, height=0.5, zorder=3)
    for bar, m in zip(bars, models):
        val = model_costs[m]
        reqs = model_reqs[m]
        ax_summary.text(
            bar.get_width() + max(costs) * 0.02,
            bar.get_y() + bar.get_height() / 2,
            f"${val:.2f} ({reqs} calls)",
            va="center",
            ha="left",
            fontsize=10,
            color=TEXT,
            fontweight="bold",
        )

    ax_summary.set_yticks(y_pos)
    ax_summary.set_yticklabels(short_names, fontsize=10)
    ax_summary.set_title(
        "Cost by Model", fontsize=13, fontweight="bold", color=TEXT, pad=10
    )
    ax_summary.xaxis.set_major_formatter(make_formatter(True))
    ax_summary.grid(True, axis="x", alpha=0.3, color=GRID)
    ax_summary.invert_yaxis()
    if costs and max(costs) > 0:
        ax_summary.set_xlim(0, max(costs) * 1.4)

    # Token breakdown panel
    ax_breakdown = axes[len(CHARTS) + 1]
    style_axes(ax_breakdown)
    token_categories = [
        ("Input", "inputTokens", COLORS["inputTokens"]),
        ("Output", "outputTokens", COLORS["outputTokens"]),
        ("Cache Create", "cacheCreateTokens", COLORS["cacheCreateTokens"]),
        ("Cache Read", "cacheReadTokens", COLORS["cacheReadTokens"]),
    ]
    cat_labels = [c[0] for c in token_categories]
    cat_totals = [sum(e[c[1]] for e in events) for c in token_categories]
    cat_colors = [c[2] for c in token_categories]
    y_pos_bd = list(range(len(cat_labels)))

    bars_bd = ax_breakdown.barh(
        y_pos_bd, cat_totals, color=cat_colors, alpha=0.85, height=0.5, zorder=3
    )
    for bar, total in zip(bars_bd, cat_totals):
        if total > 0:
            pct = total / sum(cat_totals) * 100 if sum(cat_totals) > 0 else 0
            ax_breakdown.text(
                bar.get_width() + max(cat_totals) * 0.02,
                bar.get_y() + bar.get_height() / 2,
                f"{human_format(total)} ({pct:.1f}%)",
                va="center",
                ha="left",
                fontsize=10,
                color=TEXT,
                fontweight="bold",
            )

    ax_breakdown.set_yticks(y_pos_bd)
    ax_breakdown.set_yticklabels(cat_labels, fontsize=10)
    ax_breakdown.set_title(
        "Token Breakdown", fontsize=13, fontweight="bold", color=TEXT, pad=10
    )
    ax_breakdown.xaxis.set_major_formatter(make_formatter(False))
    ax_breakdown.grid(True, axis="x", alpha=0.3, color=GRID)
    ax_breakdown.invert_yaxis()
    if cat_totals and max(cat_totals) > 0:
        ax_breakdown.set_xlim(0, max(cat_totals) * 1.35)

    # Hide unused axes slots
    for i in range(len(CHARTS) + 2, len(axes)):
        axes[i].set_visible(False)

    # -- Burn rate panel (full width, bottom row) --
    style_axes(ax_burn)
    sessions = build_sessions(events)
    if sessions:
        window_boundaries = find_window_boundaries(events)
        limit_hits = find_limit_hits(events)
        cutoff = events[0]["timestamp"] if events else None
        end_ts = events[-1]["timestamp"] if events else None
        plot_burn_rate(ax_burn, events, sessions, window_boundaries, limit_hits,
                       view_start=cutoff, view_end=end_ts)

    fig.savefig(output_path, dpi=150, bbox_inches="tight", facecolor=BG_DARK)
    plt.close()
    print(f"Saved: {output_path}", file=sys.stderr)


SCRIPT_URL = "https://raw.githubusercontent.com/nhz-io/ccusage-plot/main/ccusage_plot.py"


def _resolve_script_path():
    """Find the real path of this script, resolving symlinks and verifying identity."""
    # resolve() follows symlinks and makes the path absolute
    candidate = Path(__file__).resolve()

    # If running via stdin (curl pipe), __file__ won't be a real path
    if not candidate.is_file():
        return None

    # Verify this is actually our script by checking for our version string
    try:
        content = candidate.read_text(encoding="utf-8")
        if f'__version__ = "{__version__}"' not in content:
            return None
    except Exception:
        return None

    return candidate


def check_update(target_path=None):
    """Check for a newer version and auto-update if available."""
    script_path = Path(target_path).resolve() if target_path else _resolve_script_path()

    if script_path is None:
        print(
            "Error: cannot determine script location (running via pipe?).\n"
            "Use: --update /path/to/ccusage_plot.py",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Script location: {script_path}", file=sys.stderr)

    try:
        with urllib.request.urlopen(SCRIPT_URL, timeout=10) as resp:
            remote_source = resp.read().decode("utf-8")
    except Exception as e:
        print(f"Error checking for updates: {e}", file=sys.stderr)
        sys.exit(1)

    # Extract remote version
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', remote_source, re.MULTILINE)
    if not m:
        print("Error: could not determine remote version.", file=sys.stderr)
        sys.exit(1)

    remote_version = m.group(1)
    if remote_version == __version__:
        print(f"Already up to date (v{__version__}).", file=sys.stderr)
        sys.exit(0)

    # Update in place
    try:
        script_path.write_text(remote_source, encoding="utf-8")
        # Set executable bit on Unix (no-op on Windows)
        if sys.platform != "win32":
            script_path.chmod(script_path.stat().st_mode | 0o111)
        print(f"Updated: v{__version__} -> v{remote_version}", file=sys.stderr)
    except Exception as e:
        print(f"Error writing update: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Plot Claude Code usage from local conversation logs"
    )
    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--update",
        nargs="?",
        const=True,
        default=None,
        metavar="PATH",
        help="Auto-update to the latest version from GitHub. Optionally specify script path.",
    )
    parser.add_argument(
        "-p",
        "--period",
        default=None,
        help="Time period, e.g. 6h, 3d, 1w, 2m (default: 24h)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Plot all history",
    )
    parser.add_argument(
        "--from",
        dest="date_from",
        default=None,
        help="Start date: YYYY-MM-DD or 'YYYY-MM-DD HH:MM'",
    )
    parser.add_argument(
        "--to",
        dest="date_to",
        default=None,
        help="End date: YYYY-MM-DD or 'YYYY-MM-DD HH:MM'",
    )
    parser.add_argument(
        "-o", "--output", help="Output PNG path (default: ccusage_{period}.png)"
    )
    parser.add_argument(
        "--tz",
        default=None,
        help="Timezone for x-axis and date parsing, e.g. PST, EST, UTC, Asia/Tokyo",
    )
    parser.add_argument(
        "--highlight",
        default=None,
        help="Highlight a daily time window, e.g. 5-11 or 5:00-11:30 (uses --tz)",
    )
    args = parser.parse_args()

    if args.update is not None:
        target = None if args.update is True else args.update
        check_update(target_path=target)
        sys.exit(0)

    tz = resolve_tz(args.tz) if args.tz else None

    # Resolve date range from --from, --to, -p combinations
    now = datetime.now(timezone.utc)
    has_from = args.date_from is not None
    has_to = args.date_to is not None
    has_period = args.period is not None

    if has_from and has_to and has_period:
        print("Error: cannot use --from, --to, and -p together.", file=sys.stderr)
        sys.exit(1)

    if has_from and has_to:
        # Explicit range
        start = parse_datetime(args.date_from, tz)
        end = parse_datetime(args.date_to, tz)
        period_label = f"{args.date_from}_to_{args.date_to}"
    elif has_from and has_period:
        # Start date + period forward
        start = parse_datetime(args.date_from, tz)
        end = start + parse_period(args.period)
        period_label = f"{args.date_from}+{args.period}"
    elif has_from:
        # From date to now
        start = parse_datetime(args.date_from, tz)
        end = now
        period_label = f"{args.date_from}_to_now"
    elif has_to and has_period:
        # Period ending at date
        end = parse_datetime(args.date_to, tz)
        start = end - parse_period(args.period)
        period_label = f"{args.period}_to_{args.date_to}"
    elif has_to:
        print("Error: --to requires either --from or -p.", file=sys.stderr)
        sys.exit(1)
    elif has_period:
        # Period back from now
        delta = parse_period(args.period)
        start = now - delta
        end = now
        period_label = args.period
    elif args.all:
        # All history
        start = None
        end = None
        period_label = "all"
    else:
        # Default: last 24h
        start = now - timedelta(hours=24)
        end = now
        period_label = "24h"

    print(f"Reading conversation logs from {PROJECTS_DIR} ...", file=sys.stderr)
    events = load_events(start, end)

    if not events:
        print(f"No API calls found for {period_label}.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(events)} API calls for {period_label}.", file=sys.stderr)

    output_path = args.output or f"ccusage_{period_label}.png"
    highlight = parse_highlight(args.highlight) if args.highlight else None

    plot_timeline(events, period_label, output_path, tz=tz, highlight=highlight)


if __name__ == "__main__":
    main()
