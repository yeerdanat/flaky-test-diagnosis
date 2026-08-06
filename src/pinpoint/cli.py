"""Pinpoint CLI.

    pinpoint scan [path] [--rounds N] [--budget N] [--fix] [--verify] [--json PATH]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .orchestrator import Orchestrator
from .report import render_text, write_json, write_patches


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pinpoint",
        description="Flaky test detective: find flaky tests, isolate why, "
                    "propose verified fixes.",
    )
    parser.add_argument("--version", action="version", version=f"pinpoint {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="scan a pytest suite for flaky tests")
    scan.add_argument("path", nargs="?", default=".", help="repo root (default: .)")
    scan.add_argument("--rounds", type=int, default=6,
                      help="full-suite detection rounds; round 0 is collection "
                           "order, the rest are shuffled (default: 6)")
    scan.add_argument("--budget", type=int, default=400, metavar="TRIALS",
                      help="max total trials; partial results on exhaustion "
                           "(default: 400)")
    scan.add_argument("--baseline-trials", type=int, default=25,
                      help="max isolation-baseline trials per candidate (SPRT "
                           "usually stops much earlier; default: 25)")
    scan.add_argument("--screen-trials", type=int, default=12,
                      help="trials per perturbation condition when screening "
                           "non-order-dependent flakes (default: 12)")
    scan.add_argument("--fix", action="store_true",
                      help="synthesize candidate patches (.diff files, never applied)")
    scan.add_argument("--verify", action="store_true",
                      help="verify each patch: statistical replay + regression "
                           "+ semantic check (implies --fix)")
    scan.add_argument("--json", metavar="PATH", default=None,
                      help="JSON report path (default: <path>/.pinpoint/report.json)")
    scan.add_argument("--db", metavar="PATH", default=None,
                      help="SQLite history path (default: <path>/.pinpoint/pinpoint.db)")
    scan.add_argument("--fail-on-flake", action="store_true",
                      help="exit 1 if any flaky test is found (CI mode)")
    scan.add_argument("-q", "--quiet", action="store_true",
                      help="suppress progress output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = Path(args.path).resolve()
    if not repo.exists():
        print(f"error: {repo} does not exist", file=sys.stderr)
        return 2

    log = (lambda *_a, **_k: None) if args.quiet else print
    orchestrator = Orchestrator(
        repo,
        rounds=args.rounds,
        baseline_max_trials=args.baseline_trials,
        screen_trials=args.screen_trials,
        max_trials=args.budget,
        make_fixes=args.fix,
        verify_fixes=args.verify,
        db_path=args.db,
        log=log,
    )
    try:
        report = orchestrator.scan()
    finally:
        orchestrator.store.close()

    json_path = Path(args.json) if args.json else repo / ".pinpoint" / "report.json"
    write_json(report, json_path)
    patch_paths = write_patches(report, json_path.parent / "patches")

    print()
    print(render_text(report))
    print()
    print(f"json report: {json_path}")
    for p in patch_paths:
        print(f"patch: {p}")

    if args.fail_on_flake and report["flakes"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
