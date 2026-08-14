"""Bisector — probabilistic delta debugging for order-dependent flakes.

Given a victim that passes alone but fails after some prefix of tests, find
the minimal polluting subsequence. Standard ddmin assumes a deterministic
oracle; ours is noisy, so every oracle query is itself an SPRT with
*asymmetric* error costs: a false "this subset doesn't trigger it" prunes the
true polluter and wrecks the bisection (beta tiny), while a false "triggers"
only costs extra trials (alpha loose).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .runner import BudgetExhausted, PytestRunner
from .stats import ACCEPT_H0, ACCEPT_H1, CONTINUE, SPRT


@dataclass
class OracleQuery:
    subset: tuple[str, ...]
    triggers: bool
    trials: int
    failures: int


@dataclass
class BisectResult:
    victim: str
    polluters: list[str]          # minimal confirmed polluting subsequence
    confirmed: bool               # False if we degraded (budget) or full prefix never triggered
    exhausted: bool               # budget ran out mid-bisection
    queries: list[OracleQuery] = field(default_factory=list)
    trials_used: int = 0
    state_evidence: dict = field(default_factory=dict)  # polluter -> state diff
    note: str = ""


class _Oracle:
    """Noisy oracle: does running [subset..., victim] make the victim fail?

    H0: p_fail <= p0 (subset does not trigger)
    H1: p_fail >= p1 (subset triggers)

    beta (false ACCEPT_H0) is set very low; a single observed failure is
    enough to accept H1 given the wide p0/p1 gap.
    """

    def __init__(self, runner: PytestRunner, victim: str, max_trials_per_query: int = 10):
        self.runner = runner
        self.victim = victim
        self.max_trials_per_query = max_trials_per_query
        self.cache: dict[tuple[str, ...], OracleQuery] = {}
        self.queries: list[OracleQuery] = []
        self.trials_used = 0

    def query(self, subset: list[str]) -> bool:
        key = tuple(subset)
        if key in self.cache:
            return self.cache[key].triggers
        sprt = SPRT(p0=0.02, p1=0.50, alpha=0.10, beta=0.02,
                    max_trials=self.max_trials_per_query)
        decision = CONTINUE
        while decision == CONTINUE:
            trial = self.runner.run(list(subset) + [self.victim])
            self.trials_used += 1
            result = trial.outcome_of(self.victim)
            failed = result is None or result.failed
            decision = sprt.record(failed)
        q = OracleQuery(
            subset=key,
            triggers=(decision == ACCEPT_H1),
            trials=sprt.n,
            failures=sprt.failures,
        )
        self.cache[key] = q
        self.queries.append(q)
        return q.triggers


def _chunks(seq: list[str], n: int) -> list[list[str]]:
    size, rem = divmod(len(seq), n)
    out, i = [], 0
    for j in range(n):
        step = size + (1 if j < rem else 0)
        if step:
            out.append(seq[i:i + step])
            i += step
    return out


def bisect(
    runner: PytestRunner,
    victim: str,
    failing_prefix: list[str],
    max_trials_per_query: int = 10,
) -> BisectResult:
    """ddmin over the failing prefix, with a noisy (SPRT-guarded) oracle.

    Budget exhaustion is graceful: we report the smallest subset confirmed to
    trigger so far rather than nothing.
    """
    oracle = _Oracle(runner, victim, max_trials_per_query)
    current = list(failing_prefix)
    confirmed = False

    try:
        # Sanity: the full prefix must reproduce the failure at all.
        if not oracle.query(current):
            return BisectResult(
                victim=victim, polluters=[], confirmed=False, exhausted=False,
                queries=oracle.queries, trials_used=oracle.trials_used,
                note=("full failing prefix did not reproduce the failure in "
                      "isolation trials; the dependence may involve ambient "
                      "nondeterminism or the full-suite environment"),
            )
        confirmed = True

        n = 2
        while len(current) >= 2:
            parts = _chunks(current, n)
            reduced = False
            for part in parts:  # try each chunk alone
                if oracle.query(part):
                    current, n, reduced = part, 2, True
                    break
            if not reduced and n > 2:  # try each complement
                for i in range(len(parts)):
                    complement = [t for j, p in enumerate(parts) if j != i for t in p]
                    if oracle.query(complement):
                        current, n, reduced = complement, max(n - 1, 2), True
                        break
            if not reduced:
                if n >= len(current):
                    break
                n = min(len(current), 2 * n)
        exhausted = False
    except BudgetExhausted:
        exhausted = True
        # keep smallest subset confirmed so far
        triggering = [q for q in oracle.queries if q.triggers]
        if triggering:
            current = list(min(triggering, key=lambda q: len(q.subset)).subset)

    result = BisectResult(
        victim=victim,
        polluters=current if confirmed else [],
        confirmed=confirmed and not exhausted,
        exhausted=exhausted,
        queries=oracle.queries,
        trials_used=oracle.trials_used,
    )

    # Name *what* was polluted: replay [polluters..., victim] with state-diff
    # instrumentation and record what each polluter changed.
    if confirmed and result.polluters:
        try:
            trial = runner.run(result.polluters + [victim], statediff=True)
            for test_id in result.polluters:
                r = trial.outcome_of(test_id)
                if r is not None and r.state_diff:
                    result.state_evidence[test_id] = r.state_diff
            result.trials_used += 1
        except BudgetExhausted:
            result.exhausted = True
    return result
