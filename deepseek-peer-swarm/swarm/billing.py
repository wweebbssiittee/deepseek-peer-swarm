"""Integer USD accounting against dated DeepSeek prices.

DeepSeek charges half price outside its peak windows, so every call is priced
at the rate in force when it was made. Chinese public holidays are also
off-peak but are not published as data, so they are billed here at the peak
rate; that overstates cost on those days rather than understating it.
Actual prices can change; each run must retain its own pricing snapshot.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

NUSD_PER_USD = 1_000_000_000
PRICING_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"
PRICING_VERIFIED_AT = "2026-09-23"
USAGE_SOURCE = "https://api-docs.deepseek.com/api/create-chat-completion/"

# At these published prices, every per-token rate is an exact integer nano-USD
# at both peak and off-peak, so halving never loses a fraction.
# Names below are the documented accepted names, not guessed model aliases.
_MODELS = {
    "deepseek-flash": ("0.006", "0.30", "1.20", 6, 300, 1200, 3, 150, 600),
    "deepseek-v4-flash": ("0.006", "0.30", "1.20", 6, 300, 1200, 3, 150, 600),
    "deepseek-v4-flash-vision-exp": ("0.006", "0.30", "1.20", 6, 300, 1200, 3, 150, 600),
    "deepseek-v4-pro": ("0.044", "1.32", "3.96", 44, 1320, 3960, 22, 660, 1980),
}

# Published peak windows, in UTC hours, Monday through Friday.
PEAK_WINDOWS = ((1, 4), (6, 10))


def is_peak(moment: Any = None) -> bool:
    """True when DeepSeek charges full rate at this instant."""
    if moment is None:
        moment = datetime.now(timezone.utc)
    elif isinstance(moment, str):
        try:
            moment = datetime.fromisoformat(moment)
        except ValueError:
            return True
    if not isinstance(moment, datetime):
        return True
    moment = moment.astimezone(timezone.utc) if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    if moment.weekday() >= 5:
        return False
    hour = moment.hour + moment.minute / 60
    return any(start <= hour < end for start, end in PEAK_WINDOWS)


def pricing_for(model: str) -> dict:
    """Return a fresh serializable price snapshot or reject an unknown model."""
    if not isinstance(model, str) or model not in _MODELS:
        raise ValueError("No verified USD pricing for this model; choose a supported DeepSeek model before starting a live run")
    (cached_usd, input_usd, output_usd, cached_nusd, input_nusd, output_nusd,
     off_cached_nusd, off_input_nusd, off_output_nusd) = _MODELS[model]
    return {
        "model": model,
        "source": PRICING_SOURCE,
        "verified_at": PRICING_VERIFIED_AT,
        "basis": "time_of_day",
        "peak_windows_utc": [list(window) for window in PEAK_WINDOWS],
        "cached_input_per_million_usd": cached_usd,
        "input_per_million_usd": input_usd,
        "output_per_million_usd": output_usd,
        "cached_input_nusd": cached_nusd,
        "input_nusd": input_nusd,
        "output_nusd": output_nusd,
        "offpeak_cached_input_nusd": off_cached_nusd,
        "offpeak_input_nusd": off_input_nusd,
        "offpeak_output_nusd": off_output_nusd,
    }


def rates_at(pricing: dict, moment: Any = None) -> tuple[int, int, int]:
    """The (cached, input, output) rates in force at moment.

    Snapshots written before off-peak pricing existed carry only peak rates;
    those keep billing at peak so historical ledgers never change value.
    """
    peak = _rates(pricing)
    if is_peak(moment):
        return peak
    off = tuple(pricing.get(key) for key in
                ("offpeak_cached_input_nusd", "offpeak_input_nusd", "offpeak_output_nusd"))
    if any(not _nonnegative_integer(rate) or rate == 0 for rate in off) or off[0] > off[1]:
        return peak
    return off  # type: ignore[return-value]


def parse_budget(value: Any) -> int:
    """Parse $0.01..$100000.00 with at most two decimal places into nano-USD.

    Decimal strings are preferred. JSON integers/floats and Decimal values are
    accepted without performing any binary floating-point money arithmetic.
    Extra decimal places, including explicitly supplied trailing zeros, fail
    validation instead of being rounded into a different authorization.
    """
    error = "USD budget must be between 0.01 and 100000.00 with at most two decimal places"
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(error)
    if isinstance(value, str) and (not value.strip() or len(value) > 80):
        raise ValueError(error)
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(error) from None
    if (not amount.is_finite() or amount < Decimal("0.01") or amount > Decimal("100000.00")
            or amount.as_tuple().exponent < -2):
        raise ValueError(error)
    numerator, denominator = amount.as_integer_ratio()
    return numerator * NUSD_PER_USD // denominator


def _nonnegative_integer(value: Any) -> bool:
    # bool subclasses int, but is never a valid token count or monetary rate.
    return type(value) is int and value >= 0


def _rates(pricing: dict) -> tuple[int, int, int]:
    if not isinstance(pricing, dict):
        raise ValueError("Invalid pricing snapshot")
    rates = tuple(pricing.get(key) for key in ("cached_input_nusd", "input_nusd", "output_nusd"))
    if any(not _nonnegative_integer(rate) or rate == 0 for rate in rates) or rates[0] > rates[1]:
        raise ValueError("Invalid pricing snapshot")
    return rates


def reserve_cost(input_bound_tokens: int, max_output_tokens: int, pricing: dict, moment: Any = None) -> int:
    """Worst-case USD reservation: uncached input plus bounded output."""
    if not _nonnegative_integer(input_bound_tokens) or not _nonnegative_integer(max_output_tokens):
        raise ValueError("Reservation token bounds must be nonnegative integers")
    _, input_rate, output_rate = rates_at(pricing, moment)
    return input_bound_tokens * input_rate + max_output_tokens * output_rate


def cost_usage(usage: Any, pricing: dict, moment: Any = None) -> dict | None:
    """Price consistent provider usage, or return None to retain the reservation.

    completion_tokens already includes reasoning_tokens; never add reasoning
    twice. Top-level cache hit/miss counts must arrive together and sum to the
    prompt total. The documented nested cached_tokens alias is also accepted,
    with cross-checks when both representations are supplied. Without cache
    counts the full prompt is conservatively priced as a cache miss.
    """
    cached_rate, input_rate, output_rate = rates_at(pricing, moment)
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    total = usage.get("total_tokens")
    if (not all(_nonnegative_integer(value) for value in (prompt, completion, total))
            or prompt + completion != total):
        return None

    details_cached = None
    if "prompt_tokens_details" in usage:
        details = usage["prompt_tokens_details"]
        if not isinstance(details, dict):
            return None
        if "cached_tokens" in details:
            details_cached = details["cached_tokens"]
            if not _nonnegative_integer(details_cached) or details_cached > prompt:
                return None

    if "completion_tokens_details" in usage:
        details = usage["completion_tokens_details"]
        if not isinstance(details, dict):
            return None
        if "reasoning_tokens" in details:
            reasoning = details["reasoning_tokens"]
            if not _nonnegative_integer(reasoning) or reasoning > completion:
                return None

    has_hit = "prompt_cache_hit_tokens" in usage
    has_miss = "prompt_cache_miss_tokens" in usage
    if has_hit or has_miss:
        if not (has_hit and has_miss):
            return None
        cached = usage["prompt_cache_hit_tokens"]
        uncached = usage["prompt_cache_miss_tokens"]
        if (not _nonnegative_integer(cached) or not _nonnegative_integer(uncached)
                or cached + uncached != prompt):
            return None
        if details_cached is not None and details_cached != cached:
            return None
        cache_known = True
    elif details_cached is not None:
        cached, uncached, cache_known = details_cached, prompt - details_cached, True
    else:
        cached, uncached, cache_known = 0, prompt, False

    return {
        "cost_nusd": cached * cached_rate + uncached * input_rate + completion * output_rate,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cached,
        "uncached_tokens": uncached,
        "cache_known": cache_known,
    }


# Cold-start shape assumptions, used only until a run settles its first priced
# call. They exist because a reservation needs token counts before any real
# usage exists; both are replaced by measured ratios on the first settlement.
BYTES_PER_TOKEN = 4          # Standard UTF-8 byte-to-token approximation.
COLD_START_OUTPUT_NUMERATOR = 1
COLD_START_OUTPUT_DENOMINATOR = 4   # Assume a quarter of the output bound.


def new_observations() -> dict:
    """Empty learned-shape accumulator for a fresh run."""
    return {"calls": 0, "bound_tokens": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "cached_tokens": 0, "uncached_tokens": 0}


def observe(observations: dict, input_bound_tokens: int, priced: dict, pricing: dict = None) -> None:
    """Fold one settled, priced call into the run's learned request shape."""
    observations["calls"] += 1
    observations["bound_tokens"] += input_bound_tokens
    observations["prompt_tokens"] += priced["prompt_tokens"]
    observations["completion_tokens"] += priced["completion_tokens"]
    observations["cached_tokens"] = observations.get("cached_tokens", 0) + priced["cached_tokens"]
    observations["uncached_tokens"] = observations.get("uncached_tokens", 0) + priced["uncached_tokens"]


def estimate_cost(input_bound_tokens: int, max_output_tokens: int, pricing: dict,
                  observations: Any = None, moment: Any = None) -> int:
    """Expected USD cost of one request, learned from this run's settled calls.

    Once a call has been priced, the run's own measured ratios drive the
    estimate: how much of the byte-derived bound became real prompt tokens,
    what those prompt tokens actually cost at the observed cache mix, and how
    long replies actually ran. Before any call has been priced, the cheapest
    rate the account could pay is assumed instead. This is a throughput
    estimate, not a ceiling; the budget is enforced on settled spend.
    """
    if not _nonnegative_integer(input_bound_tokens) or not _nonnegative_integer(max_output_tokens):
        raise ValueError("Estimate token bounds must be nonnegative integers")
    cached_rate, input_rate, output_rate = rates_at(pricing, moment)
    seen = observations if isinstance(observations, dict) else {}
    calls = seen.get("calls", 0)
    if (_nonnegative_integer(calls) and calls > 0 and seen.get("bound_tokens", 0) > 0
            and seen.get("prompt_tokens", 0) > 0 and "cached_tokens" in seen):
        prompt = input_bound_tokens * seen["prompt_tokens"] // seen["bound_tokens"]
        cached = prompt * seen["cached_tokens"] // seen["prompt_tokens"]
        input_cost = cached * cached_rate + (prompt - cached) * input_rate
        output = min(max_output_tokens, -(-seen["completion_tokens"] // calls))
        return input_cost + output * output_rate
    prompt = input_bound_tokens // BYTES_PER_TOKEN
    output = max_output_tokens * COLD_START_OUTPUT_NUMERATOR // COLD_START_OUTPUT_DENOMINATOR
    return prompt * cached_rate + output * output_rate


def estimate_tokens(input_bound_tokens: int, max_output_tokens: int, observations: Any = None) -> int:
    """Expected token count for one request, learned like the cost estimate.

    The caller's input bound is derived from UTF-8 bytes and overstates real
    tokens several times over. Once calls have settled, the measured ratio and
    the observed reply length replace that bound, so the token limit tracks
    real consumption instead of a worst case. The limit itself is still
    enforced on measured totals.
    """
    if not _nonnegative_integer(input_bound_tokens) or not _nonnegative_integer(max_output_tokens):
        raise ValueError("Estimate token bounds must be nonnegative integers")
    seen = observations if isinstance(observations, dict) else {}
    calls = seen.get("calls", 0)
    if (_nonnegative_integer(calls) and calls > 0 and seen.get("bound_tokens", 0) > 0
            and seen.get("prompt_tokens", 0) > 0):
        prompt = input_bound_tokens * seen["prompt_tokens"] // seen["bound_tokens"]
        output = min(max_output_tokens, -(-seen["completion_tokens"] // calls))
        return prompt + output
    return input_bound_tokens // BYTES_PER_TOKEN + max_output_tokens * COLD_START_OUTPUT_NUMERATOR // COLD_START_OUTPUT_DENOMINATOR
