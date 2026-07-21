"""cost_buckets is a decomposition of cost_total, so it must be computed
per rate epoch. With dated rates, re-deriving one rate for a range that
straddles a cutover makes the buckets disagree with the authoritative
SUM(cost_usd) they claim to decompose.
"""
from datetime import datetime, timezone

import pytest

from backend import api, pricing

UTC = timezone.utc


def _row(model, epoch, fresh=0, cc=0, cr=0, output=0, eph5=0, eph1h=0, cost=0.0):
    # (model, rate_epoch, turns, fresh, cache_create, cache_read,
    #  output, eph5, eph1h, cost_total)
    return (model, epoch, 1, fresh, cc, cr, output, eph5, eph1h, cost)


def test_buckets_sum_to_total_within_a_single_epoch():
    rows = [_row("claude-opus-4-8", 0, fresh=1_000_000, cost=5.00)]
    out = api.fold_per_model(rows)
    assert len(out) == 1
    m = out[0]
    assert m["cost_total"] == pytest.approx(5.00)
    assert sum(m["cost_buckets"].values()) == pytest.approx(m["cost_total"])


def test_buckets_sum_to_total_across_a_dated_rate_cutover():
    # 1M input tokens before the sonnet-5 cutover ($2.00) and 1M after
    # ($3.00). Stored cost_total is authoritative at $5.00 total.
    rows = [
        _row("claude-sonnet-5", 0, fresh=1_000_000, cost=2.00),
        _row("claude-sonnet-5", 1, fresh=1_000_000, cost=3.00),
    ]
    out = api.fold_per_model(rows)
    assert len(out) == 1, "epochs must fold into one row per model"
    m = out[0]
    assert m["fresh"] == 2_000_000
    assert m["turns"] == 2
    assert m["cost_total"] == pytest.approx(5.00)
    assert sum(m["cost_buckets"].values()) == pytest.approx(5.00)
    assert m["cost_buckets"]["fresh"] == pytest.approx(5.00)


def test_epoch_index_selects_the_rate_in_force_for_that_window():
    intro = api.epoch_ts(0)
    listed = api.epoch_ts(1)
    assert pricing.rate_for("claude-sonnet-5", intro)["fresh"] == 2.00
    assert pricing.rate_for("claude-sonnet-5", listed)["fresh"] == 3.00


def test_epoch_sql_expression_has_one_case_per_boundary():
    expr, params = api.rate_epoch_sql("ts")
    assert len(params) == len(pricing.RATE_EPOCHS)
    assert expr.count("CASE") == len(pricing.RATE_EPOCHS)
