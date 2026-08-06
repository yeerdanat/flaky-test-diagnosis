"""Orchestrator — wires detector, bisector, classifier, fix synthesis and
verification into the scan pipeline (design doc §4).

Pipeline: suite rounds (shuffled orders, iDFlakies-style) -> isolation
baseline per suspect -> OD bisection / NOD screening -> fix synthesis ->
three-stage verification -> report.
"""
from __future__ import annotations

import datetime
import random
import subprocess
import time
from pathlib import Path

from . import __version__
from .bisector import bisect
from .classifier import DIM_HASHSEED, DIM_RNGSEED, screen_nod
from .detector import NOD, STABLE_FAIL, STABLE_PASS, isolation_baseline
from .fixes import Patch, fix_for_hashseed, fix_for_od, fix_for_rngseed
from .runner import Budget, BudgetExhausted, PytestRunner
from .stats import benjamini_hochberg, wilson_interval
from .store import Store
from .verifier import REPLAY_AMBIENT, REPLAY_VARY_RNG, ReplaySpec, verify_patch

KIND_OD = "order-dependent (victim)"
KIND_NOD = "non-order-dependent"
KIND_ALWAYS_FAILING = "always failing (brittle or broken)"


def _git_sha(repo: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo,
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


class Orchestrator:
    def __init__(
        self,
        repo: str | Path,
        rounds: int = 6,
        baseline_min_trials: int = 5,
        baseline_max_trials: int = 25,
        screen_trials: int = 12,
        max_trials: int = 400,
        make_fixes: bool = False,
        verify_fixes: bool = False,
        db_path: str | Path | None = None,
        pytest_args: tuple[str, ...] = (),
        log=print,
    ) -> None:
        self.repo = Path(repo).resolve()
        self.rounds = rounds
        self.baseline_min_trials = baseline_min_trials
        self.baseline_max_trials = baseline_max_trials
        self.screen_trials = screen_trials
        self.make_fixes = make_fixes or verify_fixes
        self.verify_fixes = verify_fixes
        self.pytest_args = pytest_args
        self.log = log

        self.budget = Budget(max_trials=max_trials)
        self.runner = PytestRunner(self.repo, budget=self.budget, pytest_args=pytest_args)
        self.store = Store(db_path or self.repo / ".pinpoint" / "pinpoint.db")

    # ------------------------------------------------------------------ #

    def scan(self) -> dict:
        started = time.time()
        config = {
            "rounds": self.rounds,
            "max_trials": self.budget.max_trials,
            "screen_trials": self.screen_trials,
            "fix": self.make_fixes,
            "verify": self.verify_fixes,
        }
        run_id = self.store.start_run(_git_sha(self.repo), config, self.budget.max_trials)
        exhausted = False
        flakes: list[dict] = []
        tests: list[str] = []

        try:
            tests = self.runner.collect()
            self.log(f"collected {len(tests)} tests")

            suite_stats, failing_orders, error_hashes = self._suite_rounds(run_id, tests)
            candidates = sorted(t for t, (f, _n) in suite_stats.items() if f > 0)
            self.log(f"{len(candidates)} candidate flaky test(s): {candidates}")

            baselines = {}
            for test_id in candidates:
                b = isolation_baseline(
                    self.runner, test_id,
                    min_trials=self.baseline_min_trials,
                    max_trials=self.baseline_max_trials,
                )
                baselines[test_id] = b
                error_hashes.setdefault(test_id, set()).update(b.error_hashes)
                self.log(f"  baseline {test_id}: {b.verdict} "
                         f"({b.failures}/{b.trials} failures alone)")

            screens = {}
            for test_id in candidates:
                baseline = baselines[test_id]
                entry = self._base_entry(test_id, suite_stats, baseline, error_hashes)
                if baseline.verdict == STABLE_PASS:
                    entry["kind"] = KIND_OD
                    entry["diagnosis"] = self._diagnose_od(
                        test_id, failing_orders.get(test_id, []))
                elif baseline.verdict == NOD:
                    entry["kind"] = KIND_NOD
                    screen = screen_nod(self.runner, test_id,
                                        trials_per_condition=self.screen_trials)
                    screens[test_id] = screen
                    entry["diagnosis"] = self._screen_to_dict(screen)
                else:  # STABLE_FAIL
                    entry["kind"] = KIND_ALWAYS_FAILING
                    entry["diagnosis"] = {
                        "cause": "fails even in isolation",
                        "note": ("either a genuinely broken test or a brittle "
                                 "test that needs a state-setter to run first "
                                 "(brittle detection is a v2 feature)"),
                    }
                flakes.append(entry)

            self._apply_fdr(flakes, screens)

            if self.make_fixes:
                for entry in flakes:
                    patch, replay = self._synthesize(entry)
                    if patch is None:
                        continue
                    entry["patch"] = self._patch_to_dict(patch)
                    if self.verify_fixes and replay is not None:
                        self.log(f"  verifying fix for {entry['test_id']} ...")
                        v = verify_patch(self.repo, patch, replay, self.budget,
                                         pytest_args=self.pytest_args)
                        entry["patch"]["verified"] = v.verified
                        entry["patch"]["verification"] = {
                            "replay_ok": v.replay_ok,
                            "replay_trials": v.replay_trials,
                            "replay_failures": v.replay_failures,
                            "regression_ok": v.regression_ok,
                            "newly_failing": v.newly_failing,
                            "semantic_ok": v.semantic_ok,
                            "semantic_violations": v.semantic_violations,
                            "note": v.note,
                        }
        except BudgetExhausted:
            exhausted = True
            self.log("trial budget exhausted; reporting partial results")

        self._persist(run_id, flakes)
        self.store.finish_run(run_id, "exhausted" if exhausted else "completed")

        report = {
            "pinpoint_version": __version__,
            "repo": str(self.repo),
            "started_at": datetime.datetime.fromtimestamp(
                started, datetime.timezone.utc).isoformat(),
            "config": config,
            "suite": {"tests": len(tests), "rounds": self.rounds},
            "cost": {
                "trials": self.budget.used,
                "wall_seconds": round(self.budget.wall_seconds, 2),
                "budget_trials": self.budget.max_trials,
                "exhausted": exhausted,
            },
            "flakes": flakes,
        }
        return report

    # ------------------------------------------------------------------ #

    def _suite_rounds(self, run_id: int, tests: list[str]):
        """Round 0 in collection order, later rounds shuffled (seeded) so
        order-dependent flakes get a chance to surface."""
        suite_stats: dict[str, list[int]] = {t: [0, 0] for t in tests}
        failing_orders: dict[str, list[list[str]]] = {}
        error_hashes: dict[str, set[str]] = {}

        for round_no in range(self.rounds):
            order = list(tests)
            if round_no > 0:
                random.Random(round_no).shuffle(order)
            trial = self.runner.run(order)
            self.store.record_trial(run_id, trial)
            failed = trial.failed_tests()
            self.log(f"round {round_no}: {len(failed)} failure(s)"
                     + (f" -> {failed}" if failed else ""))
            for t in order:
                r = trial.outcome_of(t)
                if r is None:
                    continue
                suite_stats[t][1] += 1
                if r.failed:
                    suite_stats[t][0] += 1
                    failing_orders.setdefault(t, []).append(order)
                    if r.error_hash:
                        error_hashes.setdefault(t, set()).add(r.error_hash)
        return (
            {t: (f, n) for t, (f, n) in suite_stats.items()},
            failing_orders,
            error_hashes,
        )

    def _base_entry(self, test_id, suite_stats, baseline, error_hashes) -> dict:
        f, n = suite_stats[test_id]
        hashes = sorted(error_hashes.get(test_id, set()))
        entry = {
            "test_id": test_id,
            "kind": None,
            "suite_failures": f,
            "suite_trials": n,
            "suite_ci": wilson_interval(f, n),
            "isolation": {
                "trials": baseline.trials,
                "failures": baseline.failures,
                "failure_rate": baseline.failure_rate,
                "ci": baseline.ci,
                "verdict": baseline.verdict,
            },
            "error_hashes": hashes,
            "diagnosis": None,
            "patch": None,
        }
        if len(hashes) > 1:
            entry["note"] = (f"{len(hashes)} distinct normalized failure "
                             "signatures — possibly flaky for more than one reason")
        return entry

    def _diagnose_od(self, victim: str, failing_orders: list[list[str]]) -> dict:
        if not failing_orders:
            return {"cause": "order-dependent (no failing order captured)",
                    "note": "victim passed alone but no suite failure order was recorded"}
        order = failing_orders[0]
        prefix = order[: order.index(victim)]
        self.log(f"  bisecting {victim} over a {len(prefix)}-test prefix ...")
        result = bisect(self.runner, victim, prefix)
        polluted = []
        for diff in result.state_evidence.values():
            for key in diff.get("env", {}):
                polluted.append(f"os.environ[{key!r}]")
            for mod, attrs in diff.get("module_globals", {}).items():
                polluted += [f"{mod}.{a}" for a in attrs]
        if result.polluters:
            confidence = "high" if result.confirmed and len(result.polluters) == 1 \
                else ("medium" if result.confirmed else "low")
        else:
            confidence = "low"
        return {
            "cause": "test-order dependence (state pollution)",
            "polluters": result.polluters,
            "confirmed": result.confirmed,
            "budget_exhausted": result.exhausted,
            "confidence": confidence,
            "oracle_queries": len(result.queries),
            "bisection_trials": result.trials_used,
            "polluted_state": polluted,
            "state_evidence": result.state_evidence,
            "note": result.note,
        }

    @staticmethod
    def _screen_to_dict(screen) -> dict:
        dims = {
            d.dimension: {
                "failure_rate": d.failure_rate,
                "failures": d.failures,
                "trials": d.trials,
                "ci": d.ci,
                "pvalue": round(d.pvalue, 6),
            }
            for d in screen.dimensions.values()
        }
        return {
            "cause": ("seed-dependent nondeterminism"
                      if screen.implicated else "unattributed nondeterminism"),
            "control": {"failures": screen.control_failures,
                        "trials": screen.control_trials,
                        "failure_rate": screen.control_rate},
            "dimensions": dims,
            "implicated": list(screen.implicated),
            "note": screen.note,
        }

    def _apply_fdr(self, flakes: list[dict], screens: dict) -> None:
        """Benjamini–Hochberg across all (test, dimension) screening p-values."""
        keys, pvals = [], []
        for test_id, screen in screens.items():
            for dim, d in screen.dimensions.items():
                keys.append((test_id, dim))
                pvals.append(d.pvalue)
        if not pvals:
            return
        rejected = benjamini_hochberg(pvals, q=0.10)
        surviving: dict[str, list[str]] = {}
        for (test_id, dim), reject in zip(keys, rejected):
            if reject:
                surviving.setdefault(test_id, []).append(dim)
        for entry in flakes:
            diag = entry.get("diagnosis") or {}
            if "implicated" in diag:
                diag["implicated"] = surviving.get(entry["test_id"], [])
                diag["fdr"] = "benjamini-hochberg q=0.10 across all screened dimensions"
                if not diag["implicated"]:
                    diag["cause"] = "unattributed nondeterminism"

    # ------------------------------------------------------------------ #

    def _synthesize(self, entry: dict) -> tuple[Patch | None, ReplaySpec | None]:
        diag = entry.get("diagnosis") or {}
        test_id = entry["test_id"]
        if entry["kind"] == KIND_OD and diag.get("polluters"):
            patch = fix_for_od(self.repo, test_id, diag["polluters"],
                               diag.get("state_evidence", {}))
            if patch is None:
                return None, None
            replay = ReplaySpec(order=diag["polluters"] + [test_id],
                                victim=test_id, mode=REPLAY_AMBIENT)
            return patch, replay
        if entry["kind"] == KIND_NOD:
            implicated = diag.get("implicated", [])
            if DIM_RNGSEED in implicated:
                return (fix_for_rngseed(self.repo),
                        ReplaySpec(order=[test_id], victim=test_id,
                                   mode=REPLAY_VARY_RNG))
            if DIM_HASHSEED in implicated:
                return (fix_for_hashseed(self.repo),
                        ReplaySpec(order=[test_id], victim=test_id,
                                   mode=REPLAY_AMBIENT))
        return None, None

    @staticmethod
    def _patch_to_dict(patch: Patch) -> dict:
        return {
            "tier": patch.tier,
            "cause": patch.cause,
            "description": patch.description,
            "files": list(patch.files),
            "diff": patch.diff_text,
            "verified": None,
            "verification": None,
        }

    def _persist(self, run_id: int, flakes: list[dict]) -> None:
        for entry in flakes:
            flake_id = self.store.record_flake(
                run_id, entry["test_id"], entry["kind"] or "unknown",
                entry["isolation"]["failure_rate"],
                tuple(entry["isolation"]["ci"]),
                entry["isolation"]["trials"] + entry["suite_trials"],
            )
            diag = entry.get("diagnosis")
            if diag:
                polluters = diag.get("polluters") or []
                diag_id = self.store.record_diagnosis(
                    flake_id, diag.get("cause", "unknown"), diag,
                    diag.get("confidence", ""),
                    polluters[0] if polluters else None,
                )
                patch = entry.get("patch")
                if patch:
                    self.store.record_patch(
                        diag_id, patch["tier"], patch["diff"],
                        patch["verified"], patch.get("verification") or {},
                    )
