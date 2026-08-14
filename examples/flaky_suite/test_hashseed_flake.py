"""Non-order-dependent flake: depends on the interpreter's hash seed.

Passes under PYTHONHASHSEED=0, ~50% of other seeds fail.
"""


def test_string_hash_parity_assumption():
    assert hash("whyflaky") % 2 == 1
