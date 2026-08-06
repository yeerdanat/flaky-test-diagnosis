"""Statistical primitives: Wilson intervals, SPRT, two-proportion tests, BH FDR.

Everything here is hand-rolled on the stdlib so the tool has no heavy
dependencies. Wilson + SPRT is deliberately the ceiling of sophistication
(see design doc §16: resist Bayesian rewrites).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

# SPRT decisions
CONTINUE = "continue"
ACCEPT_H0 = "accept_h0"  # stable (p <= p0)
ACCEPT_H1 = "accept_h1"  # flaky / triggers (p >= p1)


def wilson_interval(failures: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    1/5 and 20/100 are both "20%" and wildly different in confidence;
    this is the honest way to report a failure rate.
    """
    if n == 0:
        return (0.0, 1.0)
    p = failures / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


@dataclass
class SPRT:
    """Sequential probability ratio test for a Bernoulli failure rate.

    H0: p <= p0 (stable)   vs   H1: p >= p1 (flaky).

    alpha = P(accept H1 | H0 true)  — false "flaky/triggers" call.
    beta  = P(accept H0 | H1 true)  — false "stable/doesn't trigger" call.

    The caller sets alpha/beta *asymmetrically* where the error costs are
    asymmetric (the bisector: a false "doesn't trigger" prunes the true
    polluter and wrecks the whole bisection, so beta is tiny there).
    """

    p0: float = 0.01
    p1: float = 0.10
    alpha: float = 0.05
    beta: float = 0.10
    max_trials: int = 50

    n: int = field(default=0, init=False)
    failures: int = field(default=0, init=False)
    llr: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self._upper = math.log((1 - self.beta) / self.alpha)
        self._lower = math.log(self.beta / (1 - self.alpha))
        self._inc_fail = math.log(self.p1 / self.p0)
        self._inc_pass = math.log((1 - self.p1) / (1 - self.p0))

    def record(self, failed: bool) -> str:
        """Record one trial outcome; return CONTINUE / ACCEPT_H0 / ACCEPT_H1."""
        self.n += 1
        if failed:
            self.failures += 1
            self.llr += self._inc_fail
        else:
            self.llr += self._inc_pass
        if self.llr >= self._upper:
            return ACCEPT_H1
        if self.llr <= self._lower:
            return ACCEPT_H0
        if self.n >= self.max_trials:
            # Hard cap: decide by maximum likelihood (midpoint rule).
            rate = self.failures / self.n
            return ACCEPT_H1 if rate >= (self.p0 + self.p1) / 2 else ACCEPT_H0
        return CONTINUE

    @property
    def rate(self) -> float:
        return self.failures / self.n if self.n else 0.0


def _norm_sf(z: float) -> float:
    """Survival function of the standard normal, via erfc."""
    return 0.5 * math.erfc(z / math.sqrt(2))


def two_proportion_pvalue(f1: int, n1: int, f2: int, n2: int) -> float:
    """Two-sided two-proportion z-test p-value (pooled, normal approximation).

    Used to decide whether a perturbed dimension *shifted* the failure rate
    relative to control. Two-sided on purpose: pinning a seed can also make an
    always-failing test pass, and that shift implicates the dimension too.
    """
    if n1 == 0 or n2 == 0:
        return 1.0
    p1, p2 = f1 / n1, f2 / n2
    pooled = (f1 + f2) / (n1 + n2)
    if pooled in (0.0, 1.0):
        return 1.0
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    z = abs(p1 - p2) / se
    return 2 * _norm_sf(z)


def benjamini_hochberg(pvalues: list[float], q: float = 0.05) -> list[bool]:
    """Benjamini–Hochberg FDR control.

    Returns a reject-flag per input p-value. One hypothesis test per test in
    the suite means ~5% false flake calls at alpha=0.05 across thousands of
    tests; this keeps the false discovery rate at q instead.
    """
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    reject = [False] * m
    max_k = -1
    for rank, idx in enumerate(order, start=1):
        if pvalues[idx] <= q * rank / m:
            max_k = rank
    for rank, idx in enumerate(order, start=1):
        if rank <= max_k:
            reject[idx] = True
    return reject
