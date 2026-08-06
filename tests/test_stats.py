import math

import pytest

from pinpoint.stats import (
    ACCEPT_H0,
    ACCEPT_H1,
    CONTINUE,
    SPRT,
    benjamini_hochberg,
    two_proportion_pvalue,
    wilson_interval,
)


class TestWilson:
    def test_zero_trials_is_vacuous(self):
        assert wilson_interval(0, 0) == (0.0, 1.0)

    def test_contains_point_estimate(self):
        lo, hi = wilson_interval(2, 10)
        assert lo < 0.2 < hi

    def test_more_trials_narrower(self):
        lo1, hi1 = wilson_interval(1, 5)
        lo2, hi2 = wilson_interval(20, 100)
        assert (hi2 - lo2) < (hi1 - lo1)

    def test_bounds_clamped(self):
        lo, hi = wilson_interval(0, 10)
        assert lo == 0.0
        lo, hi = wilson_interval(10, 10)
        assert hi == 1.0


class TestSPRT:
    def test_flaky_accepted_quickly(self):
        sprt = SPRT(p0=0.01, p1=0.10)
        assert sprt.record(True) == CONTINUE
        assert sprt.record(True) == ACCEPT_H1

    def test_stable_accepted_eventually(self):
        sprt = SPRT(p0=0.01, p1=0.10, max_trials=100)
        decision = CONTINUE
        n = 0
        while decision == CONTINUE:
            decision = sprt.record(False)
            n += 1
        assert decision == ACCEPT_H0
        assert n < 40  # far fewer than a fixed-N=100 design

    def test_cap_decides_by_mle(self):
        sprt = SPRT(p0=0.10, p1=0.50, alpha=0.3, beta=0.3, max_trials=4)
        for outcome in (True, False, True, False):
            decision = sprt.record(outcome)
        assert decision == ACCEPT_H1  # rate 0.5 >= midpoint 0.3

    def test_asymmetric_thresholds(self):
        # tiny beta makes accepting H0 need much more evidence than H1
        sprt = SPRT(p0=0.02, p1=0.50, alpha=0.10, beta=0.02, max_trials=100)
        h1_after_one_failure = sprt.record(True)
        assert h1_after_one_failure == ACCEPT_H1

        sprt = SPRT(p0=0.02, p1=0.50, alpha=0.10, beta=0.02, max_trials=100)
        n = 0
        decision = CONTINUE
        while decision == CONTINUE:
            decision = sprt.record(False)
            n += 1
        assert decision == ACCEPT_H0
        assert n >= 4  # several clean passes required to discard


class TestTwoProportion:
    def test_identical_rates_not_significant(self):
        assert two_proportion_pvalue(5, 10, 5, 10) == pytest.approx(1.0)

    def test_clear_shift_significant(self):
        assert two_proportion_pvalue(9, 10, 0, 10) < 0.001

    def test_degenerate_pool(self):
        assert two_proportion_pvalue(0, 10, 0, 10) == 1.0
        assert two_proportion_pvalue(10, 10, 10, 10) == 1.0

    def test_two_sided(self):
        # a drop is as significant as a rise
        assert two_proportion_pvalue(0, 10, 9, 10) == pytest.approx(
            two_proportion_pvalue(9, 10, 0, 10)
        )


class TestBenjaminiHochberg:
    def test_empty(self):
        assert benjamini_hochberg([]) == []

    def test_all_tiny_all_rejected(self):
        assert benjamini_hochberg([0.001, 0.002, 0.003]) == [True, True, True]

    def test_all_large_none_rejected(self):
        assert benjamini_hochberg([0.5, 0.9, 0.7]) == [False, False, False]

    def test_stepup(self):
        # classic: rank cutoff includes smaller p-values below the max rank
        pvals = [0.01, 0.02, 0.03, 0.9]
        assert benjamini_hochberg(pvals, q=0.05) == [True, True, True, False]
