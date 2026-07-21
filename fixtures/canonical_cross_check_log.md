# Canonical Cross-Check — 2026-05-07

End-to-end correctness check: claudit's per-session aggregates vs. the
canonical `parse_session.py --cache` walked over the same R2 mirror.

## Mirror

- Path: `/tmp/analyst.BCYKic3p/r2/`
- Refresh: `rclone copy r2:claude/ /tmp/analyst.BCYKic3p/r2/` (exit 0, no-op
  since mirror was already in sync)
- Total `*.jsonl`: **1169**
- Unique parent dirs: **536** (459 session folders at depth 6 + 69
  `data/subagents` dirs at depth 8 + a few outliers)
- Bucket size on disk: **1.4 GiB**

## claudit ingest

Lifespan startup ingest, fresh `claudit` DB:

| field | value |
|---|---|
| trigger | startup |
| r2_listed | 451 (main `<stem>.jsonl` files only) |
| inserted | 451 |
| reparsed | 0 |
| deleted | 0 |
| error | null |
| duration | 143.1 s |

Cron ingest fired at :15 and saw no changes (`reparsed=0` against the
just-ingested rows).

## Postgres aggregate (sessions table SUMs)

| metric | value |
|---|---|
| COUNT(*) | 451 |
| input_tokens (fresh) | 404,932 |
| cache_create_5m_tokens | 39,663,323 |
| cache_create_1h_tokens | 348,905,838 |
| cache_read_tokens | 20,139,112,548 |
| output_tokens | 33,863,800 |
| SUM(cost_usd) | **$14,585.78** |
| SUM(request_count) | 82,522 |
| projects | 21 |
| record_uuids rows | 504,045 |
| session_requests rows | 82,522 |

## Canonical `parse_session.py --cache`

Walked the same `/tmp/analyst.BCYKic3p/r2/` tree. Wall: 20.1 s.

| bucket | tokens | cost |
|---|---|---|
| Fresh input | 439,428 | $1.92 |
| Cache create 5m (incl. unsplit) | 39,589,961 | $223.72 |
| Cache create 1h | 352,095,845 | $3,504.59 |
| Cache read | 20,176,180,267 | $10,058.53 |
| Output | 33,869,643 | $837.58 |
| **TOTAL** |  | **$14,626.34** |
| SESSION TOTAL turns | 83,217 |  |
| Per turn |  | $0.1758 |

## Deltas

| field | claudit | canonical | Δ | Δ % |
|---|---|---|---|---|
| TOTAL cost | $14,585.78 | $14,626.34 | -$40.56 | -0.28% |
| Turns/requests | 82,522 | 83,217 | -695 | -0.83% |
| Fresh input tokens | 404,932 | 439,428 | -34,496 | -7.85% |
| Cache 5m tokens | 39,663,323 | 39,589,961 | +73,362 | +0.19% |
| Cache 1h tokens | 348,905,838 | 352,095,845 | -3,190,007 | -0.91% |
| Cache read tokens | 20,139,112,548 | 20,176,180,267 | -37,067,719 | -0.18% |
| Output tokens | 33,863,800 | 33,869,643 | -5,843 | -0.02% |

## Gap source — main-less folders

13 session folders contain ONLY peer `agent-*.jsonl` files with NO main
`<stem>.jsonl`. Example:

```
/tmp/analyst.BCYKic3p/r2/C--Users-zmatek-PycharmProjects-kvalita/03de022a-47b0-46ce-87fd-3bf81ebf46ad/
  agent-abc8ca8.jsonl
  agent-ae2dd54.jsonl
  agent-af45044.jsonl
```

`backend/ingest._list_main_jsonls` skips folders without a main file
(`stem != session_dir`), but `parse_session.py` walks all `*.jsonl`
recursively and dedups by inner-record uuid — so canonical includes the
agent transcripts as part of the global walk while claudit drops them.

These appear to be sub-agent transcripts whose parent session folder was
deleted (the parent `<uuid>.jsonl` was cleaned up but the sub-agent
sidecars were not). They account for ~$40 / ~700 turns of the gap.

## Verdict

**FAIL** — first run.

The plan's PASS criterion was `|cost delta| ≤ $0.10` (rounding noise from
per-request vs. per-session rollup). Actual delta is $40.56 — 405× the
tolerance. Sub-1% relative is irrelevant; the absolute gap maps to real
records being dropped, not floating-point noise.

Identified cause: `backend/ingest._list_main_jsonls` only emits an entry
when filename-stem == parent-dir-name. 13 session folders contain ONLY
`agent-*.jsonl` peers with no main `<stem>.jsonl` (the parent main was
deleted but the sub-agent traces remain). Per-file whitelisting drops
them; the canonical's `os.walk` + cross-file uuid dedup picks them up.

Fix landed in a follow-up commit (see git log after this entry):
`_list_main_jsonls` now falls back to the alphabetically-first
`agent-*.jsonl` when no stem-match exists, using the folder name as the
synthetic session_id. The orphan-sub-agent transcripts get ingested as
their own session row, closing the gap.

See the second cross-check entry below for the post-fix verdict.
