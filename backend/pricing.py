"""Per-model cost rates (USD per million tokens).

SINGLE SOURCE OF TRUTH for cost in claudit. Mirrored by src/parser.js
(SV-PARSER-SPEC) — keep both in lockstep. Bump PARSER_VERSION when this
table changes; every session reparses.

Cache writes are split by TTL:
  5m write = 1.25x base input (column 'create_5m')
  1h write = 2x base input    (column 'create_1h')

Tokens recorded as cache_creation_input_tokens with NO ephemeral_5m/1h
split (older SDK versions) are charged at the 5m rate — conservative
undercount, not overcount. See SV-COST-SPLIT in
.claude/rules/claudit-doctrine.md.

Three resolution behaviours matter, in priority order:

1. EXACT — the normalised model id matches a key in MODEL_RATES, allowing
   only a dated-snapshot or bracket suffix after it. A version suffix the
   table doesn't know (``claude-opus-4-9``) deliberately does NOT match the
   shorter ``claude-opus-4`` key: billing a future Opus at retired 15/75
   rates is a silent 3x overcount.
2. TIER — an unrecognised Claude model falls back to its family's
   current-generation rates and is reported as ``kind="tier"`` so callers
   can mark the figure estimated rather than presenting it as fact.
3. DEFAULT — anything else. Also flagged.

Rates are a function of (model, timestamp): a model may carry dated
overrides (e.g. an introductory price). Cost must be computed against the
timestamp of the request being priced, not the time of rendering.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

UTC = timezone.utc


# List prices. Order: most-specific first.
MODEL_RATES = {
    "claude-fable-5":    {"fresh": 10.00, "create_5m": 12.50, "create_1h": 20.00, "read": 1.00, "output": 50.00},
    "claude-opus-4-8":   {"fresh": 5.00,  "create_5m": 6.25,  "create_1h": 10.00, "read": 0.50, "output": 25.00},
    "claude-opus-4-7":   {"fresh": 5.00,  "create_5m": 6.25,  "create_1h": 10.00, "read": 0.50, "output": 25.00},
    "claude-opus-4-6":   {"fresh": 5.00,  "create_5m": 6.25,  "create_1h": 10.00, "read": 0.50, "output": 25.00},
    "claude-opus-4-5":   {"fresh": 5.00,  "create_5m": 6.25,  "create_1h": 10.00, "read": 0.50, "output": 25.00},
    "claude-opus-4-1":   {"fresh": 15.00, "create_5m": 18.75, "create_1h": 30.00, "read": 1.50, "output": 75.00},
    "claude-opus-4":     {"fresh": 15.00, "create_5m": 18.75, "create_1h": 30.00, "read": 1.50, "output": 75.00},
    "claude-sonnet-5":   {"fresh": 3.00,  "create_5m": 3.75,  "create_1h": 6.00,  "read": 0.30, "output": 15.00},
    "claude-sonnet-4-6": {"fresh": 3.00,  "create_5m": 3.75,  "create_1h": 6.00,  "read": 0.30, "output": 15.00},
    "claude-sonnet-4-5": {"fresh": 3.00,  "create_5m": 3.75,  "create_1h": 6.00,  "read": 0.30, "output": 15.00},
    "claude-sonnet-4":   {"fresh": 3.00,  "create_5m": 3.75,  "create_1h": 6.00,  "read": 0.30, "output": 15.00},
    "claude-haiku-4-5":  {"fresh": 1.00,  "create_5m": 1.25,  "create_1h": 2.00,  "read": 0.10, "output": 5.00},
    "claude-3-7-sonnet-": {"fresh": 3.00, "create_5m": 3.75,  "create_1h": 6.00,  "read": 0.30, "output": 15.00},
    "claude-3-5-sonnet-": {"fresh": 3.00, "create_5m": 3.75,  "create_1h": 6.00,  "read": 0.30, "output": 15.00},
    "claude-3-5-haiku-": {"fresh": 0.80, "create_5m": 1.00,  "create_1h": 1.60,  "read": 0.08, "output": 4.00},
    "claude-3-opus-":    {"fresh": 15.00, "create_5m": 18.75, "create_1h": 30.00, "read": 1.50, "output": 75.00},
    "claude-3-haiku-":   {"fresh": 0.25, "create_5m": 0.30,  "create_1h": 0.50,  "read": 0.03, "output": 1.25},
}

DEFAULT_RATES = MODEL_RATES["claude-opus-4-7"]


def _scaled(fresh: float, output: float) -> dict:
    """Cache tiers derive from the input rate: 5m 1.25x, 1h 2x, read 0.1x."""
    return {
        "fresh": fresh,
        "create_5m": round(fresh * 1.25, 6),
        "create_1h": round(fresh * 2.00, 6),
        "read": round(fresh * 0.10, 6),
        "output": output,
    }


# Dated overrides, per exact key: (end_exclusive_utc, rates). Applied only
# when a timestamp is supplied and only on an EXACT key match — a tier
# fallback never inherits another model's promotional price.
#
# Claude Sonnet 5 launched at an introductory 2.00/10.00; list price
# 3.00/15.00 takes effect 2026-09-01 UTC (intro runs through 2026-08-31
# inclusive).
DATED_RATES: dict[str, list[tuple[datetime, dict]]] = {
    "claude-sonnet-5": [
        (datetime(2026, 9, 1, tzinfo=UTC), _scaled(2.00, 10.00)),
    ],
}

# Sorted boundaries where any rate changes. Read-time aggregation that
# re-derives rates from summed tokens must group by these, or its
# per-component breakdown drifts from the stored per-record cost.
RATE_EPOCHS: list[datetime] = sorted(
    {end for windows in DATED_RATES.values() for end, _ in windows}
)

# Family fallbacks for unrecognised Claude models — current-generation
# rates for the tier, at LIST price (never a dated promotion).
_TIER_FALLBACKS: tuple[tuple[re.Pattern, dict], ...] = (
    (re.compile(r"fable|mythos"), MODEL_RATES["claude-fable-5"]),
    (re.compile(r"opus"), MODEL_RATES["claude-opus-4-8"]),
    (re.compile(r"sonnet"), MODEL_RATES["claude-sonnet-5"]),
    (re.compile(r"haiku"), MODEL_RATES["claude-haiku-4-5"]),
)

# A dated snapshot suffix ("-20250514") is the same model; a short version
# suffix ("-9") or a mode suffix ("-fast") is a DIFFERENT model.
_SNAPSHOT_SUFFIX = re.compile(r"^-?\d{6,8}$")


@dataclass(frozen=True)
class Resolution:
    """Outcome of resolving a model id to rates.

    kind: "exact" | "tier" | "default". Anything other than "exact" means
    the figure is an estimate and should be surfaced as such.
    """
    rates: dict
    kind: str
    key: str | None = None

    @property
    def estimated(self) -> bool:
        return self.kind != "exact"


def _normalise(model: str) -> str:
    """Strip provider/region prefixes and normalise version separators.

    ``anthropic/claude-opus-4.8`` and ``us.anthropic.claude-opus-4-8``
    both denote the same model as ``claude-opus-4-8``.
    """
    m = (model or "").strip().lower()
    if not m:
        return ""
    # Everything before the first "claude" is provider/region routing.
    i = m.find("claude")
    if i > 0:
        m = m[i:]
    return m.replace(".", "-")


def _match_key(norm: str) -> str | None:
    for key in MODEL_RATES:
        if not norm.startswith(key):
            continue
        rest = norm[len(key):]
        if rest == "" or rest[0] in "[@" or _SNAPSHOT_SUFFIX.match(rest):
            return key
    return None


def _dated(key: str, ts: datetime | None) -> dict:
    windows = DATED_RATES.get(key)
    if not windows or ts is None:
        return MODEL_RATES[key]
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    for end_exclusive, rates in windows:
        if ts < end_exclusive:
            return rates
    return MODEL_RATES[key]


def resolve(model: str, ts: datetime | None = None) -> Resolution:
    """Resolve a model id to rates, reporting how confident the match is."""
    norm = _normalise(model)
    key = _match_key(norm)
    if key is not None:
        return Resolution(_dated(key, ts), "exact", key)
    for pattern, rates in _TIER_FALLBACKS:
        if pattern.search(norm):
            return Resolution(rates, "tier")
    return Resolution(DEFAULT_RATES, "default")


def rate_for(model: str, ts: datetime | None = None) -> dict:
    """Rates for a model at a point in time. Omitting ts yields list price."""
    return resolve(model, ts).rates


def compute_cost(
    model: str,
    *,
    fresh: int,
    output: int,
    eph5: int,
    eph1h: int,
    unsplit_create: int,
    read: int,
    ts: datetime | None = None,
) -> float:
    """USD cost for one request's token tally.

    unsplit_create = max(0, cache_creation_input_tokens - eph5 - eph1h);
    must already be computed by the caller. Pass the record's own
    timestamp so dated rates apply to when the tokens were spent.
    """
    r = rate_for(model, ts)
    return (
        fresh * r["fresh"] / 1_000_000
        + (eph5 + unsplit_create) * r["create_5m"] / 1_000_000
        + eph1h * r["create_1h"] / 1_000_000
        + read * r["read"] / 1_000_000
        + output * r["output"] / 1_000_000
    )
