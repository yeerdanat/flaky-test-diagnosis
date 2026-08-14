"""Fixed-N baseline mode: the SPRT escape hatch, the comparator, and one live
two-arm run."""
import pytest

from benchmark.metrics import (
    baseline_table,
    compare_baseline,
    score_scenario,
)
from benchmark.run import run_one
from benchmark.scenarios import TEMPLATES
from whyflaky.stats import ACCEPT_H0, ACCEPT_H1, CONTINUE, SPRT


def test_sprt_unaffected_without_env(monkeypatch):
    monkeypatch.delenv("WHYFLAKY_FIXED_N", raising=False)
    sprt = SPRT(p0=0.01, p1=0.10, alpha=0.05, beta=0.10, max_trials=50)
    # six consecutive failures cross the H1 boundary well before 15 trials
    decisions = [sprt.record(True) for _ in range(6)]
    assert ACCEPT_H1 in decisions
    assert sprt.n < 15


def test_fixed_n_runs_exactly_n_trials(monkeypatch):
    monkeypatch.setenv("WHYFLAKY_FIXED_N", "15")
    sprt = SPRT(p0=0.01, p1=0.10, alpha=0.05, beta=0.10, max_trials=50)
    for i in range(14):
        assert sprt.record(True) == CONTINUE, f"stopped early at trial {i + 1}"
    assert sprt.record(True) == ACCEPT_H1
    assert sprt.n == 15


def test_fixed_n_decides_by_midpoint_rule(monkeypatch):
    monkeypatch.setenv("WHYFLAKY_FIXED_N", "10")
    # 0/10 failures: rate 0 < midpoint of (0.01, 0.10) -> stable
    sprt = SPRT()
    outcomes = [sprt.record(False) for _ in range(10)]
    assert outcomes[-1] == ACCEPT_H0
    # 2/10 failures: rate 0.2 >= midpoint 0.055 -> flaky
    monkeypatch.setenv("WHYFLAKY_FIXED_N", "10")
    sprt2 = SPRT()
    for _ in range(8):
        sprt2.record(False)
    sprt2.record(True)
    assert sprt2.record(True) == ACCEPT_H1


def test_fixed_n_ignores_garbage_env(monkeypatch):
    monkeypatch.setenv("WHYFLAKY_FIXED_N", "banana")
    sprt = SPRT()
    decisions = [sprt.record(True) for _ in range(6)]
    assert ACCEPT_H1 in decisions  # normal SPRT behavior


def _score(template="od_env", trials=40, report_flakes=None):
    m = TEMPLATES[template](10, 1).manifest()
    truth = m["flaky_tests"][0]
    if report_flakes is None:
        report_flakes = [{
            "test_id": truth["test_id"],
            "kind": "order-dependent (victim)",
            "diagnosis": {"cause": "test-order dependence (state pollution)",
                          "polluters": [truth["polluters"][0]]},
        }]
    report = {"cost": {"trials": trials, "wall_seconds": 1.0},
              "flakes": report_flakes}
    return score_scenario(m, report)


def test_compare_baseline_ratio_and_agreement():
    cmp = compare_baseline([_score(trials=40)], [_score(trials=200)])
    assert cmp["rows"][0]["ratio"] == 5.0
    assert cmp["rows"][0]["agree"] is True
    assert cmp["overall_ratio"] == 5.0
    assert cmp["agreement"] == 1.0


def test_compare_baseline_flags_disagreement():
    missed = _score(trials=200, report_flakes=[])
    cmp = compare_baseline([_score(trials=40)], [missed])
    assert cmp["rows"][0]["agree"] is False
    assert cmp["agreement"] == 0.0


def test_compare_baseline_skips_errored_scenarios():
    ok = _score()
    errored = score_scenario(TEMPLATES["od_env"](10, 1).manifest(), None,
                             error="boom")
    cmp = compare_baseline([ok], [errored])
    assert cmp["rows"] == []
    assert cmp["skipped"] == [ok.scenario_id]


def test_baseline_table_renders():
    cmp = compare_baseline([_score(trials=40)], [_score(trials=200)])
    table = baseline_table(cmp, fixed_n=15)
    assert "5.0x" in table and "fixed-N=15" in table


@pytest.mark.integration
def test_live_two_arm_comparison(tmp_path):
    """One scenario through both arms: fixed-N must cost more trials and reach
    the same conclusions."""
    scenario = TEMPLATES["od_module_global"](10, seed=1)
    sprt_report, err1 = run_one(scenario, tmp_path / "a", verify=False,
                                budget=3000, rounds=6)
    fixed_report, err2 = run_one(scenario, tmp_path / "b", verify=False,
                                 budget=3000, rounds=6, fixed_n=10)
    assert err1 is None and err2 is None, (err1, err2)
    a = score_scenario(scenario.manifest(), sprt_report)
    b = score_scenario(scenario.manifest(), fixed_report)
    cmp = compare_baseline([a], [b])
    row = cmp["rows"][0]
    assert row["agree"], "arms must reach the same diagnosis"
    assert row["fixed_trials"] > row["sprt_trials"], row
