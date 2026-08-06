"""The victim: passes alone, fails after test_billing has run."""
import os

from app import state


def test_default_currency_is_usd():
    assert state.CURRENCY == "USD"
    assert os.environ.get("APP_CURRENCY", "USD") == "USD"
    assert state.format_price(5) == "$5.00"
