# Time-series right-edge geometry design

## Scope

Port the proven time-series geometry correction from kimimeter and codexmeter
into claudit. Every `TimeSeriesPanel` bar must remain inside the plot and must
not cover the cumulative-axis gutter. Preserve the server-bucket floor added
in issue #15; do not return to finer display bins or conceal invalid geometry
with SVG clipping.

This change does not redesign the dashboard, alter API aggregation, or change
the already-complete line-churn feature from issue #10. The rendering defect
is tracked separately as issue #17.

## Reproduced failure

The dashboard API timestamps aggregate rows at bucket centers.
`backendDashToShape` currently defines the chart range as the first center
through the last center plus 1 ms. When the display bin equals the server
bucket, the final bin begins effectively at the plot's right boundary and
retains a full bar width.

For centers at 03:00, 09:00, and 15:00 with six-hour buckets and a 400 px
plot, the current code creates 03–09, 09–15, and 15–21 bins. The last bar
begins at approximately x=460, the plot boundary, and ends at x=580. Its
cumulative point reaches approximately x=660.

## Correct temporal model

Each backend timestamp is a bucket center. Given `bucket_s`, its complete
visible coverage is:

```text
start = first_center - bucket_s / 2
end   = last_center  + bucket_s / 2
```

When valid bucket metadata is unavailable, retain the existing point-event
fallback of the first timestamp through the last timestamp plus 1 ms.

Every visual bin is a bounded half-open interval:

```text
bin.start = previous bin end
bin.end   = min(bin.start + display_bin_width, range.end)
```

The final interval may be partial. Its rendered width must be proportional to
that actual interval.

## Rendering and interaction

Bars, cumulative points, tooltip ranges, and hover selection all use the same
bounded intervals. A bar starts at scaled `bin.start` and consumes 90% of the
distance to scaled `bin.end`, preserving the existing gap. Cumulative points
use bounded `bin.end`. Hover is limited to the plot rectangle and selects the
interval containing the pointer timestamp, including the final range edge in
the last bin.

The right gutter remains exclusively for cumulative-axis labels and its
rotated title. Clipping is not the primary fix because it would hide incorrect
bar, cumulative, and hover geometry.

## Components

- `src/app.jsx` reconstructs full aggregate coverage from bucket centers.
- `src/dashboard-charts.jsx` builds bounded bins and derives every visual and
  interactive coordinate from them.
- `tests/test_time_series_geometry.py` executes the shipped JavaScript helpers
  through Node and checks adapter, interval, bar, cumulative, and hover
  behavior.

Unlike the sibling repositories, claudit has no offline drag-and-drop load
path, so the port does not introduce or test one.

## Validation

The regression suite proves full server-bucket coverage, bounded partial final
intervals, in-plot bar rectangles, cumulative termination at the range edge,
and hover selection from the same intervals. Existing issue #10 parser and API
tests are rerun to confirm its already-delivered churn data still reaches the
panels. The full pytest, type, Python lint, style, and JavaScript lint gates
must pass before one batched push, followed by all remote workflows.

Every commit includes:

```text
Co-authored-by: GPT-5.6 Sol <noreply@openai.com>
```

The completing implementation commit also includes `Closes #17`.
