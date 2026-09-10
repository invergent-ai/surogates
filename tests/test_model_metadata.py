"""Catalog invariants for :mod:`surogates.harness.model_metadata`.

The module docstring already claims these are pinned; until now nothing
checked them. An alias pointing at a missing catalog key resolves to
``None`` and the caller silently falls back to a default context window
and zero pricing, with no error anywhere -- exactly the failure mode
:func:`estimate_call_cost` was written to stop hiding.
"""

from __future__ import annotations

from surogates.harness.model_metadata import (
    _ALIASES,
    MODEL_CATALOG,
    estimate_call_cost,
    get_model_info,
)


def test_every_alias_resolves_to_a_catalog_entry() -> None:
    dangling = {a: t for a, t in _ALIASES.items() if t not in MODEL_CATALOG}
    assert not dangling, f"aliases point at missing catalog keys: {dangling}"


def test_catalog_ids_match_their_keys() -> None:
    mismatched = {k: v.id for k, v in MODEL_CATALOG.items() if v.id != k}
    assert not mismatched, f"catalog id/key mismatch: {mismatched}"


def test_tier_sentinels_carry_no_rate() -> None:
    # Pricing a sentinel would bill every platform session at the wrong
    # rate; estimate_call_cost must fall through to the served model.
    for sentinel in ("surogate", "surogate-pro"):
        info = get_model_info(sentinel)
        assert info is not None
        assert info.input_cost_per_1k == 0.0
        assert info.output_cost_per_1k == 0.0


def test_sentinel_session_prices_against_the_served_model() -> None:
    # What the base tier resolves to in PROD: the sentinel rides the
    # request, OpenRouter reports the concrete model back in usage.
    cost, priced = estimate_call_cost(
        "surogate", "deepseek/deepseek-v4.1-flash", 1_000, 1_000,
    )
    assert priced == "deepseek/deepseek-v4.1-flash"
    assert cost > 0
