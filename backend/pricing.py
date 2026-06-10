"""Per-model cost rates (USD per million tokens).

SINGLE SOURCE OF TRUTH for cost in ccudash. Mirrors
parse_session.py:1148-1166 (the canonical at the time of the spec freeze).
Bump PARSER_VERSION when this table changes — every session reparses.

Cache writes are split by TTL:
  5m write = 1.25× base input (column 'create_5m')
  1h write = 2× base input    (column 'create_1h')

Tokens recorded as cache_creation_input_tokens with NO ephemeral_5m/1h
split (older SDK versions) are charged at the 5m rate — conservative
undercount, not overcount. See SV-COST-SPLIT in
.claude/rules/ccudash-doctrine.md.

Order matters: more-specific substrings first so a model id like
'claude-opus-4-7' doesn't misroute to 'claude-opus-4'.
"""
from __future__ import annotations


# Order: most-specific first.
MODEL_RATES = {
    "claude-fable-5":    {"fresh": 10.00, "create_5m": 12.50, "create_1h": 20.00, "read": 1.00, "output": 50.00},
    "claude-opus-4-8":   {"fresh": 5.00,  "create_5m": 6.25,  "create_1h": 10.00, "read": 0.50, "output": 25.00},
    "claude-opus-4-7":   {"fresh": 5.00,  "create_5m": 6.25,  "create_1h": 10.00, "read": 0.50, "output": 25.00},
    "claude-opus-4-6":   {"fresh": 5.00,  "create_5m": 6.25,  "create_1h": 10.00, "read": 0.50, "output": 25.00},
    "claude-opus-4-5":   {"fresh": 5.00,  "create_5m": 6.25,  "create_1h": 10.00, "read": 0.50, "output": 25.00},
    "claude-opus-4-1":   {"fresh": 15.00, "create_5m": 18.75, "create_1h": 30.00, "read": 1.50, "output": 75.00},
    "claude-opus-4":     {"fresh": 15.00, "create_5m": 18.75, "create_1h": 30.00, "read": 1.50, "output": 75.00},
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


def rate_for(model: str) -> dict:
    if not model:
        return DEFAULT_RATES
    for key, rates in MODEL_RATES.items():
        if key in model:
            return rates
    return DEFAULT_RATES


def compute_cost(
    model: str,
    *,
    fresh: int,
    output: int,
    eph5: int,
    eph1h: int,
    unsplit_create: int,
    read: int,
) -> float:
    """USD cost for one request's token tally.

    unsplit_create = max(0, cache_creation_input_tokens - eph5 - eph1h);
    must already be computed by the caller.
    """
    r = rate_for(model)
    return (
        fresh * r["fresh"] / 1_000_000
        + (eph5 + unsplit_create) * r["create_5m"] / 1_000_000
        + eph1h * r["create_1h"] / 1_000_000
        + read * r["read"] / 1_000_000
        + output * r["output"] / 1_000_000
    )
