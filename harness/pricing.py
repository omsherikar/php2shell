"""Per-model prices (USD per million tokens) for cost estimation and the cap.

Source: the Anthropic model pricing table. Cache reads are ~0.1x input, cache
writes ~1.25x input. These drive run.py's spend estimate and the hard cap — they
are estimates for budgeting, not billing. Update if prices change.
"""

from __future__ import annotations

# (input_per_mtok, output_per_mtok)
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.0, 5.0),
}

_CACHE_READ_MULT = 0.1
_CACHE_WRITE_MULT = 1.25


def usd_for_usage(model: str, usage: dict) -> float:
    """Estimate USD for one call/trial from a usage dict.

    usage keys: input_tokens, output_tokens, cache_creation_input_tokens,
    cache_read_input_tokens. Unknown model -> 0.0 (and run.py warns).
    """
    if model not in PRICES:
        return 0.0
    in_rate, out_rate = PRICES[model]
    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    cw = usage.get("cache_creation_input_tokens", 0)
    cr = usage.get("cache_read_input_tokens", 0)
    dollars = (
        inp * in_rate
        + out * out_rate
        + cw * in_rate * _CACHE_WRITE_MULT
        + cr * in_rate * _CACHE_READ_MULT
    ) / 1_000_000
    return dollars
