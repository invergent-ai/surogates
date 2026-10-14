"""The channel notice a refused turn sends, in the business's wording."""

from surogates.api.routes._commerce_turn import limit_notice


def test_spent_free_plan_asks_for_an_upgrade_with_the_link() -> None:
    assert limit_notice("upgrade_required", "https://buy/x") == (
        "You have used this month's free allowance. Upgrade to keep chatting. "
        "Get more access here: https://buy/x"
    )


def test_platform_wording_is_unchanged_without_overrides() -> None:
    assert limit_notice("allowance_exhausted", None) == (
        "You've reached your usage limit for this assistant. Please try again later."
    )


def test_business_wording_replaces_each_key() -> None:
    texts = {
        "plan_required_notice": "Alegeți un plan pentru a continua.",
        "plan_link": "Planuri: {link}",
    }
    assert limit_notice("subscription_required", "https://b", texts) == (
        "Alegeți un plan pentru a continua. Planuri: https://b"
    )


def test_broken_business_wording_falls_back() -> None:
    assert limit_notice("upgrade_required", "https://b", {"plan_link": "{nope}"}).endswith(
        "Get more access here: https://b",
    )


def test_stranger_is_offered_the_free_plan_with_the_link() -> None:
    assert limit_notice("free_plan_available", "https://buy/x") == (
        "Choose a plan to keep chatting with this assistant; there is a free one. "
        "Start free here: https://buy/x"
    )
