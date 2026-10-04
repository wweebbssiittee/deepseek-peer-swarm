from copy import deepcopy
from decimal import Decimal, localcontext

import pytest

from swarm.billing import NUSD_PER_USD, cost_usage, estimate_cost, estimate_tokens, is_peak, rates_at, new_observations, observe, parse_budget, pricing_for, reserve_cost


@pytest.mark.parametrize("model,rates", [
    ("deepseek-flash", (6, 300, 1200)),
    ("deepseek-v4-flash", (6, 300, 1200)),
    ("deepseek-v4-flash-vision-exp", (6, 300, 1200)),
    ("deepseek-v4-pro", (44, 1320, 3960)),
])
def test_verified_price_snapshots_are_exact_and_independent(model, rates):
    pricing = pricing_for(model)
    assert pricing["model"] == model
    assert pricing["basis"] == "time_of_day" and pricing["verified_at"] == "2026-09-23"
    assert pricing["source"] == "https://api-docs.deepseek.com/quick_start/pricing/"
    assert tuple(pricing[key] for key in ("cached_input_nusd", "input_nusd", "output_nusd")) == rates
    for label, rate in zip(("cached_input_per_million_usd", "input_per_million_usd", "output_per_million_usd"), rates):
        assert Decimal(pricing[label]) * NUSD_PER_USD == rate * 1_000_000
    pricing["input_nusd"] = 0
    assert pricing_for(model)["input_nusd"] == rates[1]


@pytest.mark.parametrize("model", ["unknown", "deepseek-chat", "deepseek-reasoner", "deepseek-v4-pro-free", "", None, [], True])
def test_unverified_model_never_gets_a_free_price(model):
    with pytest.raises(ValueError, match="verified USD pricing"):
        pricing_for(model)


@pytest.mark.parametrize("value,expected", [
    ("0.01", 10_000_000), (0.01, 10_000_000), (1, NUSD_PER_USD),
    ("10.25", 10_250_000_000), (Decimal("100000.00"), 100_000_000_000_000),
    (100000, 100_000_000_000_000), (" 2.50 ", 2_500_000_000),
])
def test_budget_amounts_are_exact(value, expected):
    assert parse_budget(value) == expected
    assert type(parse_budget(value)) is int


@pytest.mark.parametrize("value", [
    True, False, None, {}, [], "", "abc", "-1", 0, "0.009", "100000.01",
    "1.001", "1.000", Decimal("1.2300"), 0.1 + 0.2,
    float("nan"), float("inf"), float("-inf"), Decimal("NaN"), Decimal("sNaN"),
    "Infinity", "NaN", "1e999999", "1e-999999",
])
def test_invalid_budget_is_rejected_without_rounding(value):
    with pytest.raises(ValueError):
        parse_budget(value)


def test_budget_conversion_ignores_process_decimal_rounding_context():
    with localcontext() as context:
        context.prec = 2
        assert parse_budget("12345.67") == 12_345_670_000_000


def valid_usage():
    return {
        "prompt_tokens": 1000, "completion_tokens": 250, "total_tokens": 1250,
        "prompt_cache_hit_tokens": 800, "prompt_cache_miss_tokens": 200,
        "prompt_tokens_details": {"cached_tokens": 800},
        "completion_tokens_details": {"reasoning_tokens": 200},
    }


def test_cached_cost_and_reasoning_are_counted_once():
    usage = valid_usage()
    before = deepcopy(usage)
    result = cost_usage(usage, pricing_for("deepseek-flash"))
    assert result == {
        "cost_nusd": 364_800, "prompt_tokens": 1000, "completion_tokens": 250,
        "cached_tokens": 800, "uncached_tokens": 200, "cache_known": True,
    }
    assert usage == before
    assert cost_usage(usage, pricing_for("deepseek-v4-pro"))["cost_nusd"] == 1_289_200


def test_absent_cache_details_use_the_full_miss_ceiling():
    usage = {"prompt_tokens": 1000, "completion_tokens": 250, "total_tokens": 1250}
    result = cost_usage(usage, pricing_for("deepseek-flash"))
    assert result["cost_nusd"] == 600_000
    assert result["cached_tokens"] == 0 and result["uncached_tokens"] == 1000
    assert result["cache_known"] is False
    assert result["cost_nusd"] == reserve_cost(1000, 250, pricing_for("deepseek-flash"))


def test_documented_nested_cache_alias_is_accepted_and_cross_checked():
    usage = {"prompt_tokens": 1000, "completion_tokens": 250, "total_tokens": 1250, "prompt_tokens_details": {"cached_tokens": 800}}
    assert cost_usage(usage, pricing_for("deepseek-flash"))["cost_nusd"] == 364_800
    usage.update(prompt_cache_hit_tokens=700, prompt_cache_miss_tokens=300)
    assert cost_usage(usage, pricing_for("deepseek-flash")) is None


@pytest.mark.parametrize("patch", [
    {"total_tokens": 1249}, {"total_tokens": None}, {"total_tokens": True},
    {"prompt_tokens": 1000.0}, {"completion_tokens": "250"}, {"completion_tokens": -1},
    {"prompt_cache_hit_tokens": 801}, {"prompt_cache_hit_tokens": False},
    {"prompt_cache_miss_tokens": -1}, {"prompt_cache_miss_tokens": None},
    {"prompt_tokens_details": None}, {"prompt_tokens_details": []},
    {"prompt_tokens_details": {"cached_tokens": 1001}},
    {"prompt_tokens_details": {"cached_tokens": "800"}},
    {"completion_tokens_details": None},
    {"completion_tokens_details": {"reasoning_tokens": 251}},
    {"completion_tokens_details": {"reasoning_tokens": True}},
])
def test_malformed_usage_keeps_the_reservation(patch):
    usage = {**valid_usage(), **patch}
    assert cost_usage(usage, pricing_for("deepseek-flash")) is None


@pytest.mark.parametrize("key", ["prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"])
def test_missing_required_usage_or_partial_cache_counts_are_not_settled(key):
    usage = valid_usage()
    usage.pop(key)
    assert cost_usage(usage, pricing_for("deepseek-flash")) is None


@pytest.mark.parametrize("usage", [None, [], "usage", {}, {"total_tokens": 100}])
def test_absent_usage_never_becomes_zero_cost(usage):
    assert cost_usage(usage, pricing_for("deepseek-flash")) is None


def test_zero_usage_is_valid_only_when_complete_and_consistent():
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    assert cost_usage(usage, pricing_for("deepseek-flash"))["cost_nusd"] == 0
    usage["prompt_cache_hit_tokens"] = 1
    usage["prompt_cache_miss_tokens"] = 0
    assert cost_usage(usage, pricing_for("deepseek-flash")) is None


def test_reservations_bound_all_valid_cache_splits_and_output_lengths():
    for model in ("deepseek-flash", "deepseek-v4-pro"):
        pricing = pricing_for(model)
        ceiling = reserve_cost(10_000, 8192, pricing)
        for hit in (0, 1, 5000, 10_000):
            for output in (0, 1, 4096, 8192):
                usage = {"prompt_tokens": 10_000, "completion_tokens": output, "total_tokens": 10_000 + output,
                         "prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": 10_000 - hit}
                assert cost_usage(usage, pricing)["cost_nusd"] <= ceiling


def test_large_counts_do_not_overflow_or_round_through_floats():
    prompt, output = 10**20 + 1, 10**18 + 1
    pricing = pricing_for("deepseek-v4-pro")
    usage = {"prompt_tokens": prompt, "completion_tokens": output, "total_tokens": prompt + output}
    expected = prompt * 1320 + output * 3960
    assert cost_usage(usage, pricing)["cost_nusd"] == expected
    assert reserve_cost(prompt, output, pricing) == expected


@pytest.mark.parametrize("input_tokens,output_tokens", [(True, 1), (1, False), (-1, 1), (1, -1), (1.0, 1), (1, "8192")])
def test_invalid_reservation_bounds_fail_closed(input_tokens, output_tokens):
    with pytest.raises(ValueError):
        reserve_cost(input_tokens, output_tokens, pricing_for("deepseek-flash"))


@pytest.mark.parametrize("rate", [0, -1, False, 300.0, "300", None])
def test_corrupt_snapshot_cannot_make_work_free(rate):
    pricing = pricing_for("deepseek-flash")
    pricing["input_nusd"] = rate
    with pytest.raises(ValueError):
        reserve_cost(1000, 8192, pricing)
    with pytest.raises(ValueError):
        cost_usage(valid_usage(), pricing)


def test_cold_start_assumes_the_cheapest_rate_the_account_could_pay():
    price = pricing_for("deepseek-flash")
    # Nothing observed yet: input is priced at the cached rate, not the miss rate.
    estimate = estimate_cost(4000, 8192, price, None)
    assert estimate == (4000 // 4) * 6 + (8192 // 4) * 1200
    assert estimate < reserve_worst_case(4000, 8192, price)


def reserve_worst_case(input_bound, output_bound, price):
    return input_bound * price["input_nusd"] + output_bound * price["output_nusd"]


def test_a_priced_call_replaces_the_cheapest_guess_with_the_observed_rate():
    price = pricing_for("deepseek-flash")
    seen = new_observations()
    cold = estimate_cost(4000, 8192, price, seen)
    # One real call: the byte bound over-counted tokens 4x, all input missed
    # cache, and the reply ran far shorter than the output bound.
    measured = cost_usage({"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200,
                           "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 1000}, price)
    observe(seen, 4000, measured, price)
    learned = estimate_cost(4000, 8192, price, seen)
    # Input now carries the observed cache-miss rate rather than the cached one,
    # and the forecast drops overall because real replies ran short of the bound.
    assert learned == 1000 * 300 + 200 * 1200
    assert learned < cold


def test_observed_cache_hits_lower_the_learned_input_rate():
    price = pricing_for("deepseek-flash")
    missed, hit = new_observations(), new_observations()
    for store, cached in ((missed, 0), (hit, 900)):
        measured = cost_usage({"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200,
                               "prompt_cache_hit_tokens": cached,
                               "prompt_cache_miss_tokens": 1000 - cached}, price)
        observe(store, 4000, measured, price)
    assert estimate_cost(4000, 8192, price, hit) < estimate_cost(4000, 8192, price, missed)


def test_learned_output_length_never_exceeds_the_hard_output_bound():
    price = pricing_for("deepseek-flash")
    seen = new_observations()
    measured = cost_usage({"prompt_tokens": 100, "completion_tokens": 50_000, "total_tokens": 50_100,
                           "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 100}, price)
    observe(seen, 4000, measured, price)
    # A long observed reply cannot push the forecast past what the run allows.
    assert estimate_cost(4000, 512, price, seen) == (4000 * 100 // 4000) * 300 + 512 * 1200


def test_estimates_reject_negative_bounds_and_tolerate_junk_observations():
    price = pricing_for("deepseek-flash")
    with pytest.raises(ValueError):
        estimate_cost(-1, 10, price, None)
    for junk in ("", [], {"calls": 3}, {"calls": 0, "bound_tokens": 5, "prompt_tokens": 5}):
        assert estimate_cost(4000, 8192, price, junk) == estimate_cost(4000, 8192, price, None)


def test_token_reservation_learns_the_same_ratios_as_the_cost_estimate():
    price = pricing_for("deepseek-flash")
    seen = new_observations()
    cold = estimate_tokens(4000, 8192, seen)
    assert cold == 4000 // 4 + 8192 // 4
    # A real call: the byte bound was ~4x the true prompt size and the reply
    # ran far shorter than the output allowance.
    measured = cost_usage({"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200,
                           "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 1000}, price)
    observe(seen, 4000, measured, price)
    assert estimate_tokens(4000, 8192, seen) == 1000 + 200
    # The hard output bound still caps a long observed reply.
    assert estimate_tokens(4000, 128, seen) == 1000 + 128


def test_token_reservation_rejects_negative_bounds_and_tolerates_junk():
    with pytest.raises(ValueError):
        estimate_tokens(-1, 10, None)
    for junk in ("", [], {"calls": 2}, {"calls": 0, "bound_tokens": 9, "prompt_tokens": 9}):
        assert estimate_tokens(4000, 8192, junk) == estimate_tokens(4000, 8192, None)


@pytest.mark.real_clock
@pytest.mark.parametrize("moment,expected", [
    # Published peak windows are 01:00-04:00 and 06:00-10:00 UTC, Mon-Fri.
    ("2026-09-23T01:00:00+00:00", True),    # Wednesday, window opens
    ("2026-09-23T03:59:00+00:00", True),
    ("2026-09-23T04:00:00+00:00", False),   # window closes exactly
    ("2026-09-23T05:59:00+00:00", False),
    ("2026-09-23T09:59:00+00:00", True),
    ("2026-09-23T10:00:00+00:00", False),   # the boundary this run crossed
    ("2026-09-23T00:30:00+00:00", False),
    ("2026-09-26T08:00:00+00:00", False),   # Saturday is never peak
    ("2026-09-27T02:00:00+00:00", False),   # Sunday is never peak
    ("2026-09-23T11:00:00+02:00", True),    # 09:00 UTC, offset respected
])
def test_peak_windows_follow_the_published_utc_schedule(moment, expected):
    assert is_peak(moment) is expected


@pytest.mark.real_clock
def test_unreadable_moments_bill_at_peak_rather_than_guessing_cheap():
    for junk in ("not-a-time", None if False else "", 17, [], {}):
        assert is_peak(junk) is True


@pytest.mark.real_clock
@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-pro"])
def test_offpeak_is_exactly_half_of_peak_with_no_rounding_loss(model):
    pricing = pricing_for(model)
    peak = rates_at(pricing, "2026-09-23T09:00:00+00:00")
    off = rates_at(pricing, "2026-09-23T10:00:00+00:00")
    assert all(p == o * 2 for p, o in zip(peak, off))
    assert all(isinstance(rate, int) and rate > 0 for rate in off)


@pytest.mark.real_clock
def test_snapshots_without_offpeak_rates_keep_billing_at_peak():
    """Ledgers written before off-peak pricing must not change value."""
    legacy = pricing_for("deepseek-flash")
    for key in ("offpeak_cached_input_nusd", "offpeak_input_nusd", "offpeak_output_nusd"):
        legacy.pop(key)
    assert rates_at(legacy, "2026-09-23T10:00:00+00:00") == (6, 300, 1200)


@pytest.mark.offpeak
def test_offpeak_calls_cost_half_of_the_same_peak_usage():
    pricing = pricing_for("deepseek-flash")
    usage = {"prompt_tokens": 1000, "completion_tokens": 250, "total_tokens": 1250,
             "prompt_cache_hit_tokens": 800, "prompt_cache_miss_tokens": 200}
    off = cost_usage(usage, pricing)["cost_nusd"]
    assert off == 800 * 3 + 200 * 150 + 250 * 600
    assert off * 2 == 800 * 6 + 200 * 300 + 250 * 1200
