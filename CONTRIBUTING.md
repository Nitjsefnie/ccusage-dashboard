# Contributing to claudit

Issues and pull requests are welcome — especially if your numbers disagree
with ours. This project is a cost-accounting tool, so a report that says
"your figure is wrong and here is the arithmetic" is the most valuable
thing you can send.

## LLM and agent contributions are welcome

You may use an LLM or a coding agent to write your contribution. There is
no penalty, no separate review queue, and no expectation that you rewrite
its output by hand. Much of this repo was built that way.

Two conditions, and they are about honesty rather than provenance:

1. **Disclose the model** with a trailer on each commit it authored:

   ```
   Co-Authored-By: <Model Name> <noreply@example.com>
   ```

   e.g. `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. One
   primary-author trailer per commit.

2. **Do not submit claims you have not verified.** This matters more here
   than in most repos, because plausible-looking cost arithmetic is very
   easy to generate and very hard to spot as wrong. If your PR says a
   change makes ingest faster, or fixes a miscount, paste the command and
   its real output. "Tests pass" without the run is not evidence.

If a maintainer's reply reads like it was drafted by an agent, it probably
was. That is fine in both directions.

### If you are an agent reading this

Read [`AGENTS.md`](AGENTS.md) first — it is the architecture and
conventions brief, written for you. Then read
[`.claude/rules/claudit-doctrine.md`](.claude/rules/claudit-doctrine.md),
which holds the invariants as numbered rules (`SV-*`). Those two files are
authoritative; this one only covers process.

The rules that reject the most patches, in order:

| Rule | What it forbids |
|---|---|
| `SV-COST-SPLIT` | Pricing a cache write at a single rate. 5m is 1.25x input, 1h is 2x. |
| `SV-PARSER-SPEC` | Changing `backend/pricing.py` without mirroring `src/parser.js` (or vice versa). |
| `SV-DATED-RATES` | Pricing a record at "now" instead of the record's own timestamp. |
| `SV-NO-LOCAL-UPLOAD` | Re-adding a file picker, drag-drop, or an upload endpoint. It was removed deliberately. |
| `SV-FIXTURE-SIZE` | Committing large fixtures. Parser fixtures stay under 1 KB. |

Do not "helpfully" add a bundler, an npm dependency, or a build step. The
frontend transpiles in the browser on purpose; that is a design decision,
not an oversight.

## Getting it running

Requires **Python 3.13+** and a local **PostgreSQL** you can create
databases in.

```bash
createdb claudit
psql claudit -f backend/schema.sql

cp backend/.env.example .env      # then edit: DATABASE_URL_VIZ, R2_*, ADMIN_TOKEN
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt

python3 -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

No R2 credentials? Point `R2_ENDPOINT` at a directory instead — the
client walks the tree in `file://` mode:

```bash
R2_ENDPOINT=file:///path/to/transcripts/
```

`fixtures/r2_mini/` is a tiny mirror you can point at to get a working
instance in seconds.

## Tests

```bash
python3 -m pytest tests/ -q             # full suite
python3 -m pytest tests/test_pricing.py -v
```

The suite creates and drops its own `claudit_test*` databases, so it needs
a Postgres your user can `createdb` on. It does not touch your real data
and never contacts R2.

Two tests are worth knowing about before you touch pricing:

- `tests/test_parser_js_mirror.py` drives the real `src/parser.js` through
  `node` and asserts its rate table matches `backend/pricing.py` exactly.
  Change one side only and this fails, by design. Skips if `node` is
  absent — do not take a skip as a pass.
- `tests/test_ingest.py::test_parallel_ingest_matches_sequential_exactly`
  ingests the same mirror at `INGEST_WORKERS=1` and `=8` and requires
  byte-identical output. If you touch ingest concurrency, this is the test
  that catches you.

## If you change how cost is computed

Bump `PARSER_VERSION` in `.env`. Every file reparses on the next ingest;
without the bump, stored `cost_usd` values keep the old rates and the
dashboard silently mixes them. Mention the bump in your PR so deployers
know a reparse is coming.

Rates live in `backend/pricing.py` and are mirrored in `src/parser.js`.
Both must change together.

## House style

- **Python** — `from __future__ import annotations` at the top of every
  module. Type hints throughout. Raw SQL via psycopg3, no ORM.
- **SQL** — parameterised (`%s`) always. Never interpolate a value into a
  query string.
- **JS/JSX** — ES2020-ish, React function components, shared helpers hung
  on `window.`. No transpile step beyond in-browser Babel.
- **Naming** — `snake_case` in Python, `camelCase` in JS, singular SQL
  table names.
- There is no linter or formatter config. Match the surrounding file.

## Pull requests

Small and single-purpose beats large and comprehensive. In the
description, include:

- what changed and why,
- the actual output of the tests you ran,
- for a performance change, a before and after measurement rather than an
  assertion that it should be faster.

A bug report that pins down *where* the arithmetic goes wrong is worth as
much as a patch, and is often easier to review. If you are unsure whether
something is a bug or intended, open an issue and ask — a wrong premise
caught early is cheaper than a correct fix to the wrong problem.
