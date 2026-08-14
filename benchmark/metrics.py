"""Score whyflaky reports against scenario ground truth.

Metric definitions (matching the design doc's validation section):

- Detection: a manifest flake counts as a true positive when it appears in the
  report's flakes list at all. A report entry for a test the manifest lists as
  stable is a false positive. The always_failing control is excluded from
  detection scoring (the tool is expected to report it, as broken); it is
  scored under classification instead.
- Classification, two levels:
    kind accuracy   - od_victim / nod / always_failing mapped onto the
                      report's kind strings
    cause accuracy  - order_pollution needs the OD cause; hashseed / rngseed
                      need that dimension in diagnosis.implicated; broken
                      needs the always-failing kind
- Localization: for detected od_victims, rank-1 means the report's first
  listed polluter is one of the manifest's true polluters.
- Fix: of patches proposed for true flakes, the fraction verified.
- Cost: trials and wall-clock, straight from the report.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .scenarios import (
    CAUSE_BROKEN,
    CAUSE_HASHSEED,
    CAUSE_ORDER,
    CAUSE_RNGSEED,
    KIND_ALWAYS_FAILING,
    KIND_NOD,
    KIND_OD_VICTIM,
)

# report vocabulary (src/whyflaky/orchestrator.py)
_REPORT_KIND = {
    KIND_OD_VICTIM: "order-dependent (victim)",
    KIND_NOD: "non-order-dependent",
    KIND_ALWAYS_FAILING: "always failing (brittle or broken)",
}


@dataclass
class FlakeScore:
    test_id: str
    true_kind: str
    true_cause: str
    detected: bool
    kind_correct: bool | None = None      # None when not detected
    cause_correct: bool | None = None
    localized: bool | None = None         # od_victim only
    fix_proposed: bool = False
    fix_verified: bool = False


@dataclass
class ScenarioScore:
    scenario_id: str
    template: str
    flakes: list[FlakeScore] = field(default_factory=list)
    false_positives: list[str] = field(default_factory=list)
    trials: int = 0
    wall_seconds: float = 0.0
    budget_exhausted: bool = False
    error: str | None = None              # scan crashed / report missing


def _cause_correct(true_cause: str, entry: dict) -> bool:
    diag = entry.get("diagnosis") or {}
    if true_cause == CAUSE_ORDER:
        return bool(diag.get("polluters")) or "order" in str(diag.get("cause", ""))
    if true_cause in (CAUSE_HASHSEED, CAUSE_RNGSEED):
        return true_cause in (diag.get("implicated") or [])
    if true_cause == CAUSE_BROKEN:
        return entry.get("kind") == _REPORT_KIND[KIND_ALWAYS_FAILING]
    return False


def score_scenario(manifest: dict, report: dict | None,
                   error: str | None = None) -> ScenarioScore:
    score = ScenarioScore(
        scenario_id=manifest["scenario_id"],
        template=manifest["template"],
        error=error,
    )
    if report is None:
        # scan failed: every ground-truth flake is a miss
        score.flakes = [
            FlakeScore(f["test_id"], f["kind"], f["cause"], detected=False)
            for f in manifest["flaky_tests"]
        ]
        return score

    cost = report.get("cost", {})
    score.trials = cost.get("trials", 0)
    score.wall_seconds = cost.get("wall_seconds", 0.0)
    score.budget_exhausted = bool(cost.get("exhausted"))

    reported = {e["test_id"]: e for e in report.get("flakes", [])}
    truth = {f["test_id"]: f for f in manifest["flaky_tests"]}

    for test_id, f in truth.items():
        entry = reported.get(test_id)
        fs = FlakeScore(test_id, f["kind"], f["cause"], detected=entry is not None)
        if entry is not None:
            fs.kind_correct = entry.get("kind") == _REPORT_KIND[f["kind"]]
            fs.cause_correct = _cause_correct(f["cause"], entry)
            if f["kind"] == KIND_OD_VICTIM:
                polluters = (entry.get("diagnosis") or {}).get("polluters") or []
                fs.localized = bool(polluters) and polluters[0] in f["polluters"]
            patch = entry.get("patch")
            if patch:
                fs.fix_proposed = True
                fs.fix_verified = bool(patch.get("verified"))
        score.flakes.append(fs)

    stable = set(manifest["stable_tests"])
    score.false_positives = sorted(t for t in reported if t in stable)
    return score


def aggregate(scores: list[ScenarioScore]) -> dict:
    def rate(num: int, den: int) -> float | None:
        return num / den if den else None

    # always_failing is a classification control, excluded from detection
    real = [f for s in scores for f in s.flakes if f.true_kind != KIND_ALWAYS_FAILING]
    controls = [f for s in scores for f in s.flakes if f.true_kind == KIND_ALWAYS_FAILING]

    tp = sum(f.detected for f in real)
    fn = len(real) - tp
    fp = sum(len(s.false_positives) for s in scores)

    detected = [f for f in real + controls if f.detected]
    od = [f for f in real if f.true_kind == KIND_OD_VICTIM and f.detected]
    proposed = [f for f in detected if f.fix_proposed]

    return {
        "scenarios": len(scores),
        "scan_errors": sum(1 for s in scores if s.error),
        "detection": {
            "tp": tp, "fn": fn, "fp": fp,
            "precision": rate(tp, tp + fp),
            "recall": rate(tp, tp + fn),
        },
        "classification": {
            "kind_accuracy": rate(sum(bool(f.kind_correct) for f in detected), len(detected)),
            "cause_accuracy": rate(sum(bool(f.cause_correct) for f in detected), len(detected)),
            "classified": len(detected),
        },
        "localization": {
            "rank1_accuracy": rate(sum(bool(f.localized) for f in od), len(od)),
            "od_detected": len(od),
        },
        "fix": {
            "verified_rate": rate(sum(f.fix_verified for f in proposed), len(proposed)),
            "proposed": len(proposed),
        },
        "cost": {
            "total_trials": sum(s.trials for s in scores),
            "total_wall_seconds": round(sum(s.wall_seconds for s in scores), 2),
            "budget_exhausted": sum(s.budget_exhausted for s in scores),
        },
    }


def _conclusions(s: ScenarioScore) -> tuple:
    """The scenario's diagnostic conclusions, stripped of cost: what was
    detected and how it was explained. Two arms 'agree' when these match."""
    return (
        tuple(sorted((f.test_id, f.detected, f.kind_correct, f.cause_correct,
                      f.localized) for f in s.flakes)),
        tuple(s.false_positives),
    )


def compare_baseline(sprt: list[ScenarioScore],
                     fixed: list[ScenarioScore]) -> dict:
    """Per-scenario trial cost of SPRT vs fixed-N, plus conclusion agreement.

    Only scenarios where both arms completed are compared; rows with a scan
    error in either arm are listed separately rather than silently dropped.
    """
    by_id = {s.scenario_id: s for s in fixed}
    rows, skipped = [], []
    for a in sprt:
        b = by_id.get(a.scenario_id)
        if b is None or a.error or b.error:
            skipped.append(a.scenario_id)
            continue
        rows.append({
            "scenario_id": a.scenario_id,
            "sprt_trials": a.trials,
            "fixed_trials": b.trials,
            "ratio": round(b.trials / a.trials, 2) if a.trials else None,
            "agree": _conclusions(a) == _conclusions(b),
        })
    total_sprt = sum(r["sprt_trials"] for r in rows)
    total_fixed = sum(r["fixed_trials"] for r in rows)
    return {
        "rows": rows,
        "skipped": skipped,
        "total_sprt_trials": total_sprt,
        "total_fixed_trials": total_fixed,
        "overall_ratio": round(total_fixed / total_sprt, 2) if total_sprt else None,
        "agreement": (sum(r["agree"] for r in rows) / len(rows)) if rows else None,
    }


def baseline_table(cmp: dict, fixed_n: int) -> str:
    lines = [
        f"| scenario | SPRT trials | fixed-N={fixed_n} trials | ratio | conclusions agree |",
        "|---|---|---|---|---|",
    ]
    for r in cmp["rows"]:
        lines.append(
            f"| {r['scenario_id']} | {r['sprt_trials']} | {r['fixed_trials']} |"
            f" {r['ratio']}x | {'yes' if r['agree'] else 'NO'} |"
        )
    for sid in cmp["skipped"]:
        lines.append(f"| {sid} | skipped (scan error) | | | |")
    lines += [
        "",
        f"**SPRT vs fixed-N={fixed_n}**: {cmp['total_sprt_trials']} vs"
        f" {cmp['total_fixed_trials']} trials"
        f" ({cmp['overall_ratio']}x saving), conclusions agree on"
        f" {_fmt(cmp['agreement'])} of scenarios",
    ]
    return "\n".join(lines)


def _fmt(x: float | None) -> str:
    return "-" if x is None else f"{100 * x:.0f}%"


def markdown_table(scores: list[ScenarioScore], agg: dict) -> str:
    lines = [
        "| scenario | detected | kind | cause | polluter rank-1 | fix verified | trials | wall (s) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in scores:
        if s.error:
            lines.append(f"| {s.scenario_id} | SCAN ERROR | | | | | | |")
            continue
        if not s.flakes:  # stable control
            d = "clean" if not s.false_positives else f"{len(s.false_positives)} FP"
            lines.append(f"| {s.scenario_id} | {d} | - | - | - | - |"
                         f" {s.trials} | {s.wall_seconds} |")
            continue
        for f in s.flakes:
            mark = lambda v: "-" if v is None else ("yes" if v else "NO")
            fp_note = f" (+{len(s.false_positives)} FP)" if s.false_positives else ""
            lines.append(
                f"| {s.scenario_id}{fp_note} | {mark(f.detected)} |"
                f" {mark(f.kind_correct)} | {mark(f.cause_correct)} |"
                f" {mark(f.localized)} |"
                f" {mark(f.fix_verified if f.fix_proposed else None)} |"
                f" {s.trials} | {s.wall_seconds} |"
            )

    det, cls = agg["detection"], agg["classification"]
    loc, fix, cost = agg["localization"], agg["fix"], agg["cost"]
    lines += [
        "",
        f"**Detection** precision {_fmt(det['precision'])}"
        f" (tp={det['tp']} fp={det['fp']}), recall {_fmt(det['recall'])}"
        f" (fn={det['fn']}) · "
        f"**Classification** kind {_fmt(cls['kind_accuracy'])},"
        f" cause {_fmt(cls['cause_accuracy'])} ({cls['classified']} classified) · "
        f"**Localization** rank-1 {_fmt(loc['rank1_accuracy'])}"
        f" ({loc['od_detected']} OD) · "
        f"**Fix** verified {_fmt(fix['verified_rate'])}"
        f" ({fix['proposed']} proposed) · "
        f"**Cost** {cost['total_trials']} trials,"
        f" {cost['total_wall_seconds']}s wall",
    ]
    return "\n".join(lines)
