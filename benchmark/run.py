"""Run the benchmark: generate scenarios, scan each with the real CLI, score.

Usage:
    python -m benchmark.run                     # full matrix, a few minutes
    python -m benchmark.run --quick             # 5-scenario subset
    python -m benchmark.run --no-verify         # skip fix verification (faster)
    python -m benchmark.run --results out.json  # also write results as JSON
    python -m benchmark.run --baseline          # SPRT vs fixed-N cost comparison

Each scenario is scanned through the installed `whyflaky` CLI in a fresh temp
directory, so the benchmark doubles as an end-to-end integration test of the
public contract (exit codes, --json report shape).

--baseline runs every scenario twice: once normally (SPRT stopping) and once
with WHYFLAKY_FIXED_N set, which replaces sequential stopping with constant
repetition per query. N defaults to 15; pytest-flakefinder's default of 50
reruns is the industry reference point and can be requested with
--fixed-n 50. Baseline mode skips fix verification in both arms so the
comparison isolates diagnosis cost, and raises the trial budget so the
fixed-N arm is never rescued by budget exhaustion.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .generate import write_scenario
from .metrics import (
    aggregate,
    baseline_table,
    compare_baseline,
    markdown_table,
    score_scenario,
)
from .scenarios import Scenario, build_matrix

QUICK_IDS = {
    "od_module_global_s10_seed1",
    "od_env_s10_seed1",
    "nod_hashseed_s10_seed1",
    "nod_rng40_s10_seed1",
    "stable_s10_seed1",
}

_SCAN_TIMEOUT = 900  # seconds per scenario; a hang should fail one row, not the sweep


def _whyflaky_executable() -> str:
    """The `whyflaky` console script installed next to this interpreter."""
    exe = Path(sys.executable).parent / "whyflaky"
    if not exe.exists():
        raise SystemExit(
            "whyflaky CLI not found next to the interpreter; run `pip install -e .` first"
        )
    return str(exe)


def run_one(scenario: Scenario, workdir: Path, *, verify: bool,
            budget: int, rounds: int,
            fixed_n: int | None = None) -> tuple[dict | None, str | None]:
    """Scan one materialized scenario. Returns (report, error).

    fixed_n switches the scan to the fixed-repetition baseline (see module
    docstring); None runs normal SPRT stopping.
    """
    repo = write_scenario(scenario, workdir)
    report_path = repo / ".whyflaky" / "report.json"
    cmd = [
        _whyflaky_executable(), "scan", str(repo),
        "--rounds", str(rounds),
        "--budget", str(budget),
        "--json", str(report_path),
        "-q",
    ]
    if verify:
        cmd.append("--verify")
    env = {k: v for k, v in os.environ.items() if k != "WHYFLAKY_FIXED_N"}
    if fixed_n:
        env["WHYFLAKY_FIXED_N"] = str(fixed_n)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              env=env, timeout=_SCAN_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, f"scan timed out after {_SCAN_TIMEOUT}s"
    if not report_path.exists():
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        return None, f"no report (exit {proc.returncode}): {' / '.join(tail)}"
    try:
        return json.loads(report_path.read_text()), None
    except json.JSONDecodeError as exc:
        return None, f"unparseable report: {exc}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true",
                        help="run the 5-scenario subset")
    parser.add_argument("--only", action="append", default=[],
                        help="run matching scenario ids only (substring, repeatable)")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--rounds", type=int, default=6)
    parser.add_argument("--budget", type=int, default=400)
    parser.add_argument("--no-verify", dest="verify", action="store_false",
                        help="skip fix synthesis + verification")
    parser.add_argument("--baseline", action="store_true",
                        help="also run a fixed-N arm and compare trial cost")
    parser.add_argument("--fixed-n", type=int, default=15,
                        help="trials per query in the baseline arm (default 15;"
                             " 50 = pytest-flakefinder's default)")
    parser.add_argument("--results", type=Path,
                        help="write scores + aggregate to this JSON file")
    parser.add_argument("--workdir", type=Path,
                        help="keep scenario dirs here (default: temp, deleted)")
    args = parser.parse_args(argv)

    scenarios = build_matrix(args.seed)
    if args.quick:
        scenarios = [s for s in scenarios if s.scenario_id in QUICK_IDS]
    for needle in args.only:
        scenarios = [s for s in scenarios if needle in s.scenario_id]
    if not scenarios:
        parser.error("no scenarios selected")

    verify = args.verify
    budget = args.budget
    if args.baseline:
        verify = False
        budget = max(budget, args.fixed_n * 200)
        print(f"baseline mode: SPRT vs fixed-N={args.fixed_n},"
              f" verification off, budget {budget}\n")

    keep = args.workdir is not None
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="whyflaky-bench-"))
    workdir.mkdir(parents=True, exist_ok=True)

    scores, fixed_scores = [], []
    try:
        for i, s in enumerate(scenarios, 1):
            print(f"[{i}/{len(scenarios)}] {s.scenario_id} ...",
                  end=" ", flush=True)
            report, error = run_one(s, workdir, verify=verify,
                                    budget=budget, rounds=args.rounds)
            score = score_scenario(s.manifest(), report, error)
            scores.append(score)
            if error:
                print(f"ERROR: {error}")
            else:
                print(f"{score.trials} trials, {score.wall_seconds}s",
                      end="" if args.baseline else "\n", flush=True)
            if args.baseline:
                # same scenario dir, fresh arm; .whyflaky artifacts are overwritten
                report_f, error_f = run_one(
                    s, workdir, verify=verify, budget=budget,
                    rounds=args.rounds, fixed_n=args.fixed_n)
                score_f = score_scenario(s.manifest(), report_f, error_f)
                fixed_scores.append(score_f)
                if error_f:
                    print(f"  | fixed-N ERROR: {error_f}")
                else:
                    print(f"  | fixed-N: {score_f.trials} trials,"
                          f" {score_f.wall_seconds}s")
    finally:
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)

    agg = aggregate(scores)
    print()
    print(markdown_table(scores, agg))

    baseline_cmp = None
    if args.baseline:
        baseline_cmp = compare_baseline(scores, fixed_scores)
        print()
        print(baseline_table(baseline_cmp, args.fixed_n))

    if args.results:
        payload = {
            "config": {
                "seed": args.seed, "rounds": args.rounds,
                "budget": budget, "verify": verify,
                "quick": args.quick,
                "baseline": args.baseline,
                "fixed_n": args.fixed_n if args.baseline else None,
            },
            "aggregate": agg,
            "baseline": baseline_cmp,
            "scenarios": [
                {
                    "scenario_id": s.scenario_id,
                    "template": s.template,
                    "error": s.error,
                    "trials": s.trials,
                    "wall_seconds": s.wall_seconds,
                    "budget_exhausted": s.budget_exhausted,
                    "false_positives": s.false_positives,
                    "flakes": [vars(f) for f in s.flakes],
                }
                for s in scores
            ],
        }
        args.results.parent.mkdir(parents=True, exist_ok=True)
        args.results.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"\nresults written to {args.results}")

    fixed_errors = sum(1 for s in fixed_scores if s.error)
    return 1 if agg["scan_errors"] or fixed_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
