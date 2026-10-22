"""minty_shared.money: the one rule for writing an amount (owner, 2026-10-21). Lifted with the
pure cases of minty-subscription-api's test_money_formatting.py; each service keeps its own tests
for resolving a currency's places from currency_info."""

from decimal import Decimal

import pytest

from minty_shared import money

NB = "\u00a0"


def test_the_label_is_the_trimmed_upper_case_code_and_empty_for_none():
    assert money.label("hkd") == "HKD"
    assert money.label(" jpy ") == "JPY"
    assert money.label(None) == ""
    assert money.label("") == ""


def test_the_code_is_joined_with_a_non_breaking_space():
    assert money.join("HKD", "400.00") == f"HKD{NB}400.00"
    assert money.join("", "400.00") == "400.00"
    assert money.NBSP == "\u00a0" and money.NBSP != " "


@pytest.mark.parametrize(
    ("minor", "places", "major"),
    [
        (28000, 2, Decimal("280")),
        (28000, 0, Decimal("28000")),
        (28000, 3, Decimal("28")),
        (None, 2, Decimal("0")),
    ],
)
def test_minor_units_scale_by_the_currency(minor, places, major):
    assert money.to_major(minor, places) == major


def test_format_minor_has_the_currencys_precision_groups_and_drops_the_sign():
    assert money.format_minor(28000, 2) == "280.00"
    assert money.format_minor(280, 0) == "280"
    assert money.format_minor(280000, 3) == "280.000"
    assert money.format_minor(-1234567, 2) == "12,345.67"
    assert money.format_minor(1234567, 0) == "1,234,567"


@pytest.mark.parametrize(
    ("amount", "places", "text"),
    [
        (Decimal("400"), 2, f"HKD{NB}400.00"),
        ("400.5", 2, f"HKD{NB}400.50"),
        (1200, 0, f"HKD{NB}1,200"),
        (Decimal("1234567.891"), 3, f"HKD{NB}1,234,567.891"),
    ],
)
def test_format_major_keeps_the_places(amount, places, text):
    assert money.format_major("HKD", amount, places) == text


@pytest.mark.parametrize(
    ("amount", "places", "text"),
    [
        (Decimal("400"), 2, f"HKD{NB}400"),
        (Decimal("400.00"), 2, f"HKD{NB}400"),
        (Decimal("400.5"), 2, f"HKD{NB}400.50"),
        (Decimal("12000"), 2, f"HKD{NB}12,000"),
        (1200, 0, f"HKD{NB}1,200"),
    ],
)
def test_format_price_drops_a_whole_point_zero_zero(amount, places, text):
    assert money.format_price("HKD", amount, places) == text


def test_format_amount_says_when_there_is_no_currency():
    assert money.format_amount("hkd", Decimal("1234.5")) == f"HKD{NB}1,234.50"
    assert money.format_amount(None, Decimal("12")) == "12.00 (No currency selected)"
    assert money.format_amount("JPY", 1200, 0) == f"JPY{NB}1,200"


def test_no_raw_non_breaking_space_in_the_source():
    # The rule is written only as the \u00a0 escape: a raw one is invisible in review.
    import minty_shared.money as mod

    assert "\u00a0" not in open(mod.__file__, encoding="utf-8").read()
