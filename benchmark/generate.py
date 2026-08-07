"""Write scenario suites to disk and validate their manifests.

Usage:
    python -m benchmark.generate --out DIR                  # full default matrix
    python -m benchmark.generate --out DIR --template od_env --size 30
    python -m benchmark.generate --out DIR --seed 7 --list
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .scenarios import (
    KIND_ALWAYS_FAILING,
    KIND_NOD,
    KIND_OD_VICTIM,
    MANIFEST_VERSION,
    TEMPLATES,
    Scenario,
    build_matrix,
)

_VALID_KINDS = {KIND_OD_VICTIM, KIND_NOD, KIND_ALWAYS_FAILING}
_VALID_CAUSES = {"order_pollution", "hashseed", "rngseed", "broken"}


def validate_manifest(m: dict) -> list[str]:
    """Hand-rolled schema check; returns a list of problems, empty if valid."""
    errors: list[str] = []

    def need(key: str, typ: type) -> None:
        if key not in m:
            errors.append(f"missing key: {key}")
        elif not isinstance(m[key], typ):
            errors.append(f"{key}: expected {typ.__name__}, got {type(m[key]).__name__}")

    need("manifest_version", int)
    need("scenario_id", str)
    need("template", str)
    need("seed", int)
    need("suite_size", int)
    need("flaky_tests", list)
    need("stable_tests", list)

    if m.get("manifest_version") != MANIFEST_VERSION:
        errors.append(f"manifest_version: expected {MANIFEST_VERSION}")

    stable = set(m.get("stable_tests", []))
    for i, f in enumerate(m.get("flaky_tests", [])):
        where = f"flaky_tests[{i}]"
        if not isinstance(f, dict):
            errors.append(f"{where}: not an object")
            continue
        if f.get("kind") not in _VALID_KINDS:
            errors.append(f"{where}.kind: {f.get('kind')!r} not in {sorted(_VALID_KINDS)}")
        if f.get("cause") not in _VALID_CAUSES:
            errors.append(f"{where}.cause: {f.get('cause')!r} not in {sorted(_VALID_CAUSES)}")
        if not isinstance(f.get("test_id"), str) or "::" not in f.get("test_id", ""):
            errors.append(f"{where}.test_id: not a pytest node id")
        if f.get("test_id") in stable:
            errors.append(f"{where}: {f['test_id']} also listed as stable")
        if f.get("kind") == KIND_OD_VICTIM:
            if not f.get("polluters"):
                errors.append(f"{where}: od_victim without polluters")
            for p in f.get("polluters", []):
                if p not in stable:
                    errors.append(f"{where}: polluter {p} missing from stable_tests")

    declared = len(stable) + len(m.get("flaky_tests", []))
    if declared != m.get("suite_size"):
        errors.append(f"suite_size {m.get('suite_size')} != {declared} declared tests")

    return errors


def write_scenario(scenario: Scenario, out_root: Path) -> Path:
    """Materialize one scenario as a pytest-runnable directory."""
    root = out_root / scenario.scenario_id
    for relpath, content in scenario.files.items():
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    manifest = scenario.manifest()
    problems = validate_manifest(manifest)
    if problems:
        raise ValueError(f"{scenario.scenario_id}: invalid manifest: {problems}")
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--template", choices=sorted(TEMPLATES),
                        help="generate one template only (default: full matrix)")
    parser.add_argument("--size", type=int, default=10,
                        help="suite size for --template (default 10)")
    parser.add_argument("--list", action="store_true",
                        help="print scenario ids without writing anything")
    args = parser.parse_args(argv)

    if args.template:
        scenarios = [TEMPLATES[args.template](args.size, args.seed)]
    else:
        scenarios = build_matrix(args.seed)

    if args.list:
        for s in scenarios:
            print(s.scenario_id)
        return 0

    for s in scenarios:
        root = write_scenario(s, args.out)
        n_flaky = len(s.flakes)
        print(f"wrote {root}  ({s.suite_size} tests, {n_flaky} flaky)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
