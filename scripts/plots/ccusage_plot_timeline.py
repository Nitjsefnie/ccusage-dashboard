"""Full-timeline figure rendering for ccusage_plot_db.py (issue #8 split).

The six binned metric panels, the cost-by-model and token-breakdown
summary panels, and plot_timeline itself. Shared theme constants and the
burn-rate panel live in ccusage_plot_render.py; this module imports from
it (one direction only, so there is no cycle).
"""

from __future__ import annotations

import sys
from datetime import timedelta, timezone

from ccusage_plot_render import (
    BG_AXES, BG_DARK, BORDER, CHARTS, COLORS, GRID, TEXT, TEXT_DIM,
    TZ_ALIASES, add_highlight_bands, apply_theme, build_sessions,
    find_window_boundaries, get_claude_info, human_format, make_formatter,
    plot_burn_rate, style_axes,
)

try:
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib import gridspec
except ImportError:
    mdates = gridspec = plt = None



def _tz_label(tz):
    """UTC, or the short alias when the tz maps to one."""
    if not tz:
        return "UTC"
    label = str(tz)
    # Shorten IANA names for display
    for alias, iana in TZ_ALIASES.items():
        if iana == str(tz):
            return alias
    return label


def _pick_bin(span_h: float):
    """Snap the bar width to a clean interval targeting ~120 bars."""
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
    return bin_delta, bin_label


def _timeline_ctx(events, tz) -> dict:
    """Shared per-render values: display timestamps, labels, bin width."""
    if tz:
        timestamps = [e["timestamp"].astimezone(tz) for e in events]
    else:
        timestamps = [e["timestamp"] for e in events]
    # Determine time span and bin size
    span_h = (
        (timestamps[-1] - timestamps[0]).total_seconds() / 3600
        if len(timestamps) > 1
        else 1
    )
    bin_delta, bin_label = _pick_bin(span_h)
    return {
        "timestamps": timestamps,
        "tz_label": _tz_label(tz),
        "span_h": span_h,
        "bin_delta": bin_delta,
        "bin_label": bin_label,
        "fmt_tz": tz if tz else timezone.utc,
    }


def _timeline_figure():
    """The 4x2 metric grid plus the full-width burn-rate row."""
    if plt is None or gridspec is None:
        raise RuntimeError("matplotlib is required to render plots")
    fig = plt.figure(figsize=(18, 26))
    gs_top = gridspec.GridSpec(4, 2, figure=fig,
                               top=0.94, bottom=0.27, hspace=0.35, wspace=0.3)
    gs_burn = gridspec.GridSpec(1, 1, figure=fig,
                                top=0.21, bottom=0.03)
    axes = [fig.add_subplot(gs_top[r, c]) for r in range(4) for c in range(2)]
    ax_burn = fig.add_subplot(gs_burn[0])
    return fig, axes, ax_burn


def _timeline_header(fig, events, ctx, tz, highlight):
    """Suptitle (plan/version) + subtitle (range, calls, cost)."""
    display_tz = tz if tz else timezone.utc
    timestamps = ctx["timestamps"]
    # Actual date range from data
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
    date_range_str = f"{first_ts.strftime('%b %d %H:%M')} – {last_ts.strftime('%b %d %H:%M')} {ctx['tz_label']}"

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
        f"{len(events)} API calls",
        f"${sum(e['costUSD'] for e in events):.2f} total",
    ]
    if highlight:
        subtitle_parts.append(
            f"Highlight: {int(highlight[0])}:00–{int(highlight[1])}:00"
        )
    fig.text(
        0.5,
        0.96,
        "  |  ".join(subtitle_parts),
        ha="center",
        fontsize=11,
        color=TEXT_DIM,
    )


def _bin_events(timestamps, values, bin_delta):
    """Sum values into fixed-width time bins starting at the first ts."""
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
    return bin_starts, bin_totals


def _cumulative(values):
    out = []
    running = 0
    for v in values:
        running += v
        out.append(running)
    return out


def _adaptive_xaxis(ax, span_h, fmt_tz):
    """Locator/formatter picked by the visible time span."""
    if mdates is None:
        raise RuntimeError("matplotlib is required to render plots")
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


def _plot_metric_panel(ax, ctx, events, title, key, is_currency, highlight, tz):
    """One binned metric panel + cumulative line on a secondary axis."""
    style_axes(ax)
    timestamps = ctx["timestamps"]
    values = [e[key] for e in events]
    color = COLORS[key]

    bin_starts, bin_totals = _bin_events(timestamps, values, ctx["bin_delta"])

    # Bar width fills bin with small gap (matplotlib date numbers are days)
    ax.bar(
        bin_starts, bin_totals,
        width=ctx["bin_delta"].total_seconds() / 86400 * 0.9,
        color=color, alpha=0.3, align="edge", zorder=3,
    )

    # Cumulative line on secondary y-axis
    cumulative = _cumulative(values)
    ax2 = ax.twinx()
    ax2.plot(timestamps, cumulative, color="#ffffff", alpha=0.15, linewidth=4, zorder=4)
    ax2.plot(timestamps, cumulative, color=color, alpha=1.0, linewidth=2, zorder=5)
    ax2.fill_between(timestamps, cumulative, alpha=0.04, color=color, zorder=2)
    ax2.yaxis.set_major_formatter(make_formatter(is_currency))
    ax2.tick_params(colors=TEXT_DIM, labelsize=8)
    ax2.spines["right"].set_color(BORDER)

    if cumulative:
        ax2.annotate(
            f"Total: {human_format(cumulative[-1], is_currency)}",
            xy=(timestamps[-1], cumulative[-1]),
            xytext=(-10, 8),
            textcoords="offset points",
            fontsize=10,
            color=color,
            fontweight="bold",
            ha="right",
            va="bottom",
            bbox={
                "boxstyle": "round,pad=0.3",
                "facecolor": BG_AXES,
                "edgecolor": color,
                "alpha": 0.8,
            },
        )

    ax.set_title(title, fontsize=13, fontweight="bold", color=TEXT, pad=10)
    ax.yaxis.set_major_formatter(make_formatter(is_currency))
    ax.set_ylabel(ctx["bin_label"], fontsize=8, color=TEXT_DIM)
    ax2.set_ylabel("cumulative", fontsize=8, color=TEXT_DIM)
    ax.grid(True, alpha=0.2, color=GRID)

    if highlight:
        add_highlight_bands(ax, timestamps, highlight[0], highlight[1], tz)

    _adaptive_xaxis(ax, ctx["span_h"], ctx["fmt_tz"])


def _plot_cost_by_model(ax_summary, events):
    """Horizontal bar chart of total cost per model."""
    style_axes(ax_summary)
    stats: dict[str, list] = {}
    for e in events:
        entry = stats.setdefault(e["model"], [0.0, 0])
        entry[0] += e["costUSD"]
        entry[1] += 1

    models = sorted(stats, key=lambda m: stats[m][0], reverse=True)
    costs = [stats[m][0] for m in models]
    bar_colors = list(COLORS.values())
    c = [bar_colors[i % len(bar_colors)] for i in range(len(models))]

    bars = ax_summary.barh(
        list(range(len(models))), costs, color=c, alpha=0.85, height=0.5, zorder=3
    )
    for rect, m in zip(bars, models):
        ax_summary.text(
            rect.get_width() + max(costs) * 0.02,
            rect.get_y() + rect.get_height() / 2,
            f"${stats[m][0]:.2f} ({stats[m][1]} calls)",
            va="center",
            ha="left",
            fontsize=10,
            color=TEXT,
            fontweight="bold",
        )

    ax_summary.set_yticks(list(range(len(models))))
    ax_summary.set_yticklabels(
        [m.replace("claude-", "").split("-2")[0] for m in models], fontsize=10
    )
    ax_summary.set_title(
        "Cost by Model", fontsize=13, fontweight="bold", color=TEXT, pad=10
    )
    ax_summary.xaxis.set_major_formatter(make_formatter(True))
    ax_summary.grid(True, axis="x", alpha=0.3, color=GRID)
    ax_summary.invert_yaxis()
    if costs and max(costs) > 0:
        ax_summary.set_xlim(0, max(costs) * 1.4)


def _plot_token_breakdown(ax_breakdown, events):
    """Horizontal bar chart of token totals by category."""
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
    for rect, total in zip(bars_bd, cat_totals):
        if total > 0:
            pct = total / sum(cat_totals) * 100 if sum(cat_totals) > 0 else 0
            ax_breakdown.text(
                rect.get_width() + max(cat_totals) * 0.02,
                rect.get_y() + rect.get_height() / 2,
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




def _plot_panels(axes, ctx, events, highlight, tz):
    """The six binned metric panels + cost-by-model + token breakdown."""
    for idx, (title, key, is_currency) in enumerate(CHARTS):
        _plot_metric_panel(
            axes[idx], ctx, events, title, key, is_currency, highlight, tz
        )

    _plot_cost_by_model(axes[len(CHARTS)], events)
    _plot_token_breakdown(axes[len(CHARTS) + 1], events)

    # Hide unused axes slots
    for i in range(len(CHARTS) + 2, len(axes)):
        axes[i].set_visible(False)

def plot_timeline(events, period_str, output_path, tz=None, highlight=None,
                  limit_hits=None):
    """Render the full dashboard PNG: 6 metric panels + cost-by-model +
    token breakdown + the full-width burn-rate row.

    `limit_hits` is a zero-arg callable (evaluated lazily — only when a
    burn panel will actually render) or an already-computed list.
    """
    if plt is None:
        raise RuntimeError("matplotlib is required to render plots")
    apply_theme()
    ctx = _timeline_ctx(events, tz)
    fig, axes, ax_burn = _timeline_figure()

    _timeline_header(fig, events, ctx, tz, highlight)

    _plot_panels(axes, ctx, events, highlight, tz)

    # -- Burn rate panel (full width, bottom row) --
    style_axes(ax_burn)
    sessions = build_sessions(events)
    if sessions:
        window_boundaries = find_window_boundaries(events)
        hits = limit_hits() if callable(limit_hits) else (limit_hits or [])
        plot_burn_rate(ax_burn, events, sessions, window_boundaries, hits,
                       view_start=events[0]["timestamp"],
                       view_end=events[-1]["timestamp"])

    fig.savefig(output_path, dpi=150, bbox_inches="tight", facecolor=BG_DARK)
    plt.close()
    print(f"Saved: {output_path}", file=sys.stderr)
