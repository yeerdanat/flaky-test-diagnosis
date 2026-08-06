"""Bisector tests against a simulated (optionally noisy) oracle — no
subprocesses, so the ddmin adaptations are testable in milliseconds."""
import random

from culpa.bisector import _chunks, bisect
from culpa.runner import Budget, BudgetExhausted, TrialResult
from culpa.runner import TestResult as _Result  # alias avoids pytest collection


class FakeRunner:
    """Simulates trials: the victim fails iff all `polluters` appear before it,
    with optional trigger noise (a genuinely-polluted run may still pass)."""

    def __init__(self, polluters, trigger_rate=1.0, seed=7, budget=None):
        self.polluters = set(polluters)
        self.trigger_rate = trigger_rate
        self.rng = random.Random(seed)
        self.budget = budget
        self.trials = 0

    def run(self, tests, statediff=False, **kwargs):
        self.trials += 1
        if self.budget is not None:
            self.budget.charge()
        victim = tests[-1]
        prefix = set(tests[:-1])
        triggered = self.polluters <= prefix and self.rng.random() < self.trigger_rate
        diff = {"env": {"K": {"before": "a", "after": "b"}}} if statediff else None
        results = {
            t: _Result(test_id=t, status="passed", duration_ms=1.0, state_diff=diff)
            for t in tests[:-1]
        }
        results[victim] = _Result(
            test_id=victim,
            status="failed" if triggered else "passed",
            duration_ms=1.0,
            state_diff=diff,
        )
        return TrialResult(order=list(tests), results=results, exit_code=int(triggered),
                           duration_ms=1.0)


PREFIX = [f"test_{i}" for i in range(12)]


def test_finds_single_polluter_deterministic():
    runner = FakeRunner(polluters={"test_7"})
    result = bisect(runner, "victim", PREFIX)
    assert result.polluters == ["test_7"]
    assert result.confirmed


def test_finds_single_polluter_noisy_oracle():
    # polluted runs still pass 30% of the time; asymmetric SPRT must cope
    runner = FakeRunner(polluters={"test_3"}, trigger_rate=0.7, seed=42)
    result = bisect(runner, "victim", PREFIX)
    assert result.polluters == ["test_3"]
    assert result.confirmed


def test_finds_polluter_pair():
    runner = FakeRunner(polluters={"test_2", "test_9"})
    result = bisect(runner, "victim", PREFIX)
    assert sorted(result.polluters) == ["test_2", "test_9"]
    assert result.confirmed


def test_nonreproducing_prefix_reports_inconclusive():
    runner = FakeRunner(polluters={"not_in_prefix"})
    result = bisect(runner, "victim", PREFIX)
    assert result.polluters == []
    assert not result.confirmed
    assert "did not reproduce" in result.note


def test_budget_exhaustion_degrades_gracefully():
    budget = Budget(max_trials=8)
    runner = FakeRunner(polluters={"test_5"}, budget=budget)
    result = bisect(runner, "victim", PREFIX)
    assert result.exhausted
    # graceful degradation: whatever it reports must still contain the polluter
    assert "test_5" in result.polluters


def test_state_evidence_collected():
    runner = FakeRunner(polluters={"test_1"})
    result = bisect(runner, "victim", PREFIX)
    assert result.state_evidence  # replayed with statediff and captured a diff


def test_chunks_partition():
    assert _chunks([1, 2, 3, 4, 5], 2) == [[1, 2, 3], [4, 5]]
    assert _chunks([1, 2, 3], 5) == [[1], [2], [3]]
    flat = [x for c in _chunks(list(range(10)), 3) for x in c]
    assert flat == list(range(10))
