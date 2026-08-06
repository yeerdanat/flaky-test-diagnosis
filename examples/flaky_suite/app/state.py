"""Shared application state — the pollution target."""

CURRENCY = "USD"


def format_price(amount: float) -> str:
    symbol = {"USD": "$", "EUR": "€"}[CURRENCY]
    return f"{symbol}{amount:.2f}"
