"""MODEL_RATES is the single source of truth for cost in this repo.
Mirrors parse_session.py:1148-1166 — the canonical table at the time of
the spec freeze. If the canonical bumps, bump PARSER_VERSION here.
"""
from datetime import datetime, timezone

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


# --- Claude Sonnet 5: flat pricing, no dated window ------------------------
# The 2.00/10.00 launch price was announced as introductory through
# 2026-08-31, but Anthropic made it the standard price and cancelled the
# 2026-09-01 rise to 3.00/15.00. There is no cutover to price around.

UTC = timezone.utc


def test_sonnet_5_rates_are_flat_2_10():
    want = {
        "fresh": 2.00, "create_5m": 2.50, "create_1h": 4.00,
        "read": 0.20, "output": 10.00,
    }
    assert pricing.rate_for("claude-sonnet-5") == want
    # Same on both sides of the cancelled cutover, and with no timestamp.
    assert pricing.rate_for("claude-sonnet-5", ts=datetime(2026, 7, 21, tzinfo=UTC)) == want
    assert pricing.rate_for("claude-sonnet-5", ts=datetime(2026, 9, 1, tzinfo=UTC)) == want
    assert pricing.rate_for("claude-sonnet-5", ts=datetime(2027, 1, 1, tzinfo=UTC)) == want


def _sonnet_5_cost_per_mtok_input(ts=None) -> float:
    return pricing.compute_cost(
        "claude-sonnet-5", fresh=1_000_000, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0, ts=ts,
    )


def test_compute_cost_for_sonnet_5_is_timestamp_independent():
    assert _sonnet_5_cost_per_mtok_input() == 2.00
    assert _sonnet_5_cost_per_mtok_input(datetime(2026, 7, 21, tzinfo=UTC)) == 2.00
    assert _sonnet_5_cost_per_mtok_input(datetime(2026, 9, 1, tzinfo=UTC)) == 2.00


def test_no_model_currently_carries_a_dated_window():
    # Guards against a stale promotion outliving its announcement.
    assert pricing.DATED_RATES == {}
    assert pricing.RATE_EPOCHS == []


# --- dated-rate machinery (exercised via a synthetic window) ---------------
# SV-DATED-RATES outlives any individual promotion, so these drive the code
# path through conftest's synthetic_dated_rate rather than a live entry.


def test_dated_window_applies_before_its_cutover(synthetic_dated_rate):
    w = synthetic_dated_rate
    assert pricing.rate_for(w.model, ts=datetime(2026, 7, 21, tzinfo=UTC)) == w.before


def test_list_rates_apply_from_the_cutover(synthetic_dated_rate):
    w = synthetic_dated_rate
    assert pricing.rate_for(w.model, ts=w.cutover) == w.after


def test_dated_window_boundary_is_exclusive_at_the_cutover(synthetic_dated_rate):
    w = synthetic_dated_rate
    last = pricing.rate_for(w.model, ts=datetime(2026, 8, 31, 23, 59, 59, tzinfo=UTC))
    assert last == w.before
    assert pricing.rate_for(w.model, ts=datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)) == w.after


def test_omitting_ts_yields_list_price_never_the_discount(synthetic_dated_rate):
    # Conservative: an unknown timestamp must never silently apply a promo.
    assert pricing.rate_for(synthetic_dated_rate.model) == synthetic_dated_rate.after


def test_dated_window_does_not_leak_to_other_models(synthetic_dated_rate):
    assert synthetic_dated_rate.model not in ("claude-opus-4-8", "claude-fable-5",
                                              "claude-haiku-4-5")
    for m in ("claude-opus-4-8", "claude-fable-5", "claude-haiku-4-5"):
        during = pricing.rate_for(m, ts=datetime(2026, 7, 21, tzinfo=UTC))
        after = pricing.rate_for(m, ts=datetime(2026, 9, 1, tzinfo=UTC))
        assert during == after == pricing.rate_for(m)


def test_tier_fallback_never_inherits_a_dated_promotion(synthetic_dated_rate):
    # An unrecognised sonnet falls back to Sonnet 5's LIST rates, not its
    # promotional ones, even inside the window.
    r = pricing.resolve("claude-sonnet-9", ts=datetime(2026, 7, 21, tzinfo=UTC))
    assert r.kind == "tier"
    assert r.rates == synthetic_dated_rate.after


def test_rate_epochs_are_exposed_sorted_for_read_time_grouping(synthetic_dated_rate):
    assert pricing.RATE_EPOCHS == [synthetic_dated_rate.cutover]


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
    # Current-generation Sonnet rates, whatever they are today.
    assert res.rates == pricing.MODEL_RATES["claude-sonnet-5"]


def test_resolve_reports_default_for_wholly_unknown_model():
    assert pricing.resolve("gpt-5").kind == "default"


def test_fast_variants_are_not_silently_billed_at_standard_rates():
    # Fast mode is premium-priced and we have no published rate for it;
    # resolve must flag it rather than pass it off as an exact match.
    assert pricing.resolve("claude-opus-4-8-fast").kind != "exact"
