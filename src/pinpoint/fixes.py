"""Fix synthesizer — emits risk-tiered candidate patches (design doc §9).

v1 covers the two v1 flake categories:

- order-dependent pollution  -> autouse fixture that saves/restores the
  *specific* polluted state named by the state diff (balanced tier)
- unseeded RNG               -> autouse fixture seeding random/numpy (conservative)
- hash-seed dependence       -> conftest guard that re-execs with a pinned
  PYTHONHASHSEED (balanced; the conservative alternative — set the env var in
  CI — is stated in the description)

Patches are emitted as unified diffs for human review. Pinpoint never
commits and never mutates the working tree unless --apply (not in v1).
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path

TIER_CONSERVATIVE = "conservative"
TIER_BALANCED = "balanced"


@dataclass
class Patch:
    tier: str
    cause: str
    description: str
    # path (relative to repo root) -> (old_content or None, new_content)
    files: dict[str, tuple[str | None, str]] = field(default_factory=dict)

    @property
    def diff_text(self) -> str:
        chunks = []
        for path, (old, new) in self.files.items():
            old_lines = (old or "").splitlines(keepends=True)
            new_lines = new.splitlines(keepends=True)
            chunks.append("".join(difflib.unified_diff(
                old_lines, new_lines,
                fromfile=f"a/{path}", tofile=f"b/{path}",
            )))
        return "\n".join(chunks)


def _read_conftest(repo: Path) -> str | None:
    conftest = repo / "conftest.py"
    return conftest.read_text() if conftest.exists() else None


def _append_to_conftest(old: str | None, block: str) -> str:
    base = old or ""
    if base and not base.endswith("\n"):
        base += "\n"
    return base + ("\n" if base else "") + block


def fix_for_od(
    repo: Path,
    victim: str,
    polluters: list[str],
    state_evidence: dict,
) -> Patch | None:
    """Autouse fixture restoring exactly the state the polluter(s) changed."""
    env_keys: set[str] = set()
    module_attrs: dict[str, set[str]] = {}
    for diff in state_evidence.values():
        env_keys.update(diff.get("env", {}).keys())
        for mod, attrs in diff.get("module_globals", {}).items():
            module_attrs.setdefault(mod, set()).update(attrs.keys())

    if not env_keys and not module_attrs:
        return None  # nothing concrete to restore; don't emit a guess

    lines = [
        "# --- added by pinpoint: restore state polluted by "
        + ", ".join(polluters) + " ---",
        "import os as _pinpoint_os",
        "import pytest as _pinpoint_pytest",
        "",
        "",
        "@_pinpoint_pytest.fixture(autouse=True)",
        "def _pinpoint_restore_polluted_state():",
    ]
    body = []
    if env_keys:
        keys = sorted(env_keys)
        body.append(f"    _saved_env = {{k: _pinpoint_os.environ.get(k) for k in {keys!r}}}")
    for i, (mod, attrs) in enumerate(sorted(module_attrs.items())):
        body.append(f"    import {mod} as _pinpoint_mod{i}")
        for attr in sorted(attrs):
            body.append(f"    _saved_{i}_{attr} = getattr(_pinpoint_mod{i}, {attr!r}, None)")
    body.append("    yield")
    if env_keys:
        body += [
            "    for k, v in _saved_env.items():",
            "        if v is None:",
            "            _pinpoint_os.environ.pop(k, None)",
            "        else:",
            "            _pinpoint_os.environ[k] = v",
        ]
    for i, (mod, attrs) in enumerate(sorted(module_attrs.items())):
        for attr in sorted(attrs):
            body.append(f"    setattr(_pinpoint_mod{i}, {attr!r}, _saved_{i}_{attr})")
    block = "\n".join(lines + body) + "\n"

    old = _read_conftest(repo)
    what = []
    if env_keys:
        what.append(f"os.environ keys {sorted(env_keys)}")
    for mod, attrs in sorted(module_attrs.items()):
        what.append(f"{mod}.{{{', '.join(sorted(attrs))}}}")
    return Patch(
        tier=TIER_BALANCED,
        cause="order-dependent pollution",
        description=(
            f"Autouse fixture in conftest.py saves/restores {' and '.join(what)} "
            f"around every test, isolating {victim} from pollution by "
            f"{', '.join(polluters)}. Review whether the polluter itself should "
            "clean up instead (iFixFlakies-style cleaner insertion is the "
            "aggressive-tier alternative)."
        ),
        files={"conftest.py": (old, _append_to_conftest(old, block))},
    )


def fix_for_rngseed(repo: Path) -> Patch:
    """Conservative: seed the RNG in an autouse fixture."""
    block = (
        "# --- added by pinpoint: pin RNG seed (unseeded-randomness flake) ---\n"
        "import pytest as _pinpoint_pytest\n"
        "\n"
        "\n"
        "@_pinpoint_pytest.fixture(autouse=True)\n"
        "def _pinpoint_seed_rng():\n"
        "    import random\n"
        "    random.seed(0)\n"
        "    try:\n"
        "        import numpy\n"
        "        numpy.random.seed(0)\n"
        "    except ImportError:\n"
        "        pass\n"
        "    yield\n"
    )
    old = _read_conftest(repo)
    return Patch(
        tier=TIER_CONSERVATIVE,
        cause="unseeded randomness",
        description=(
            "Autouse fixture in conftest.py seeds random (and numpy if present) "
            "before every test. Cannot change what any assertion checks; it only "
            "makes the input distribution deterministic."
        ),
        files={"conftest.py": (old, _append_to_conftest(old, block))},
    )


def fix_for_hashseed(repo: Path) -> Patch:
    """Pin PYTHONHASHSEED by re-exec at conftest import time."""
    block = (
        "# --- added by pinpoint: pin PYTHONHASHSEED (hash-order flake) ---\n"
        "# The hash seed must be fixed before interpreter start, so if it is\n"
        "# unset we re-exec the test process once with it pinned.\n"
        "import os as _pinpoint_os\n"
        "import sys as _pinpoint_sys\n"
        "\n"
        "if _pinpoint_os.environ.get(\"PYTHONHASHSEED\") is None:\n"
        "    _pinpoint_os.environ[\"PYTHONHASHSEED\"] = \"0\"\n"
        "    _pinpoint_os.execv(\n"
        "        _pinpoint_sys.executable,\n"
        "        [_pinpoint_sys.executable] + _pinpoint_sys.argv,\n"
        "    )\n"
    )
    old = _read_conftest(repo)
    return Patch(
        tier=TIER_BALANCED,
        cause="hash-seed dependence (dict/set iteration order)",
        description=(
            "conftest.py guard re-execs pytest once with PYTHONHASHSEED=0 when "
            "unset. Conservative alternative: export PYTHONHASHSEED=0 in CI and "
            "dev shells instead of patching conftest. Note the real fix is to "
            "remove the iteration-order assumption from the test."
        ),
        files={"conftest.py": (old, _append_to_conftest(old, block))},
    )
