"""Pytest plugin that Culpa's runner injects into every trial subprocess.

Loaded explicitly with ``-p culpa.plugin`` and activated only when
``CULPA_OUT`` is set, so it is inert in any other pytest invocation.

Responsibilities (driven entirely by environment variables):

- ``CULPA_OUT``       — path of the JSONL results file to append to (activation flag)
- ``CULPA_ORDER``     — path of a JSON list of node ids; run exactly these, in this order
- ``CULPA_RNG_SEED``  — seed ``random`` (and numpy if present) at session start
- ``CULPA_STATEDIFF`` — "1" to snapshot/diff process state around every test
- ``CULPA_ROOT``      — repo root; module-global snapshots are limited to modules under it
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import warnings

import pytest

_ENV_IGNORE_PREFIXES = ("CULPA_", "PYTEST_")
_REPR_LIMIT = 300
_SIMPLE_TYPES = (int, float, str, bool, bytes, type(None))
_CONTAINER_TYPES = (list, dict, set, tuple, frozenset)

# per-process accumulators
_outcomes: dict[str, dict] = {}


def _active() -> bool:
    return bool(os.environ.get("CULPA_OUT"))


# --------------------------------------------------------------------------- #
# error normalization
# --------------------------------------------------------------------------- #

_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")
_LINE_RE = re.compile(r"line \d+")
_PATH_RE = re.compile(r"(/[^\s:'\"]+)+/")
_NUM_RE = re.compile(r"\d+\.\d+")
_BIGINT_RE = re.compile(r"\d{4,}")


def normalize_error(text: str) -> str:
    """Strip addresses, paths, line numbers, floats — so 'flaky for the same
    reason' hashes identically across trials."""
    text = _ADDR_RE.sub("0xADDR", text)
    text = _PATH_RE.sub("", text)
    text = _LINE_RE.sub("line N", text)
    text = _NUM_RE.sub("F", text)
    text = _BIGINT_RE.sub("N", text)  # hash values, ids, timestamps
    return text


def error_hash(text: str | None) -> str | None:
    if not text:
        return None
    return hashlib.sha1(normalize_error(text).encode("utf-8", "replace")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# state snapshot / diff
# --------------------------------------------------------------------------- #

def _bounded_repr(value) -> str | None:
    try:
        r = repr(value)
    except Exception:
        return None
    return r[:_REPR_LIMIT]


def _snapshot() -> dict:
    root = os.environ.get("CULPA_ROOT", "")
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(_ENV_IGNORE_PREFIXES)
    }
    modules: dict[str, dict[str, str]] = {}
    for name, mod in list(sys.modules.items()):
        try:
            mod_file = getattr(mod, "__file__", None)
        except Exception:
            continue
        if not mod_file or not root or not mod_file.startswith(root):
            continue
        attrs: dict[str, str] = {}
        try:
            items = list(vars(mod).items())
        except Exception:
            continue
        for attr, value in items:
            if attr.startswith("__"):
                continue
            if isinstance(value, _SIMPLE_TYPES) or isinstance(value, _CONTAINER_TYPES):
                r = _bounded_repr(value)
                if r is not None:
                    attrs[attr] = r
        modules[name] = attrs
    import random

    return {
        "env": env,
        "cwd": os.getcwd(),
        "modules": modules,
        "random": hashlib.sha1(repr(random.getstate()).encode()).hexdigest()[:12],
        "warnings": hashlib.sha1(repr(warnings.filters).encode()).hexdigest()[:12],
    }


def _diff_snapshots(before: dict, after: dict) -> dict:
    diff: dict = {}

    env_diff: dict[str, dict] = {}
    b_env, a_env = before["env"], after["env"]
    for k in set(b_env) | set(a_env):
        if b_env.get(k) != a_env.get(k):
            env_diff[k] = {"before": b_env.get(k), "after": a_env.get(k)}
    if env_diff:
        diff["env"] = env_diff

    if before["cwd"] != after["cwd"]:
        diff["cwd"] = {"before": before["cwd"], "after": after["cwd"]}

    mod_diff: dict[str, dict] = {}
    b_mods, a_mods = before["modules"], after["modules"]
    for mod in set(b_mods) & set(a_mods):
        changed = {}
        b_attrs, a_attrs = b_mods[mod], a_mods[mod]
        for attr in set(b_attrs) | set(a_attrs):
            if b_attrs.get(attr) != a_attrs.get(attr):
                changed[attr] = {
                    "before": b_attrs.get(attr),
                    "after": a_attrs.get(attr),
                }
        if changed:
            mod_diff[mod] = changed
    if mod_diff:
        diff["module_globals"] = mod_diff

    if before["random"] != after["random"]:
        diff["random_state_changed"] = True
    if before["warnings"] != after["warnings"]:
        diff["warnings_filters_changed"] = True
    return diff


# --------------------------------------------------------------------------- #
# pytest hooks
# --------------------------------------------------------------------------- #

def pytest_configure(config):
    if not _active():
        return
    seed = os.environ.get("CULPA_RNG_SEED")
    if seed is not None:
        import random

        random.seed(int(seed))
        try:
            import numpy  # noqa: F401

            numpy.random.seed(int(seed) % (2**32))
        except Exception:
            pass


def pytest_collection_modifyitems(config, items):
    if not _active():
        return
    order_file = os.environ.get("CULPA_ORDER")
    if not order_file:
        return
    with open(order_file) as f:
        wanted: list[str] = json.load(f)
    by_id = {item.nodeid: item for item in items}
    wanted_set = set(wanted)
    selected = [by_id[nodeid] for nodeid in wanted if nodeid in by_id]
    deselected = [item for item in items if item.nodeid not in wanted_set]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = selected


def pytest_runtest_logreport(report):
    if not _active():
        return
    rec = _outcomes.setdefault(
        report.nodeid, {"status": "passed", "duration_ms": 0.0, "error": None}
    )
    rec["duration_ms"] += getattr(report, "duration", 0.0) * 1000
    if report.skipped:
        rec["status"] = "skipped"
    elif report.failed:
        rec["status"] = "failed" if report.when == "call" else "error"
        rec["error"] = str(report.longrepr)[:4000] if report.longrepr else ""


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    if not _active():
        yield
        return
    statediff = os.environ.get("CULPA_STATEDIFF") == "1"
    before = _snapshot() if statediff else None
    yield
    state_diff = _diff_snapshots(before, _snapshot()) if statediff else None
    rec = _outcomes.pop(item.nodeid, {"status": "unknown", "duration_ms": 0.0, "error": None})
    line = {
        "test_id": item.nodeid,
        "status": rec["status"],
        "duration_ms": round(rec["duration_ms"], 3),
        "error_hash": error_hash(rec["error"]),
        "error": rec["error"][:2000] if rec["error"] else None,
        "state_diff": state_diff or None,
    }
    with open(os.environ["CULPA_OUT"], "a") as f:
        f.write(json.dumps(line) + "\n")
