"""Runner adapter — the only component that knows pytest.

Executes a specified subset of tests, in a specified order, under a specified
environment, in a fresh subprocess per trial, and returns structured per-test
outcomes. Everything above this layer is framework-agnostic.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path


class BudgetExhausted(Exception):
    """Raised when the trial budget runs out; callers degrade gracefully."""


@dataclass
class TestResult:
    test_id: str
    status: str  # passed | failed | error | skipped | unknown
    duration_ms: float
    error_hash: str | None = None
    error: str | None = None
    state_diff: dict | None = None

    @property
    def failed(self) -> bool:
        return self.status in ("failed", "error")


@dataclass
class TrialResult:
    order: list[str]
    results: dict[str, TestResult]
    exit_code: int
    duration_ms: float
    env_fingerprint: str = ""

    def outcome_of(self, test_id: str) -> TestResult | None:
        return self.results.get(test_id)

    def failed_tests(self) -> list[str]:
        return [t for t, r in self.results.items() if r.failed]


@dataclass
class Budget:
    """Shared trial budget with graceful exhaustion."""

    max_trials: int
    used: int = 0
    wall_seconds: float = 0.0

    def charge(self, wall: float = 0.0) -> None:
        self.used += 1
        self.wall_seconds += wall
        if self.used > self.max_trials:
            raise BudgetExhausted(f"trial budget of {self.max_trials} exhausted")

    @property
    def remaining(self) -> int:
        return max(0, self.max_trials - self.used)


class PytestRunner:
    """Runs pytest trials in fresh subprocesses with a controlled environment."""

    def __init__(
        self,
        repo: str | Path,
        budget: Budget | None = None,
        timeout: int = 900,
        pytest_args: tuple[str, ...] = (),
    ) -> None:
        self.repo = Path(repo).resolve()
        self.budget = budget
        self.timeout = timeout
        self.pytest_args = pytest_args
        # Make the culpa package importable inside the trial subprocess.
        import culpa

        self._pkg_dir = str(Path(culpa.__file__).resolve().parent.parent)

    # ------------------------------------------------------------------ #

    def _base_env(self) -> dict[str, str]:
        env = dict(os.environ)
        # Ambient default: let the interpreter randomize its hash seed so
        # hash-order flakes actually surface. Trials pin it explicitly.
        env.pop("PYTHONHASHSEED", None)
        for k in list(env):
            if k.startswith("CULPA_"):
                del env[k]
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (self._pkg_dir, env.get("PYTHONPATH")) if p
        )
        return env

    def collect(self) -> list[str]:
        """Collect the suite's test ids in default (definition) order."""
        env = self._base_env()
        env["PYTHONHASHSEED"] = "0"  # deterministic collection
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q",
             "--rootdir", str(self.repo), "-p", "no:cacheprovider",
             *self.pytest_args, "."],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )
        tests = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if "::" in line and " " not in line:
                tests.append(line)
        if not tests:
            raise RuntimeError(
                f"pytest collected no tests in {self.repo}\n{proc.stdout}\n{proc.stderr}"
            )
        return tests

    def run(
        self,
        tests: list[str],
        env_overrides: dict[str, str] | None = None,
        rng_seed: int | None = None,
        statediff: bool = False,
    ) -> TrialResult:
        """Run exactly `tests`, in order, in a fresh process. One trial."""
        start = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="culpa-trial-") as tmp:
            out_file = os.path.join(tmp, "results.jsonl")
            order_file = os.path.join(tmp, "order.json")
            with open(order_file, "w") as f:
                json.dump(tests, f)

            env = self._base_env()
            env["CULPA_OUT"] = out_file
            env["CULPA_ORDER"] = order_file
            env["CULPA_ROOT"] = str(self.repo)
            if statediff:
                env["CULPA_STATEDIFF"] = "1"
            if rng_seed is not None:
                env["CULPA_RNG_SEED"] = str(rng_seed)
            if env_overrides:
                env.update(env_overrides)

            # --rootdir pins node ids relative to the scanned repo, so ids stay
            # stable between the original tree and the verifier's patched copy.
            cmd = [
                sys.executable, "-m", "pytest", "-q",
                "--rootdir", str(self.repo),
                "-p", "culpa.plugin",
                "-p", "no:cacheprovider",
                "-p", "no:randomly",
                "--continue-on-collection-errors",
                *self.pytest_args,
                ".",
            ]
            try:
                proc = subprocess.run(
                    cmd, cwd=self.repo, env=env,
                    capture_output=True, text=True, timeout=self.timeout,
                )
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                exit_code = -1

            results: dict[str, TestResult] = {}
            if os.path.exists(out_file):
                with open(out_file) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        rec = json.loads(line)
                        results[rec["test_id"]] = TestResult(
                            test_id=rec["test_id"],
                            status=rec["status"],
                            duration_ms=rec["duration_ms"],
                            error_hash=rec.get("error_hash"),
                            error=rec.get("error"),
                            state_diff=rec.get("state_diff"),
                        )

        wall = time.monotonic() - start
        if self.budget is not None:
            self.budget.charge(wall)
        fp_parts = sorted((env_overrides or {}).items())
        env_fp = f"seed={rng_seed};" + ";".join(f"{k}={v}" for k, v in fp_parts)
        return TrialResult(
            order=list(tests),
            results=results,
            exit_code=exit_code,
            duration_ms=wall * 1000,
            env_fingerprint=env_fp,
        )
