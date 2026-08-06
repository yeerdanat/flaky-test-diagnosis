"""Non-order-dependent flake: unseeded randomness (~50% failure rate)."""
import random


def test_sampler_hits_upper_half():
    assert random.random() > 0.5
