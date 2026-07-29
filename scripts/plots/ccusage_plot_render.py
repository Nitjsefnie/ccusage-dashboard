"""Matplotlib rendering for ccusage_plot_db.py (issue #8 split).

Everything that touches matplotlib lives here so the DB/CLI half of the
script stays importable on matplotlib-less hosts (the test suite loads it
for load_events). Split out of ccusage_plot_db.py when the single file
outgrew pylint's 1000-line gate; plot_burn_rate and plot_timeline were
decomposed into draw-step helpers at the same time so no function trips
the locals/branches/statements gates.

The guarded import is deliberate: importing this module must not require
matplotlib, but every function that uses it checks and raises loudly.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib import ticker
    from matplotlib.lines import Line2D
except ImportError:
    mdates = ticker = plt = Line2D = None

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

HIGHLIGHT_COLOR = "#ffffff"
HIGHLIGHT_ALPHA = 0.06


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
    if ticker is None:
        raise RuntimeError("matplotlib is required to render plots")
    return ticker.FuncFormatter(lambda v, _: human_format(v, is_currency))


_PERIOD_UNITS = {
    "h": lambda v: timedelta(hours=v),
    "d": lambda v: timedelta(days=v),
    "w": lambda v: timedelta(weeks=v),
    "m": lambda v: timedelta(days=v * 30),
}


def parse_period(period_str):
    m = re.fullmatch(r"(\d+)\s*([hdwm])", period_str.strip().lower())
    if not m:
        print(
            f"Error: invalid period '{period_str}'. Use e.g. 6h, 3d, 1w, 2m",
            file=sys.stderr,
        )
        sys.exit(1)
    return _PERIOD_UNITS[m.group(2)](int(m.group(1)))


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


def apply_theme():
    if plt is None:
        raise RuntimeError("matplotlib is required to render plots")
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


def resolve_tz(tz_str: str | None) -> ZoneInfo | None:
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
            # A missing/failed CLI falls back to the credentials file below.
            check=False,
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
            # A missing/failed CLI just leaves the version blank.
            check=False,
        )
        version = result.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    return plan, version


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
        primary = max(models, key=models.__getitem__)

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


def _session_buckets(session_events, start, end, field_map, token_keys, bucket_s):
    """Fixed-width per-hour-rate buckets for one session range."""
    buckets = []
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
    return buckets


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
        buckets += _session_buckets(
            session_events, start, end, field_map, token_keys, bucket_min * 60
        )
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


# ---------------------------------------------------------------------------
# Session burn rate panel
# ---------------------------------------------------------------------------


def _burn_emas(sessions, visible, token_keys):
    """Per-token EMAs over all sessions (shift detection) and over the
    visible subset (display, with an alpha adapted to the count)."""
    all_emas = {
        key: compute_ema([s[f"{key}_per_h"] for s in sessions])
        for key in token_keys
    }
    session_emas = {
        key: {id(s): all_emas[key][i] for i, s in enumerate(sessions)}
        for key in token_keys
    }
    shifts = detect_shifts(all_emas["output"], sessions)

    display_alpha = max(EMA_ALPHA, 2.0 / (len(visible) + 1))
    display_emas = {
        key: compute_ema([s[f"{key}_per_h"] for s in visible], alpha=display_alpha)
        for key in token_keys
    }
    return session_emas, shifts, display_emas


def _burn_context(sessions, view_start, view_end):
    """Everything the burn-rate draw steps need, computed once.

    Returns None when no session is visible (the panel hides itself).
    """
    token_keys = ["output", "input", "cache_create", "cache_read"]

    visible = sessions
    if view_start or view_end:
        visible = [s for s in sessions
                   if (not view_start or s["end"] >= view_start)
                   and (not view_end or s["start"] <= view_end)]
    if not visible:
        return None

    session_emas, shifts, display_emas = _burn_emas(sessions, visible, token_keys)

    xlim_start = view_start or visible[0]["start"] - timedelta(hours=2)
    xlim_end = view_end or visible[-1]["end"] + timedelta(hours=2)
    span_h = (xlim_end - xlim_start).total_seconds() / 3600

    if span_h <= 4:
        rate_mult, rate_unit = 1 / 60, "min"
    else:
        rate_mult, rate_unit = 1, "hour"

    return {
        "token_keys": token_keys,
        "visible": visible,
        "session_emas": session_emas,
        "shifts": shifts,
        "display_emas": {
            key: [v * rate_mult for v in display_emas[key]] for key in token_keys
        },
        "timestamps": [s["mid"] for s in visible],
        "out_rates": [s["output_per_h"] * rate_mult for s in visible],
        "colors": [MODEL_COLORS.get(s["primary_model"], "#888888") for s in visible],
        "xlim_start": xlim_start,
        "xlim_end": xlim_end,
        "span_h": span_h,
        "rate_mult": rate_mult,
        "rate_unit": rate_unit,
        "visible_hits": [],
    }


def _burn_draw_markers(ax, ctx, window_boundaries, limit_hits):
    """Window boundary lines + rate-limit hit markers."""
    for wb in window_boundaries:
        if wb < ctx["xlim_start"] or wb > ctx["xlim_end"]:
            continue
        ax.axvline(wb, color=COLOR_WINDOW, alpha=0.12, linewidth=1, linestyle=":", zorder=1)

    visible_hits = [h for h in limit_hits if ctx["xlim_start"] <= h["ts"] <= ctx["xlim_end"]]
    for hit in visible_hits:
        ax.axvline(hit["ts"], color=COLOR_LIMIT_HIT, alpha=0.7, linewidth=2, zorder=9)
    ctx["visible_hits"] = visible_hits


def _burn_draw_series(ax, ctx, events):
    """Session dots, per-token EMA lines, and (narrow views) bucket lines."""
    visible = ctx["visible"]

    # Session dots
    sizes = [min(max(s["dur_h"] * 60, 25), 250) for s in visible]
    ax.scatter(ctx["timestamps"], ctx["out_rates"], s=sizes, c=ctx["colors"], alpha=0.5,
               edgecolors="white", linewidths=0.3, zorder=6)

    # EMA lines
    for key in ctx["token_keys"]:
        style = BURN_TOKEN_STYLES[key]
        ax.plot(ctx["timestamps"], ctx["display_emas"][key], color=style["color"],
                alpha=style["alpha"], linewidth=style["lw"], zorder=8,
                label=style["label"])

    # Intra-session bucket lines (narrow views)
    if len(visible) <= BUCKET_THRESHOLD and events:
        buckets = build_buckets(events, visible)
        if len(buckets) > len(visible):
            bucket_ts = [b["mid"] for b in buckets]
            for key in ctx["token_keys"]:
                raw = [b[f"{key}_per_h"] * ctx["rate_mult"] for b in buckets]
                smoothed = compute_ema(raw, alpha=0.3)
                style = BURN_TOKEN_STYLES[key]
                ax.plot(bucket_ts, smoothed, color=style["color"],
                        alpha=0.25, linewidth=0.8, zorder=5, linestyle="-")


def _burn_draw_shifts(ax, ctx):
    """Behavioral-shift annotations (≥2x or ≤0.5x vs the 10-session baseline)."""
    visible_shifts = [s for s in ctx["shifts"] if ctx["xlim_start"] <= s["ts"] <= ctx["xlim_end"]]
    for shift in visible_shifts:
        for s in ctx["visible"]:
            if abs((s["mid"] - shift["ts"]).total_seconds()) < 7200:
                y_pos = ctx["session_emas"]["output"][id(s)] * ctx["rate_mult"]
                if shift["direction"] == "up":
                    arrow, fg, bg, edge = "↑", "#ff6666", "#3a1a1a", "#ff6666"
                else:
                    arrow, fg, bg, edge = "↓", "#44ff88", "#1a3a2a", "#44ff88"
                ax.annotate(
                    f"{arrow} {shift['ratio']:.1f}x",
                    xy=(shift["ts"], y_pos),
                    xytext=(0, -25), textcoords="offset points",
                    fontsize=7, color=fg, ha="center", va="top",
                    bbox={"boxstyle": "round,pad=0.2", "facecolor": bg,
                          "edgecolor": edge, "alpha": 0.8},
                    zorder=11,
                )
                break


def _burn_style_axes(ax, ctx):
    """Log y-scale, limits, spines, formatters and the adaptive x-axis."""
    if mdates is None or ticker is None:
        raise RuntimeError("matplotlib is required to render plots")
    all_visible_rates = []
    for key in ctx["token_keys"]:
        all_visible_rates.extend(ctx["display_emas"][key])
    all_visible_rates.extend(ctx["out_rates"])
    ax.set_yscale("log")
    y_bottom = max(min(all_visible_rates) * 0.3, 1)
    y_top = max(all_visible_rates) * 3
    ax.set_ylim(bottom=y_bottom, top=y_top)
    ax.set_xlim(ctx["xlim_start"], ctx["xlim_end"])

    for spine in ax.spines.values():
        spine.set_color(BORDER)
        spine.set_linewidth(1.5)
    ax.tick_params(colors=TEXT_DIM, labelsize=9)
    ax.yaxis.set_major_formatter(ticker.FuncFormatter(
        lambda v, _: human_format(v, False)))
    ax.set_ylabel(f"Tokens / {ctx['rate_unit']} (EMA)", fontsize=11, color=TEXT_DIM)
    ax.grid(True, alpha=0.2, color=GRID, axis="y")
    ax.grid(True, alpha=0.1, color=GRID, axis="x")

    fmt_tz = timezone.utc
    span_h = ctx["span_h"]
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


def _burn_legend_handles(ctx):
    """One legend handle per token EMA, window marker, model and dot size."""
    if Line2D is None:
        raise RuntimeError("matplotlib is required to render plots")
    handles = []
    for key in ctx["token_keys"]:
        style = BURN_TOKEN_STYLES[key]
        handles.append(Line2D([0], [0], color=style["color"],
                              linewidth=style["lw"],
                              alpha=style["alpha"],
                              label=f"{style['label']} (EMA)"))
    handles.append(Line2D([0], [0], color=COLOR_WINDOW, alpha=0.3,
                          linewidth=1, linestyle=":",
                          label="Window start (5h+ gap)"))
    if ctx["visible_hits"]:
        handles.append(Line2D([0], [0], color=COLOR_LIMIT_HIT,
                              linewidth=2, label="Rate limit hit"))
    for model in sorted(set(s["primary_model"] for s in ctx["visible"])):
        c = MODEL_COLORS.get(model, "#888888")
        handles.append(Line2D([0], [0], marker="o", color="none",
                              markerfacecolor=c, markeredgecolor="white",
                              markeredgewidth=0.3, markersize=8,
                              alpha=0.6, label=model))
    for dur_label, dur_h in [("30m", 0.5), ("1h", 1), ("4h", 4)]:
        sz = min(max(dur_h * 60, 25), 250)
        handles.append(Line2D([0], [0], marker="o", color="none",
                              markerfacecolor="#888888",
                              markeredgecolor="white",
                              markeredgewidth=0.3,
                              markersize=sz ** 0.5,
                              alpha=0.4, label=dur_label))
    return handles


def _burn_title(ax, ctx, window_boundaries):
    visible = ctx["visible"]
    t0 = visible[0]["start"].strftime("%b %d")
    t1 = visible[-1]["end"].strftime("%b %d, %Y")
    total_reqs = sum(s["reqs"] for s in visible)
    n_windows = sum(
        1 for wb in window_boundaries if ctx["xlim_start"] <= wb <= ctx["xlim_end"]
    ) + 1
    ax.set_title(
        f"Session Burn Rate  |  {t0} – {t1} UTC"
        f"  |  {len(visible)} sessions, {n_windows} windows, {total_reqs:,} requests",
        fontsize=13, fontweight="bold", color=TEXT, pad=70,
    )


def plot_burn_rate(ax, events, sessions, window_boundaries, limit_hits,
                   view_start=None, view_end=None):
    """Render the session burn rate panel onto the given axes."""
    ctx = _burn_context(sessions, view_start, view_end)
    if ctx is None:
        ax.set_visible(False)
        return
    _burn_draw_markers(ax, ctx, window_boundaries, limit_hits)
    _burn_draw_series(ax, ctx, events)
    _burn_draw_shifts(ax, ctx)
    _burn_style_axes(ax, ctx)
    ax.legend(handles=_burn_legend_handles(ctx), loc="lower center",
              bbox_to_anchor=(0.5, 1.03), fontsize=7, ncol=6,
              facecolor=BG_AXES, edgecolor=BORDER, labelcolor=TEXT,
              framealpha=0.9)
    _burn_title(ax, ctx, window_boundaries)
