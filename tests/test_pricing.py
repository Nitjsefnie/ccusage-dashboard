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


def test_fable_5_rates_are_double_opus_4x():
    f = pricing.rate_for("claude-fable-5")
    o = pricing.rate_for("claude-opus-4-8")
    assert f == {
        "fresh": 10.00, "create_5m": 12.50, "create_1h": 20.00,
        "read": 1.00, "output": 50.00,
    }
    assert all(f[k] == 2 * o[k] for k in f)
    # model ids carry suffixes like claude-fable-5[1m]
    assert pricing.rate_for("claude-fable-5[1m]") == f


def test_opus_4_8_does_not_misroute_to_legacy_opus_4():
    r = pricing.rate_for("claude-opus-4-8")
    assert r["fresh"] == 5.00 and r["output"] == 25.00


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


# --- dated rates: Claude Sonnet 5 introductory pricing ---------------------
# List price is 3.00/15.00; an introductory 2.00/10.00 applies through
# 2026-08-31 (UTC, inclusive). Cache tiers scale off input: 5m = 1.25x,
# 1h = 2x, read = 0.1x.

from datetime import datetime, timezone

UTC = timezone.utc


def test_sonnet_5_uses_intro_rates_during_intro_window():
    r = pricing.rate_for("claude-sonnet-5", ts=datetime(2026, 7, 21, tzinfo=UTC))
    assert r == {
        "fresh": 2.00, "create_5m": 2.50, "create_1h": 4.00,
        "read": 0.20, "output": 10.00,
    }


def test_sonnet_5_uses_list_rates_after_intro_window():
    r = pricing.rate_for("claude-sonnet-5", ts=datetime(2026, 9, 1, tzinfo=UTC))
    assert r == {
        "fresh": 3.00, "create_5m": 3.75, "create_1h": 6.00,
        "read": 0.30, "output": 15.00,
    }


def test_sonnet_5_intro_window_boundary_is_inclusive_through_aug_31():
    last = pricing.rate_for("claude-sonnet-5", ts=datetime(2026, 8, 31, 23, 59, 59, tzinfo=UTC))
    first = pricing.rate_for("claude-sonnet-5", ts=datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC))
    assert last["fresh"] == 2.00
    assert first["fresh"] == 3.00


def test_sonnet_5_without_ts_uses_list_price():
    # No timestamp => conservative default (list), never the discount.
    assert pricing.rate_for("claude-sonnet-5")["fresh"] == 3.00


def test_dated_rates_do_not_affect_other_models():
    for m in ("claude-opus-4-8", "claude-fable-5", "claude-haiku-4-5"):
        during = pricing.rate_for(m, ts=datetime(2026, 7, 21, tzinfo=UTC))
        after = pricing.rate_for(m, ts=datetime(2026, 9, 1, tzinfo=UTC))
        assert during == after == pricing.rate_for(m)


def test_compute_cost_honours_ts_for_sonnet_5():
    kw = dict(fresh=1_000_000, output=0, eph5=0, eph1h=0, unsplit_create=0, read=0)
    assert pricing.compute_cost("claude-sonnet-5", ts=datetime(2026, 7, 21, tzinfo=UTC), **kw) == 2.00
    assert pricing.compute_cost("claude-sonnet-5", ts=datetime(2026, 9, 1, tzinfo=UTC), **kw) == 3.00


def test_rate_epochs_are_exposed_sorted_for_read_time_grouping():
    assert pricing.RATE_EPOCHS == [datetime(2026, 9, 1, tzinfo=UTC)]


# --- resolution robustness -------------------------------------------------


def test_future_opus_does_not_inherit_legacy_opus_4_pricing():
    # 'claude-opus-4' is a prefix of 'claude-opus-4-9' but a naive substring
    # match would bill a future Opus at retired 15/75 rates.
    r = pricing.rate_for("claude-opus-4-9")
    assert r["fresh"] == 5.00 and r["output"] == 25.00


def test_dated_snapshot_still_matches_its_generic_key():
    assert pricing.rate_for("claude-opus-4-20250514")["fresh"] == 15.00
    assert pricing.rate_for("claude-haiku-4-5-20251001")["fresh"] == 1.00


def test_provider_prefixed_and_dotted_ids_normalise_to_the_exact_key():
    exact = pricing.rate_for("claude-opus-4-8")
    for variant in (
        "anthropic/claude-opus-4.8",
        "us.anthropic.claude-opus-4-8",
        "eu.anthropic.claude-opus-4-8",
        "CLAUDE-OPUS-4-8",
    ):
        assert pricing.rate_for(variant) == exact, variant


def test_resolve_reports_exact_match():
    assert pricing.resolve("claude-opus-4-8").kind == "exact"


def test_resolve_reports_tier_fallback_for_unknown_claude_model():
    res = pricing.resolve("claude-sonnet-6")
    assert res.kind == "tier"
    assert res.rates["fresh"] == 3.00


def test_resolve_reports_default_for_wholly_unknown_model():
    assert pricing.resolve("gpt-5").kind == "default"


def test_fast_variants_are_not_silently_billed_at_standard_rates():
    # Fast mode is premium-priced and we have no published rate for it;
    # resolve must flag it rather than pass it off as an exact match.
    assert pricing.resolve("claude-opus-4-8-fast").kind != "exact"
