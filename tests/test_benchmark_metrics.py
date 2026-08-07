"""Scorer unit tests against hand-built report dicts (no real scans), plus one
live end-to-end run through the benchmark runner."""
from pathlib import Path

from benchmark.metrics import aggregate, markdown_table, score_scenario
from benchmark.run import run_one
from benchmark.scenarios import TEMPLATES


def _manifest(template="od_env", size=10, seed=1):
    return TEMPLATES[template](size, seed).manifest()


def _od_report(manifest, polluter=None, kind="order-dependent (victim)",
               extra_flakes=()):
    truth = manifest["flaky_tests"][0]
    entry = {
        "test_id": truth["test_id"],
        "kind": kind,
        "diagnosis": {
            "cause": "test-order dependence (state pollution)",
            "polluters": [polluter or truth["polluters"][0]],
        },
        "patch": {"verified": True},
    }
    return {
        "cost": {"trials": 40, "wall_seconds": 5.0, "exhausted": False},
        "flakes": [entry, *extra_flakes],
    }


def test_perfect_od_scenario_scores_clean():
    m = _manifest()
    s = score_scenario(m, _od_report(m))
    f = s.flakes[0]
    assert f.detected and f.kind_correct and f.cause_correct and f.localized
    assert f.fix_verified
    assert s.false_positives == []


def test_wrong_polluter_fails_localization_only():
    m = _manifest()
    s = score_scenario(m, _od_report(m, polluter=m["stable_tests"][0]))
    f = s.flakes[0]
    assert f.detected and f.cause_correct
    assert f.localized is False


def test_missed_flake_is_a_false_negative():
    m = _manifest()
    s = score_scenario(m, {"cost": {}, "flakes": []})
    assert s.flakes[0].detected is False
    agg = aggregate([s])
    assert agg["detection"]["fn"] == 1
    assert agg["detection"]["recall"] == 0.0


def test_stable_test_reported_flaky_is_a_false_positive():
    m = _manifest()
    fp_entry = {"test_id": m["stable_tests"][0], "kind": "non-order-dependent"}
    s = score_scenario(m, _od_report(m, extra_flakes=[fp_entry]))
    assert s.false_positives == [m["stable_tests"][0]]
    agg = aggregate([s])
    assert agg["detection"]["fp"] == 1
    assert agg["detection"]["precision"] == 0.5


def test_nod_cause_needs_implicated_dimension():
    m = _manifest("nod_hashseed")
    truth = m["flaky_tests"][0]
    entry = {
        "test_id": truth["test_id"],
        "kind": "non-order-dependent",
        "diagnosis": {"cause": "seed-dependent nondeterminism",
                      "implicated": ["rngseed"]},  # wrong dimension
    }
    s = score_scenario(m, {"cost": {}, "flakes": [entry]})
    f = s.flakes[0]
    assert f.kind_correct and f.cause_correct is False


def test_always_failing_excluded_from_detection_math():
    m = _manifest("always_failing")
    truth = m["flaky_tests"][0]
    entry = {"test_id": truth["test_id"],
             "kind": "always failing (brittle or broken)"}
    s = score_scenario(m, {"cost": {}, "flakes": [entry]})
    agg = aggregate([s])
    assert agg["detection"]["tp"] == 0 and agg["detection"]["fn"] == 0
    assert agg["classification"]["kind_accuracy"] == 1.0


def test_scan_error_counts_all_flakes_as_missed():
    m = _manifest()
    s = score_scenario(m, None, error="scan timed out")
    assert s.error and s.flakes[0].detected is False
    agg = aggregate([s])
    assert agg["scan_errors"] == 1


def test_markdown_table_renders_all_rows():
    m = _manifest()
    s = score_scenario(m, _od_report(m))
    agg = aggregate([s])
    table = markdown_table([s], agg)
    assert m["scenario_id"] in table
    assert "precision" in table and "rank-1" in table


def test_run_one_live_od_scenario(tmp_path):
    """End to end through the real CLI: generate, scan, score. No --verify to
    keep it fast; the scan itself takes a few seconds."""
    scenario = TEMPLATES["od_module_global"](10, seed=1)
    report, error = run_one(scenario, tmp_path, verify=False,
                            budget=400, rounds=6)
    assert error is None, error
    score = score_scenario(scenario.manifest(), report)
    f = score.flakes[0]
    assert f.detected and f.kind_correct and f.cause_correct
    assert f.localized, "bisector should name the injected polluter"
    assert score.trials > 0
