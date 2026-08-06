"""Healthy tests — noise for the bisector to prune away."""
from app import state


def test_price_formatting_two_decimals():
    assert state.format_price(3.14159).endswith("3.14")


def test_arithmetic():
    assert 2 + 2 == 4


def test_string_ops():
    assert "flaky".upper() == "FLAKY"


def test_list_sorting():
    assert sorted([3, 1, 2]) == [1, 2, 3]
