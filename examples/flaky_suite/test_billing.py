"""The polluter: mutates shared state and never restores it."""
import os

from app import state


def test_eur_invoice_formatting():
    state.CURRENCY = "EUR"           # pollutes module global
    os.environ["APP_CURRENCY"] = "EUR"  # pollutes the environment
    assert state.format_price(10) == "€10.00"
