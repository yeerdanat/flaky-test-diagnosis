"""Detector — establishes the isolation baseline (design doc §6.1) and decides
whether a test is flaky at all (§6.2), cheaply, via SPRT.

The baseline is non-negotiable: without it you can't tell order dependence
from ambient nondeterminism, and every later conclusion is confounded.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .runner import BudgetExhausted, PytestRunner
from .stats import ACCEPT_H0, ACCEPT_H1, CONTINUE, SPRT, wilson_interval

# baseline verdicts
STABLE_PASS = "stable_pass"   # passes alone consistently -> candidate victim
STABLE_FAIL = "stable_fail"   # fails alone consistently  -> brittle or broken
NOD = "nod"                   # mixed alone               -> non-order-dependent flake


@dataclass
class BaselineResult:
    test_id: str
    trials: int
    failures: int
    verdict: str
    ci: tuple[float, float]
    error_hashes: set[str] = field(default_factory=set)

    @property
    def failure_rate(self) -> float:
        return self.failures / self.trials if self.trials else 0.0


def isolation_baseline(
    runner: PytestRunner,
    test_id: str,
    min_trials: int = 5,
    max_trials: int = 25,
) -> BaselineResult:
    """Run the test alone in a fresh process under ambient conditions.

    SPRT (H0: p_fail <= 0.01 vs H1: p_fail >= 0.10) decides how many trials
    are actually needed instead of a wasteful/wrong fixed N — but we always
    run at least `min_trials` so a fast ACCEPT_H0 can't hide a rare flake
    behind two lucky passes.
    """
    sprt = SPRT(p0=0.01, p1=0.10, alpha=0.05, beta=0.10, max_trials=max_trials)
    failures = 0
    error_hashes: set[str] = set()
    decision = CONTINUE
    n = 0
    try:
        while True:
            trial = runner.run([test_id])
            result = trial.outcome_of(test_id)
            failed = result is None or result.failed
            if failed:
                failures += 1
                if result is not None and result.error_hash:
                    error_hashes.add(result.error_hash)
            n += 1
            decision = sprt.record(failed)
            if decision != CONTINUE and n >= min_trials:
                break
            if n >= max_trials:
                break
    except BudgetExhausted:
        pass  # report what we have

    if n == 0:
        raise RuntimeError(f"no trials run for {test_id} (budget exhausted)")

    if failures == 0:
        verdict = STABLE_PASS
    elif failures == n:
        verdict = STABLE_FAIL
    else:
        verdict = NOD

    return BaselineResult(
        test_id=test_id,
        trials=n,
        failures=failures,
        verdict=verdict,
        ci=wilson_interval(failures, n),
        error_hashes=error_hashes,
    )
