"""Flake templates and the default scenario matrix.

Every template is a pure function of (suite_size, seed, params): the same
inputs always generate byte-identical suites, so published benchmark numbers
are reproducible from the scenario id alone.

Ground-truth vocabulary (manifest `kind` / `cause`) is deliberately abstract;
the scorer in commit 2 maps whyflaky's report strings onto it:

    kind:  od_victim | nod | always_failing
    cause: order_pollution | hashseed | rngseed | broken
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

MANIFEST_VERSION = 1

KIND_OD_VICTIM = "od_victim"
KIND_NOD = "nod"
KIND_ALWAYS_FAILING = "always_failing"

CAUSE_ORDER = "order_pollution"
CAUSE_HASHSEED = "hashseed"
CAUSE_RNGSEED = "rngseed"
CAUSE_BROKEN = "broken"


@dataclass
class FlakeSpec:
    """Ground truth for one injected flake."""
    test_id: str
    kind: str
    cause: str
    polluters: list[str] = field(default_factory=list)
    polluted_state: list[str] = field(default_factory=list)
    # For rng flakes: the designed per-trial failure probability.
    expected_failure_rate: float | None = None

    def to_dict(self) -> dict:
        return {
            "test_id": self.test_id,
            "kind": self.kind,
            "cause": self.cause,
            "polluters": self.polluters,
            "polluted_state": self.polluted_state,
            "expected_failure_rate": self.expected_failure_rate,
        }


@dataclass
class Scenario:
    scenario_id: str
    template: str
    seed: int
    suite_size: int
    files: dict[str, str]          # relpath -> file content
    flakes: list[FlakeSpec]
    stable_tests: list[str]
    notes: str = ""

    def manifest(self) -> dict:
        return {
            "manifest_version": MANIFEST_VERSION,
            "scenario_id": self.scenario_id,
            "template": self.template,
            "seed": self.seed,
            "suite_size": self.suite_size,
            "flaky_tests": [f.to_dict() for f in self.flakes],
            "stable_tests": self.stable_tests,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# building blocks

_APP_INIT = ""

_APP_STATE = '''\
"""Shared application state. The pollution target for OD scenarios."""

MODE = "plain"
RETRIES = 3


def describe() -> str:
    return f"mode={MODE} retries={RETRIES}"
'''

_WORDS = [
    "amber", "basalt", "cedar", "delta", "ember", "fjord", "garnet",
    "harbor", "indigo", "juniper", "krypton", "lumen", "marble", "nickel",
]


def _distractor_file(index: int, rng: random.Random, n_tests: int) -> tuple[str, str, list[str]]:
    """A file of stable tests. Read-only use of app.state so distractors are
    plausible bisection candidates without being polluters."""
    fname = f"test_m{index:02d}_misc.py"
    lines = ["from app import state", "", ""]
    ids = []
    for t in range(n_tests):
        a, b = rng.randint(2, 9), rng.randint(2, 9)
        word = rng.choice(_WORDS)
        test = f"test_calc_{t}"
        lines += [
            f"def {test}():",
            f"    assert {a} * {b} == {a * b}",
            f"    assert '{word}'.upper() == '{word.upper()}'",
            "    assert state.RETRIES == 3",
            "",
            "",
        ]
        ids.append(f"{fname}::{test}")
    return fname, "\n".join(lines).rstrip() + "\n", ids


def _base_suite(rng: random.Random, suite_size: int, reserved: int) -> tuple[dict[str, str], list[str]]:
    """App package plus enough distractor files to reach suite_size tests."""
    files = {"app/__init__.py": _APP_INIT, "app/state.py": _APP_STATE}
    stable: list[str] = []
    need = suite_size - reserved
    index = 0
    while need > 0:
        per_file = min(need, rng.randint(2, 4))
        fname, content, ids = _distractor_file(index, rng, per_file)
        files[fname] = content
        stable += ids
        need -= per_file
        index += 1
    return files, stable


def _pair_positions(rng: random.Random, polluter_first: bool) -> tuple[str, str]:
    """File-name prefixes that fix collection order for polluter and victim.

    Distractors occupy m00..m39; a## sorts before them, z## after, so the pair
    lands at the edges and the order between the two is exact.
    """
    if polluter_first:
        return "test_a01_polluter.py", "test_z01_victim.py"
    return "test_z02_polluter.py", "test_a02_victim.py"


# ---------------------------------------------------------------------------
# templates

def od_module_global(suite_size: int, seed: int, polluter_first: bool = True) -> Scenario:
    rng = random.Random(seed)
    files, stable = _base_suite(rng, suite_size, reserved=2)
    pol_file, vic_file = _pair_positions(rng, polluter_first)

    files[pol_file] = '''\
"""Mutates a module global and never restores it."""
from app import state


def test_fancy_mode_description():
    state.MODE = "fancy"
    assert state.describe() == "mode=fancy retries=3"
'''
    files[vic_file] = '''\
"""Passes alone, fails after the polluter has run in the same process."""
from app import state


def test_default_mode_description():
    assert state.MODE == "plain"
    assert state.describe() == "mode=plain retries=3"
'''
    polluter = f"{pol_file}::test_fancy_mode_description"
    victim = f"{vic_file}::test_default_mode_description"
    return Scenario(
        scenario_id=f"od_module_global_s{suite_size}_seed{seed}"
                    f"{'' if polluter_first else '_rev'}",
        template="od_module_global",
        seed=seed,
        suite_size=suite_size,
        files=files,
        flakes=[FlakeSpec(
            test_id=victim,
            kind=KIND_OD_VICTIM,
            cause=CAUSE_ORDER,
            polluters=[polluter],
            polluted_state=["app.state.MODE"],
        )],
        stable_tests=stable + [polluter],
        notes="polluter %s victim in collection order"
              % ("precedes" if polluter_first else "follows"),
    )


def od_env(suite_size: int, seed: int, polluter_first: bool = True) -> Scenario:
    rng = random.Random(seed)
    files, stable = _base_suite(rng, suite_size, reserved=2)
    pol_file, vic_file = _pair_positions(rng, polluter_first)

    files[pol_file] = '''\
"""Sets an environment variable and never unsets it."""
import os


def test_eu_region_configuration():
    os.environ["BENCH_REGION"] = "eu"
    assert os.environ["BENCH_REGION"] == "eu"
'''
    files[vic_file] = '''\
"""Assumes the environment is clean."""
import os


def test_default_region_is_us():
    assert os.environ.get("BENCH_REGION", "us") == "us"
'''
    polluter = f"{pol_file}::test_eu_region_configuration"
    victim = f"{vic_file}::test_default_region_is_us"
    return Scenario(
        scenario_id=f"od_env_s{suite_size}_seed{seed}"
                    f"{'' if polluter_first else '_rev'}",
        template="od_env",
        seed=seed,
        suite_size=suite_size,
        files=files,
        flakes=[FlakeSpec(
            test_id=victim,
            kind=KIND_OD_VICTIM,
            cause=CAUSE_ORDER,
            polluters=[polluter],
            polluted_state=["os.environ['BENCH_REGION']"],
        )],
        stable_tests=stable + [polluter],
        notes="polluter %s victim in collection order"
              % ("precedes" if polluter_first else "follows"),
    )


def od_cwd(suite_size: int, seed: int, polluter_first: bool = True) -> Scenario:
    """Polluter chdirs into a subdirectory and never returns. The victim uses
    a repo-root-relative path. The runner pins cwd=repo per trial, so the
    victim passes alone and fails after the polluter in the same process."""
    rng = random.Random(seed)
    files, stable = _base_suite(rng, suite_size, reserved=2)
    pol_file, vic_file = _pair_positions(rng, polluter_first)

    files["data/config.txt"] = "flavor=vanilla\n"
    files[pol_file] = '''\
"""Changes cwd and never restores it."""
import os


def test_reads_config_from_data_dir():
    os.chdir("data")
    with open("config.txt") as f:
        assert f.read().startswith("flavor=")
'''
    files[vic_file] = '''\
"""Assumes cwd is the repo root."""


def test_config_path_from_repo_root():
    with open("data/config.txt") as f:
        assert f.read().startswith("flavor=")
'''
    polluter = f"{pol_file}::test_reads_config_from_data_dir"
    victim = f"{vic_file}::test_config_path_from_repo_root"
    return Scenario(
        scenario_id=f"od_cwd_s{suite_size}_seed{seed}"
                    f"{'' if polluter_first else '_rev'}",
        template="od_cwd",
        seed=seed,
        suite_size=suite_size,
        files=files,
        flakes=[FlakeSpec(
            test_id=victim,
            kind=KIND_OD_VICTIM,
            cause=CAUSE_ORDER,
            polluters=[polluter],
            polluted_state=["cwd"],
        )],
        stable_tests=stable + [polluter],
        notes="polluter %s victim in collection order"
              % ("precedes" if polluter_first else "follows"),
    )


def nod_hashseed(suite_size: int, seed: int) -> Scenario:
    """String-hash parity assumption: passes on roughly half of all
    PYTHONHASHSEED values, deterministic under any pinned seed."""
    rng = random.Random(seed)
    files, stable = _base_suite(rng, suite_size, reserved=1)
    word = rng.choice(_WORDS)

    fname = "test_z01_hashflake.py"
    files[fname] = f'''\
"""Depends on the interpreter hash seed (~50% of seeds fail)."""


def test_bucket_assignment_is_stable():
    assert hash("{word}") % 2 == 0
'''
    victim = f"{fname}::test_bucket_assignment_is_stable"
    return Scenario(
        scenario_id=f"nod_hashseed_s{suite_size}_seed{seed}",
        template="nod_hashseed",
        seed=seed,
        suite_size=suite_size,
        files=files,
        flakes=[FlakeSpec(
            test_id=victim,
            kind=KIND_NOD,
            cause=CAUSE_HASHSEED,
            expected_failure_rate=0.5,
        )],
        stable_tests=stable,
        notes=f"parity of hash({word!r})",
    )


def nod_rng(suite_size: int, seed: int, fail_rate: float = 0.4) -> Scenario:
    """Unseeded randomness with a designed per-trial failure probability."""
    rng = random.Random(seed)
    files, stable = _base_suite(rng, suite_size, reserved=1)

    fname = "test_z01_rngflake.py"
    files[fname] = f'''\
"""Unseeded randomness (fails ~{int(fail_rate * 100)}% of trials)."""
import random


def test_sampled_latency_within_budget():
    assert random.random() >= {fail_rate}
'''
    victim = f"{fname}::test_sampled_latency_within_budget"
    return Scenario(
        scenario_id=f"nod_rng{int(fail_rate * 100)}_s{suite_size}_seed{seed}",
        template="nod_rng",
        seed=seed,
        suite_size=suite_size,
        files=files,
        flakes=[FlakeSpec(
            test_id=victim,
            kind=KIND_NOD,
            cause=CAUSE_RNGSEED,
            expected_failure_rate=fail_rate,
        )],
        stable_tests=stable,
    )


def always_failing(suite_size: int, seed: int) -> Scenario:
    """Deterministic failure. Must be reported as broken/brittle, never as
    flaky; a tool that calls this flaky is hallucinating nondeterminism."""
    rng = random.Random(seed)
    files, stable = _base_suite(rng, suite_size, reserved=1)

    fname = "test_z01_broken.py"
    files[fname] = '''\
"""Plain broken test: fails every time, everywhere."""
from app import state


def test_retries_are_generous():
    assert state.RETRIES == 5
'''
    victim = f"{fname}::test_retries_are_generous"
    return Scenario(
        scenario_id=f"always_failing_s{suite_size}_seed{seed}",
        template="always_failing",
        seed=seed,
        suite_size=suite_size,
        files=files,
        flakes=[FlakeSpec(
            test_id=victim,
            kind=KIND_ALWAYS_FAILING,
            cause=CAUSE_BROKEN,
        )],
        stable_tests=stable,
    )


def stable(suite_size: int, seed: int) -> Scenario:
    """No flakes at all. Anything reported here is a false positive; this is
    the denominator for detection precision."""
    rng = random.Random(seed)
    files, stable_ids = _base_suite(rng, suite_size, reserved=0)
    return Scenario(
        scenario_id=f"stable_s{suite_size}_seed{seed}",
        template="stable",
        seed=seed,
        suite_size=suite_size,
        files=files,
        flakes=[],
        stable_tests=stable_ids,
    )


TEMPLATES = {
    "od_module_global": od_module_global,
    "od_env": od_env,
    "od_cwd": od_cwd,
    "nod_hashseed": nod_hashseed,
    "nod_rng": nod_rng,
    "always_failing": always_failing,
    "stable": stable,
}

# (template, suite_size, kwargs). Sizes trade coverage against sweep runtime;
# the _rev variants place the polluter after the victim in collection order,
# so only shuffled detection rounds can surface them.
DEFAULT_MATRIX: list[tuple[str, int, dict]] = [
    ("od_module_global", 10, {}),
    ("od_module_global", 30, {"polluter_first": False}),
    ("od_module_global", 80, {}),
    ("od_env", 10, {}),
    ("od_env", 30, {"polluter_first": False}),
    ("od_cwd", 10, {}),
    ("od_cwd", 30, {}),
    ("nod_hashseed", 10, {}),
    ("nod_hashseed", 30, {}),
    ("nod_rng", 10, {"fail_rate": 0.2}),
    ("nod_rng", 10, {"fail_rate": 0.4}),
    ("nod_rng", 30, {"fail_rate": 0.6}),
    ("always_failing", 10, {}),
    ("stable", 10, {}),
    ("stable", 30, {}),
]


def build_matrix(seed: int = 1) -> list[Scenario]:
    return [TEMPLATES[name](size, seed, **kwargs)
            for name, size, kwargs in DEFAULT_MATRIX]
