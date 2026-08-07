"""Run the benchmark: generate scenarios, scan each with the real CLI, score.

Usage:
    python -m benchmark.run                     # full matrix, ~10 min
    python -m benchmark.run --quick             # 5-scenario subset, ~2 min
    python -m benchmark.run --no-verify         # skip fix verification (faster)
    python -m benchmark.run --results out.json  # also write results as JSON

Each scenario is scanned through the installed `culpa` CLI in a fresh temp
directory, so the benchmark doubles as an end-to-end integration test of the
public contract (exit codes, --json report shape).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .generate import write_scenario
from .metrics import aggregate, markdown_table, score_scenario
from .scenarios import Scenario, build_matrix

QUICK_IDS = {
    "od_module_global_s10_seed1",
    "od_env_s10_seed1",
    "nod_hashseed_s10_seed1",
    "nod_rng40_s10_seed1",
    "stable_s10_seed1",
}

_SCAN_TIMEOUT = 900  # seconds per scenario; a hang should fail one row, not the sweep


def _culpa_executable() -> str:
    """The `culpa` console script installed next to this interpreter."""
    exe = Path(sys.executable).parent / "culpa"
    if not exe.exists():
        raise SystemExit(
            "culpa CLI not found next to the interpreter; run `pip install -e .` first"
        )
    return str(exe)


def run_one(scenario: Scenario, workdir: Path, *, verify: bool,
            budget: int, rounds: int) -> tuple[dict | None, str | None]:
    """Scan one materialized scenario. Returns (report, error)."""
    repo = write_scenario(scenario, workdir)
    report_path = repo / ".culpa" / "report.json"
    cmd = [
        _culpa_executable(), "scan", str(repo),
        "--rounds", str(rounds),
        "--budget", str(budget),
        "--json", str(report_path),
        "-q",
    ]
    if verify:
        cmd.append("--verify")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=_SCAN_TIMEOUT)
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

    keep = args.workdir is not None
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="culpa-bench-"))
    workdir.mkdir(parents=True, exist_ok=True)

    scores = []
    try:
        for i, s in enumerate(scenarios, 1):
            print(f"[{i}/{len(scenarios)}] {s.scenario_id} ...",
                  end=" ", flush=True)
            report, error = run_one(s, workdir, verify=args.verify,
                                    budget=args.budget, rounds=args.rounds)
            score = score_scenario(s.manifest(), report, error)
            scores.append(score)
            if error:
                print(f"ERROR: {error}")
            else:
                print(f"{score.trials} trials, {score.wall_seconds}s")
    finally:
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)

    agg = aggregate(scores)
    print()
    print(markdown_table(scores, agg))

    if args.results:
        payload = {
            "config": {
                "seed": args.seed, "rounds": args.rounds,
                "budget": args.budget, "verify": args.verify,
                "quick": args.quick,
            },
            "aggregate": agg,
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

    return 1 if agg["scan_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
