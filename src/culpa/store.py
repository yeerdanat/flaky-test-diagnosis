"""Run store — local SQLite history (design doc §10). No ORM on purpose:
single-writer, zero-config, stdlib only."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
    id INTEGER PRIMARY KEY,
    started_at REAL,
    code_sha TEXT,
    config_json TEXT,
    budget INTEGER,
    outcome TEXT
);
CREATE TABLE IF NOT EXISTS trial (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES run(id),
    order_hash TEXT,
    env_fingerprint TEXT,
    seed TEXT,
    duration_ms REAL
);
CREATE TABLE IF NOT EXISTS result (
    trial_id INTEGER REFERENCES trial(id),
    test_id TEXT,
    status TEXT,
    duration_ms REAL,
    error_hash TEXT
);
CREATE TABLE IF NOT EXISTS flake (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES run(id),
    test_id TEXT,
    kind TEXT,
    failure_rate REAL,
    ci_low REAL,
    ci_high REAL,
    trials INTEGER,
    first_seen REAL
);
CREATE TABLE IF NOT EXISTS diagnosis (
    id INTEGER PRIMARY KEY,
    flake_id INTEGER REFERENCES flake(id),
    cause TEXT,
    evidence_json TEXT,
    confidence TEXT,
    polluter_test_id TEXT
);
CREATE TABLE IF NOT EXISTS patch (
    id INTEGER PRIMARY KEY,
    diagnosis_id INTEGER REFERENCES diagnosis(id),
    tier TEXT,
    diff TEXT,
    verified INTEGER,
    verify_evidence_json TEXT
);
"""


class Store:
    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    # ------------------------------------------------------------------ #

    def start_run(self, code_sha: str, config: dict, budget: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO run (started_at, code_sha, config_json, budget, outcome)"
            " VALUES (?, ?, ?, ?, ?)",
            (time.time(), code_sha, json.dumps(config), budget, "running"),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, outcome: str) -> None:
        self.conn.execute("UPDATE run SET outcome = ? WHERE id = ?", (outcome, run_id))
        self.conn.commit()

    def record_trial(self, run_id: int, trial) -> int:
        import hashlib

        order_hash = hashlib.sha1("\n".join(trial.order).encode()).hexdigest()[:16]
        cur = self.conn.execute(
            "INSERT INTO trial (run_id, order_hash, env_fingerprint, seed, duration_ms)"
            " VALUES (?, ?, ?, ?, ?)",
            (run_id, order_hash, trial.env_fingerprint, "", trial.duration_ms),
        )
        trial_id = cur.lastrowid
        self.conn.executemany(
            "INSERT INTO result (trial_id, test_id, status, duration_ms, error_hash)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (trial_id, r.test_id, r.status, r.duration_ms, r.error_hash)
                for r in trial.results.values()
            ],
        )
        self.conn.commit()
        return trial_id

    def record_flake(
        self, run_id: int, test_id: str, kind: str,
        failure_rate: float, ci: tuple[float, float], trials: int,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO flake (run_id, test_id, kind, failure_rate, ci_low,"
            " ci_high, trials, first_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, test_id, kind, failure_rate, ci[0], ci[1], trials, time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    def record_diagnosis(
        self, flake_id: int, cause: str, evidence: dict,
        confidence: str, polluter: str | None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO diagnosis (flake_id, cause, evidence_json, confidence,"
            " polluter_test_id) VALUES (?, ?, ?, ?, ?)",
            (flake_id, cause, json.dumps(evidence), confidence, polluter),
        )
        self.conn.commit()
        return cur.lastrowid

    def record_patch(
        self, diagnosis_id: int, tier: str, diff: str,
        verified: bool | None, verify_evidence: dict,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO patch (diagnosis_id, tier, diff, verified,"
            " verify_evidence_json) VALUES (?, ?, ?, ?, ?)",
            (diagnosis_id, tier, diff,
             None if verified is None else int(verified),
             json.dumps(verify_evidence)),
        )
        self.conn.commit()
        return cur.lastrowid
