"""Verifier — proves a candidate fix actually holds (design doc §9).

Three stages, all required, in order:

1. Replay the original failing condition against the patched tree, with the
   same statistical rigor as detection (SPRT, not "it passed once").
2. Regression check: no test that passed before the patch may fail after it.
3. Semantic check: the patch must not weaken any test — no assertions
   removed, no skip/xfail added. The trivially "correct" fix for any flaky
   test is deleting its assertions; a tool that can do that silently is
   dangerous.

Patches are applied to a disposable copy of the repo, never the live tree.
"""
from __future__ import annotations

import ast
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .fixes import Patch
from .runner import Budget, BudgetExhausted, PytestRunner
from .stats import ACCEPT_H0, ACCEPT_H1, CONTINUE, SPRT

# how the replay perturbs the environment per trial
REPLAY_AMBIENT = "ambient"      # fresh process, randomized hash seed (OD, hashseed)
REPLAY_VARY_RNG = "vary_rng"    # pin hash seed, vary RNG seed (rngseed flakes)


@dataclass
class ReplaySpec:
    order: list[str]      # e.g. [*polluters, victim]
    victim: str
    mode: str = REPLAY_AMBIENT


@dataclass
class VerifyResult:
    replay_ok: bool = False
    regression_ok: bool = False
    semantic_ok: bool = False
    replay_trials: int = 0
    replay_failures: int = 0
    newly_failing: list[str] = field(default_factory=list)
    semantic_violations: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def verified(self) -> bool:
        return self.replay_ok and self.regression_ok and self.semantic_ok


def _apply_patch(repo_copy: Path, patch: Patch) -> None:
    for rel_path, (_old, new) in patch.files.items():
        target = repo_copy / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new)


def _test_weakening(old_src: str | None, new_src: str) -> list[str]:
    """Compare assertion count and skip/xfail usage between file versions."""
    def stats(src: str | None) -> tuple[int, int]:
        if not src:
            return (0, 0)
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return (0, 0)
        asserts = sum(isinstance(n, ast.Assert) for n in ast.walk(tree))
        skips = sum(
            isinstance(n, ast.Attribute) and n.attr in ("skip", "skipif", "xfail")
            for n in ast.walk(tree)
        )
        return (asserts, skips)

    old_asserts, old_skips = stats(old_src)
    new_asserts, new_skips = stats(new_src)
    violations = []
    if new_asserts < old_asserts:
        violations.append(f"assertions reduced {old_asserts} -> {new_asserts}")
    if new_skips > old_skips:
        violations.append(f"skip/xfail markers increased {old_skips} -> {new_skips}")
    return violations


def verify_patch(
    repo: Path,
    patch: Patch,
    replay: ReplaySpec,
    budget: Budget,
    replay_max_trials: int = 12,
    pytest_args: tuple[str, ...] = (),
) -> VerifyResult:
    result = VerifyResult()

    # Stage 3 is cheapest and gates the expensive stages: run it first, but
    # report it in its documented position.
    for rel_path, (old, new) in patch.files.items():
        for violation in _test_weakening(old, new):
            result.semantic_violations.append(f"{rel_path}: {violation}")
    result.semantic_ok = not result.semantic_violations
    if not result.semantic_ok:
        result.note = "semantic check failed; skipped replay and regression"
        return result

    workdir = Path(tempfile.mkdtemp(prefix="culpa-verify-"))
    try:
        repo_copy = workdir / "repo"
        shutil.copytree(
            repo, repo_copy,
            ignore=shutil.ignore_patterns("__pycache__", ".git", ".culpa",
                                          "*.pyc", ".venv", "venv"),
        )
        _apply_patch(repo_copy, patch)
        patched_runner = PytestRunner(repo_copy, budget=budget, pytest_args=pytest_args)
        original_runner = PytestRunner(repo, budget=budget, pytest_args=pytest_args)

        # ---- stage 1: statistical replay of the original failing condition
        sprt = SPRT(p0=0.02, p1=0.30, alpha=0.05, beta=0.05,
                    max_trials=replay_max_trials)
        decision = CONTINUE
        try:
            i = 0
            while decision == CONTINUE:
                if replay.mode == REPLAY_VARY_RNG:
                    trial = patched_runner.run(
                        replay.order,
                        env_overrides={"PYTHONHASHSEED": "0"},
                        rng_seed=2000 + i,
                    )
                else:
                    trial = patched_runner.run(replay.order)
                r = trial.outcome_of(replay.victim)
                failed = r is None or r.failed
                decision = sprt.record(failed)
                i += 1
        except BudgetExhausted:
            result.note = "budget exhausted during replay"
            return result
        result.replay_trials = sprt.n
        result.replay_failures = sprt.failures
        result.replay_ok = decision == ACCEPT_H0
        if not result.replay_ok:
            result.note = "fix did not suppress the failure under replay"
            return result

        # ---- stage 2: regression check under a pinned, deterministic env
        pinned = {"PYTHONHASHSEED": "0"}
        try:
            before = original_runner.run(
                original_runner.collect(), env_overrides=pinned, rng_seed=0)
            after = patched_runner.run(
                patched_runner.collect(), env_overrides=pinned, rng_seed=0)
        except BudgetExhausted:
            result.note = "budget exhausted during regression check"
            return result
        passed_before = {t for t, r in before.results.items() if not r.failed}
        result.newly_failing = sorted(
            t for t in passed_before
            if t not in after.results or after.results[t].failed
        )
        result.regression_ok = not result.newly_failing
        if not result.regression_ok:
            result.note = "patch makes previously-passing tests fail"
        return result
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
