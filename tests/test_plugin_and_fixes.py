"""Unit tests for error normalization, fix synthesis, and the semantic guard."""
from pathlib import Path

from whyflaky.fixes import (
    TIER_BALANCED,
    TIER_CONSERVATIVE,
    fix_for_hashseed,
    fix_for_od,
    fix_for_rngseed,
)
from whyflaky.plugin import error_hash, normalize_error
from whyflaky.verifier import _test_weakening


class TestErrorNormalization:
    def test_paths_addresses_lines_stripped(self):
        a = ("Traceback at /Users/alice/proj/test_x.py, line 42: "
             "<Foo object at 0x10ab3f2d0> took 0.031s")
        b = ("Traceback at /home/ci/build/test_x.py, line 97: "
             "<Foo object at 0x7ffee01a2b40> took 1.882s")
        assert normalize_error(a) == normalize_error(b)
        assert error_hash(a) == error_hash(b)

    def test_different_errors_differ(self):
        assert error_hash("AssertionError: x == 1") != error_hash("KeyError: 'y'")

    def test_none_and_empty(self):
        assert error_hash(None) is None
        assert error_hash("") is None


class TestFixSynthesis:
    def test_od_fix_restores_named_state(self, tmp_path: Path):
        evidence = {
            "test_billing.py::test_eur": {
                "env": {"APP_CURRENCY": {"before": None, "after": "EUR"}},
                "module_globals": {
                    "app.state": {"CURRENCY": {"before": "'USD'", "after": "'EUR'"}}
                },
            }
        }
        patch = fix_for_od(tmp_path, "test_invoice.py::test_usd",
                           ["test_billing.py::test_eur"], evidence)
        assert patch is not None
        assert patch.tier == TIER_BALANCED
        _old, new = patch.files["conftest.py"]
        assert "APP_CURRENCY" in new
        assert "app.state" in new
        assert "CURRENCY" in new
        assert "+++ b/conftest.py" in patch.diff_text
        compile(new, "conftest.py", "exec")  # generated code must parse

    def test_od_fix_without_evidence_declines(self, tmp_path: Path):
        assert fix_for_od(tmp_path, "v", ["p"], {}) is None

    def test_od_fix_appends_to_existing_conftest(self, tmp_path: Path):
        (tmp_path / "conftest.py").write_text("EXISTING = 1\n")
        patch = fix_for_od(
            tmp_path, "v", ["p"],
            {"p": {"env": {"K": {"before": "a", "after": "b"}}}},
        )
        _old, new = patch.files["conftest.py"]
        assert new.startswith("EXISTING = 1\n")
        compile(new, "conftest.py", "exec")

    def test_rng_fix_is_conservative_and_parses(self, tmp_path: Path):
        patch = fix_for_rngseed(tmp_path)
        assert patch.tier == TIER_CONSERVATIVE
        compile(patch.files["conftest.py"][1], "conftest.py", "exec")

    def test_hashseed_fix_parses(self, tmp_path: Path):
        patch = fix_for_hashseed(tmp_path)
        compile(patch.files["conftest.py"][1], "conftest.py", "exec")
        assert "PYTHONHASHSEED" in patch.files["conftest.py"][1]


class TestSemanticGuard:
    def test_assertion_deletion_caught(self):
        old = "def test_a():\n    assert 1\n    assert 2\n"
        new = "def test_a():\n    assert 1\n"
        assert _test_weakening(old, new)

    def test_skip_addition_caught(self):
        old = "def test_a():\n    assert 1\n"
        new = "import pytest\n@pytest.mark.skip\ndef test_a():\n    assert 1\n"
        assert _test_weakening(old, new)

    def test_pure_addition_ok(self):
        old = "def test_a():\n    assert 1\n"
        new = old + "\n\ndef helper():\n    return 3\n"
        assert _test_weakening(old, new) == []

    def test_new_file_ok(self):
        assert _test_weakening(None, "import pytest\n") == []
