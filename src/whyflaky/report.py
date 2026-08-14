"""Report — JSON for machines (the CI contract), readable text for humans."""
from __future__ import annotations

import json
from pathlib import Path


def write_json(report: dict, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    return path


def write_patches(report: dict, directory: str | Path) -> list[Path]:
    directory = Path(directory)
    written = []
    for entry in report["flakes"]:
        patch = entry.get("patch")
        if not patch or not patch.get("diff"):
            continue
        directory.mkdir(parents=True, exist_ok=True)
        safe = entry["test_id"].replace("/", "_").replace("::", "__")
        path = directory / f"{safe}.diff"
        path.write_text(patch["diff"])
        written.append(path)
    return written


def _pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def _ci(ci) -> str:
    lo, hi = ci
    return f"[{_pct(lo)}, {_pct(hi)}]"


def render_text(report: dict) -> str:
    lines: list[str] = []
    add = lines.append
    cost = report["cost"]
    add(f"whyflaky {report['whyflaky_version']} — scan of {report['repo']}")
    add(f"suite: {report['suite']['tests']} tests, {report['suite']['rounds']} rounds")
    add(f"cost: {cost['trials']} trials, {cost['wall_seconds']}s wall"
        f" (budget {cost['budget_trials']}"
        + (", EXHAUSTED — results partial)" if cost["exhausted"] else ")"))
    add("")

    flakes = report["flakes"]
    if not flakes:
        add("no flaky tests detected")
        return "\n".join(lines)

    add(f"{len(flakes)} flaky test(s) found")
    add("=" * 70)
    for entry in flakes:
        iso = entry["isolation"]
        add(f"\n● {entry['test_id']}")
        add(f"  kind: {entry['kind']}")
        add(f"  in suite: failed {entry['suite_failures']}/{entry['suite_trials']}"
            f" rounds, Wilson CI {_ci(entry['suite_ci'])}")
        add(f"  alone:    failed {iso['failures']}/{iso['trials']}"
            f" ({_pct(iso['failure_rate'])}), CI {_ci(iso['ci'])} -> {iso['verdict']}")
        if entry.get("note"):
            add(f"  note: {entry['note']}")

        diag = entry.get("diagnosis")
        if diag:
            add(f"  cause: {diag.get('cause')}")
            if diag.get("polluters"):
                conf = diag.get("confidence", "?")
                add(f"  polluter(s): {', '.join(diag['polluters'])}"
                    f" (confidence: {conf}, "
                    f"{diag.get('oracle_queries', '?')} oracle queries, "
                    f"{diag.get('bisection_trials', '?')} trials)")
            for item in diag.get("polluted_state", []):
                add(f"    polluted state: {item}")
            for test_id, diff in (diag.get("state_evidence") or {}).items():
                for key, change in diff.get("env", {}).items():
                    add(f"    {test_id}: os.environ[{key!r}]:"
                        f" {change['before']!r} -> {change['after']!r}")
                for mod, attrs in diff.get("module_globals", {}).items():
                    for attr, change in attrs.items():
                        add(f"    {test_id}: {mod}.{attr}:"
                            f" {change['before']} -> {change['after']}")
            if diag.get("dimensions"):
                ctrl = diag["control"]
                add(f"  control (all seeds pinned): "
                    f"{ctrl['failures']}/{ctrl['trials']} failed")
                for dim, d in diag["dimensions"].items():
                    mark = " <== implicated" if dim in diag.get("implicated", []) else ""
                    add(f"    vary {dim}: {d['failures']}/{d['trials']} failed,"
                        f" p={d['pvalue']:.4f}{mark}")
            if diag.get("note"):
                add(f"  diagnosis note: {diag['note']}")

        patch = entry.get("patch")
        if patch:
            add(f"  proposed fix ({patch['tier']} tier): {patch['description']}")
            verification = patch.get("verification")
            if verification is not None:
                status = "VERIFIED" if patch["verified"] else "NOT VERIFIED"
                add(f"  verification: {status}"
                    f" (replay {verification['replay_failures']}/"
                    f"{verification['replay_trials']} failures,"
                    f" regression {'ok' if verification['regression_ok'] else 'FAIL'},"
                    f" semantic {'ok' if verification['semantic_ok'] else 'FAIL'})")
                if verification.get("note"):
                    add(f"    {verification['note']}")
    return "\n".join(lines)
