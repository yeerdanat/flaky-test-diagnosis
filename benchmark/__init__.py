"""Synthetic-flake benchmark harness.

Generates pytest suites with injected flakes whose causes are known, so the
tool's diagnoses can be scored against ground truth (detection precision and
recall, cause-classification accuracy, polluter localization, cost).

This package is a development harness. It ships in the repo, never in the wheel.
"""
