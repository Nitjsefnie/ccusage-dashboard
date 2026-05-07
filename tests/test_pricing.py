"""MODEL_RATES is the single source of truth for cost in this repo.
Mirrors parse_session.py:1148-1166 — the canonical table at the time of
the spec freeze. If the canonical bumps, bump PARSER_VERSION here.
"""
from backend import pricing


def test_opus_4_7_rates_match_canonical():
    r = pricing.rate_for("claude-opus-4-7")
    assert r == {
        "fresh": 5.00, "create_5m": 6.25, "create_1h": 10.00,
        "read": 0.50, "output": 25.00,
    }


def test_sonnet_4_5_rates():
    r = pricing.rate_for("claude-sonnet-4-5")
    assert r["fresh"] == 3.00 and r["create_5m"] == 3.75


def test_haiku_4_5_rates():
    r = pricing.rate_for("claude-haiku-4-5")
    assert r["fresh"] == 1.00 and r["read"] == 0.10


def test_unknown_model_falls_back_to_default():
    r = pricing.rate_for("claude-zzzz-9999")
    assert r == pricing.DEFAULT_RATES


def test_substring_order_does_not_misroute_4_7_to_4():
    r47 = pricing.rate_for("claude-opus-4-7")
    r4 = pricing.rate_for("claude-opus-4")
    assert r47["fresh"] == 5.00
    assert r4["fresh"] == 15.00


def test_compute_cost_split_known_vector():
    cost = pricing.compute_cost(
        "claude-opus-4-7",
        fresh=1_000_000, output=0, eph5=0, eph1h=0, unsplit_create=0, read=0,
    )
    assert cost == 5.00


def test_unsplit_cache_charges_at_5m_rate():
    cost = pricing.compute_cost(
        "claude-sonnet-4-5",
        fresh=0, output=0, eph5=0, eph1h=0, unsplit_create=1_000_000, read=0,
    )
    assert cost == 3.75   # NOT 6.00 (1h rate)


def test_split_cache_charges_each_bucket_separately():
    cost = pricing.compute_cost(
        "claude-sonnet-4-5",
        fresh=0, output=0,
        eph5=1_000_000, eph1h=1_000_000,
        unsplit_create=0, read=0,
    )
    # 1M @ 3.75 + 1M @ 6.00
    assert cost == 9.75
