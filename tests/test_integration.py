"""End-to-end: scan a tiny polluter/victim suite in real subprocesses.

Slow by unit-test standards (tens of pytest subprocesses); marked integration.
"""
import json
import shutil
from pathlib import Path

import pytest

from whyflaky.cli import main

EXAMPLE = Path(__file__).parent.parent / "examples" / "flaky_suite"


@pytest.mark.integration
def test_scan_finds_polluter_and_victim(tmp_path: Path):
    repo = tmp_path / "suite"
    repo.mkdir()
    for name in ("conftest.py", "test_billing.py", "test_invoice.py", "test_stable.py"):
        shutil.copy(EXAMPLE / name, repo / name)
    shutil.copytree(EXAMPLE / "app", repo / "app")

    json_path = tmp_path / "report.json"
    exit_code = main([
        "scan", str(repo),
        "--rounds", "2",          # round 0 (definition order) already fails
        "--budget", "120",
        "--json", str(json_path),
        "--fix",
        "-q",
    ])
    assert exit_code == 0

    report = json.loads(json_path.read_text())
    flakes = {f["test_id"]: f for f in report["flakes"]}
    victim = flakes["test_invoice.py::test_default_currency_is_usd"]
    assert victim["kind"] == "order-dependent (victim)"

    diag = victim["diagnosis"]
    assert diag["polluters"] == ["test_billing.py::test_eur_invoice_formatting"]
    polluted = " ".join(diag["polluted_state"])
    assert "APP_CURRENCY" in polluted
    assert "app.state.CURRENCY" in polluted

    patch = victim["patch"]
    assert patch is not None
    assert "conftest.py" in patch["files"]
