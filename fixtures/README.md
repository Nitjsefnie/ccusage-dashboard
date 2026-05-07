# fixtures

Small jsonl + zip samples for manual UI testing and parser unit tests.

## Convention

- Keep individual files under 200 KB. Larger samples go gitignored under `fixtures/large/` or pulled on demand from `/tmp/analyst.BCYKic3p/r2/`.
- Naming: `sample-<feature>-<small-uuid>.jsonl` (e.g. `sample-subagent-fan-out-7a9d.jsonl`).
- Each fixture should target a SPECIFIC parser feature so the corresponding test name maps 1:1.

## What to seed (when first implementation begins)

Suggested starter set:

| fixture | exercises |
|---|---|
| `sample-single-turn.jsonl` | minimal happy-path parse |
| `sample-multi-turn-tool-use.jsonl` | tool_call → tool_result pairing |
| `sample-streaming-merge.jsonl` | requestId-based usage max-merge |
| `sample-subagent-sidecar.zip` | cross-file UUID dedup + sidecar resolution |
| `sample-mixed-models.jsonl` | per-model rate routing (opus + haiku in same session) |
| `sample-cache-1h-only.jsonl` | 1h-only cache_create cost path |
| `sample-cache-5m-only.jsonl` | 5m-only cache_create cost path |
| `sample-cache-mixed.jsonl` | both 5m + 1h on same turn |
| `sample-legacy-no-split.jsonl` | older SDK records with `cache_creation_input_tokens` only (no `ephemeral_*` keys) |

The analyst-side R2 staging at `/tmp/analyst.BCYKic3p/r2/` has 1000+ real jsonls; pull representative samples from there, anonymise if needed, and trim to <200 KB before committing.
