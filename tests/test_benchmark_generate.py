"""Generator acceptance: determinism, manifest validity, and behavioral ground
truth (the injected flakes actually flake the way the manifest claims)."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from benchmark.generate import validate_manifest, write_scenario
from benchmark.scenarios import TEMPLATES, build_matrix


def _pytest_run(repo: Path, node_ids: list[str], env_extra: dict | None = None) -> int:
    """Run pytest on the given node ids; return the exit code."""
    import os
    env = dict(os.environ)
    env.pop("PYTHONHASHSEED", None)
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *node_ids],
        cwd=repo, env=env, capture_output=True, text=True,
    )
    return proc.returncode


def test_generation_is_deterministic():
    a = build_matrix(seed=1)
    b = build_matrix(seed=1)
    for sa, sb in zip(a, b):
        assert sa.scenario_id == sb.scenario_id
        assert sa.files == sb.files
        assert sa.manifest() == sb.manifest()


def test_different_seed_changes_distractors():
    a = TEMPLATES["od_env"](10, 1)
    b = TEMPLATES["od_env"](10, 2)
    assert a.files != b.files


def test_all_matrix_manifests_validate():
    for s in build_matrix(seed=1):
        assert validate_manifest(s.manifest()) == [], s.scenario_id


def test_declared_suite_sizes_are_exact():
    for s in build_matrix(seed=1):
        declared = len(s.stable_tests) + len(s.flakes)
        assert declared == s.suite_size, s.scenario_id


def test_all_scenarios_collect_under_pytest(tmp_path):
    for s in build_matrix(seed=1):
        root = write_scenario(s, tmp_path)
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q",
             "-p", "no:cacheprovider"],
            cwd=root, capture_output=True, text=True,
        )
        assert proc.returncode == 0, f"{s.scenario_id}:\n{proc.stdout}{proc.stderr}"
        collected = [l for l in proc.stdout.splitlines() if "::" in l]
        assert len(collected) == s.suite_size, s.scenario_id


@pytest.mark.parametrize("template", ["od_module_global", "od_env", "od_cwd"])
def test_od_victim_passes_alone_and_fails_after_polluter(template, tmp_path):
    s = TEMPLATES[template](10, seed=1)
    root = write_scenario(s, tmp_path)
    flake = s.flakes[0]
    victim, polluter = flake.test_id, flake.polluters[0]

    assert _pytest_run(root, [victim]) == 0, "victim must pass alone"
    assert _pytest_run(root, [polluter, victim]) != 0, \
        "victim must fail after the polluter in the same process"


def test_always_failing_fails_alone(tmp_path):
    s = TEMPLATES["always_failing"](10, seed=1)
    root = write_scenario(s, tmp_path)
    assert _pytest_run(root, [s.flakes[0].test_id]) != 0


def test_stable_suite_passes(tmp_path):
    s = TEMPLATES["stable"](10, seed=1)
    root = write_scenario(s, tmp_path)
    assert _pytest_run(root, []) == 0


def test_rng_flake_rate_is_near_design(tmp_path):
    s = TEMPLATES["nod_rng"](10, seed=1, fail_rate=0.4)
    root = write_scenario(s, tmp_path)
    victim = s.flakes[0].test_id
    fails = sum(_pytest_run(root, [victim]) != 0 for _ in range(30))
    # designed 40%; 30 Bernoulli trials, accept a generous band
    assert 4 <= fails <= 22, f"observed {fails}/30 failures"


def test_hashseed_flake_depends_on_seed_only(tmp_path):
    s = TEMPLATES["nod_hashseed"](10, seed=1)
    root = write_scenario(s, tmp_path)
    victim = s.flakes[0].test_id
    outcomes = {
        seed: _pytest_run(root, [victim], {"PYTHONHASHSEED": str(seed)})
        for seed in ("0", "1", "2", "3", "4", "5", "6", "7")
    }
    # deterministic per seed
    for seed, rc in outcomes.items():
        assert _pytest_run(root, [victim], {"PYTHONHASHSEED": seed}) == rc
    # and both outcomes occur across seeds
    assert len(set(outcomes.values())) == 2, outcomes


def test_manifest_validator_catches_broken_manifests():
    m = build_matrix(seed=1)[0].manifest()
    assert validate_manifest(m) == []
    bad = dict(m)
    bad["flaky_tests"] = [dict(m["flaky_tests"][0], kind="bogus")]
    assert validate_manifest(bad)
    bad2 = dict(m)
    bad2["suite_size"] = 999
    assert validate_manifest(bad2)
