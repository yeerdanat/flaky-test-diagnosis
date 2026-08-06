"""Classifier — attributes non-order-dependent flakes to a cause by
perturbing one dimension at a time (design doc §6.4).

v1 dimensions: PYTHONHASHSEED and RNG seed. Control pins both; each
perturbed condition varies exactly one. A dimension is implicated when the
failure rate shifts significantly (two-sided two-proportion test) vs control.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .runner import BudgetExhausted, PytestRunner
from .stats import two_proportion_pvalue, wilson_interval

DIM_HASHSEED = "hashseed"
DIM_RNGSEED = "rngseed"


@dataclass
class DimensionResult:
    dimension: str
    trials: int
    failures: int
    pvalue: float  # vs control
    ci: tuple[float, float]

    @property
    def failure_rate(self) -> float:
        return self.failures / self.trials if self.trials else 0.0


@dataclass
class ScreenResult:
    test_id: str
    control_trials: int
    control_failures: int
    dimensions: dict[str, DimensionResult] = field(default_factory=dict)
    implicated: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def control_rate(self) -> float:
        return self.control_failures / self.control_trials if self.control_trials else 0.0


# Seeds for the "vary" conditions are spread across the seed space rather than
# taken consecutively: a short run of adjacent seeds is a small, arbitrary slice
# that can badly misrepresent a dimension's true failure rate, and because the
# sequence is fixed it would misrepresent it the *same way on every run*. The
# stride is a large prime, so the sequence stays deterministic and reproducible
# while covering the space evenly.
_SEED_STRIDE = 104729
_SEED_MAX = 2**32 - 1


def _spread_seed(i: int) -> int:
    return (1 + i * _SEED_STRIDE) % _SEED_MAX


def _run_condition(
    runner: PytestRunner,
    test_id: str,
    n: int,
    hashseed: str | None,   # None => vary per trial
    rng_seed: int | None,   # None => vary per trial
) -> tuple[int, int]:
    failures = 0
    trials = 0
    for i in range(n):
        varied = _spread_seed(i)
        env = {"PYTHONHASHSEED": hashseed if hashseed is not None else str(varied)}
        seed = rng_seed if rng_seed is not None else varied
        trial = runner.run([test_id], env_overrides=env, rng_seed=seed)
        result = trial.outcome_of(test_id)
        if result is None or result.failed:
            failures += 1
        trials += 1
    return failures, trials


def screen_nod(
    runner: PytestRunner,
    test_id: str,
    trials_per_condition: int = 10,
    alpha: float = 0.05,
) -> ScreenResult:
    """Single-factor screening over the v1 dimensions.

    Confounding note: single-factor screening finds main effects only. If
    both dimensions look implicated we say so in the report rather than
    picking one arbitrarily (a 2x2 factorial is the v2 follow-up).
    """
    out = ScreenResult(test_id=test_id, control_trials=0, control_failures=0)
    try:
        cf, cn = _run_condition(runner, test_id, trials_per_condition,
                                hashseed="0", rng_seed=0)
        out.control_failures, out.control_trials = cf, cn

        for dim, hashseed, rng_seed in (
            (DIM_HASHSEED, None, 0),   # vary hash seed, pin rng
            (DIM_RNGSEED, "0", None),  # pin hash seed, vary rng
        ):
            f, n = _run_condition(runner, test_id, trials_per_condition,
                                  hashseed=hashseed, rng_seed=rng_seed)
            out.dimensions[dim] = DimensionResult(
                dimension=dim,
                trials=n,
                failures=f,
                pvalue=two_proportion_pvalue(f, n, cf, cn),
                ci=wilson_interval(f, n),
            )
    except BudgetExhausted:
        out.note = "budget exhausted mid-screening; results partial"

    out.implicated = [
        d.dimension for d in out.dimensions.values() if d.pvalue < alpha
    ]
    if len(out.implicated) > 1:
        out.note = (out.note + " " if out.note else "") + (
            "multiple dimensions implicated; single-factor screening cannot "
            "rule out interaction — treat attribution as joint"
        )
    elif not out.implicated and out.dimensions:
        out.note = (out.note + " " if out.note else "") + (
            "no v1 dimension implicated; likely time/thread/network "
            "nondeterminism (v2 dimensions) or below detection power"
        )
    return out
