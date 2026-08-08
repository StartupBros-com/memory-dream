#!/usr/bin/env python3
"""Tests for `eval export-paired`: the suite-integration seam.

The exporter owns every memory-dream semantic (suite identity, decay
exclusion, unpaired-id drop) and emits two flat {question_id: score} files
with identical key sets — the input shape generic paired-comparison tools
accept. Nothing here depends on any such tool being installed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _clean_env(claude_config_dir: Path) -> dict:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("MEMORY_DREAM_", "CLAUDE_MEMORY_")) and key != "CLAUDE_JOB_DIR"
    }
    env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir)
    return env


def _run(record_overrides: list[dict], suite_id: str = "s1", fingerprint: str = "f1") -> dict:
    records = []
    for override in record_overrides:
        record = {"id": "q?", "project": "proj", "score": 50.0, "verdict": "hit"}
        record.update(override)
        records.append(record)
    return {"suite_id": suite_id, "fingerprint": fingerprint, "records": records}


def _write_run(base: Path, name: str, run: dict) -> Path:
    path = base / name
    path.write_text(json.dumps(run), encoding="utf-8", newline="\n")
    return path


def _export(temp: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "memory_dream", "eval", "export-paired", *args],
        cwd=REPO_ROOT,
        env=_clean_env(temp / "claude-config"),
        text=True,
        capture_output=True,
        check=False,
    )


class ExportPairedTests(unittest.TestCase):
    def test_round_trip_excludes_decay_and_unpaired(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = _write_run(root, "pre.json", _run([
                {"id": "q1", "score": 60},
                {"id": "q2", "score": 70.5},
                {"id": "q3", "score": 80},
                {"id": "q4", "score": 40},
                {"id": "q5-base-only", "score": 10},
            ]))
            candidate = _write_run(root, "post.json", _run([
                {"id": "q1", "score": 65},
                {"id": "q2", "score": 70.5},
                {"id": "q3", "score": 90},
                {"id": "q4", "score": 0, "verdict": "broken"},
                {"id": "q6-cand-only", "score": 99},
            ]))
            out_dir = root / "paired"
            result = _export(
                root, "--baseline", str(baseline), "--candidate", str(candidate),
                "--out-dir", str(out_dir),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            base_scores = json.loads((out_dir / "paired-baseline.json").read_text(encoding="utf-8"))
            cand_scores = json.loads((out_dir / "paired-candidate.json").read_text(encoding="utf-8"))
            self.assertEqual(set(base_scores), set(cand_scores))
            self.assertEqual(base_scores, {"q1": 60.0, "q2": 70.5, "q3": 80.0})
            self.assertEqual(cand_scores, {"q1": 65.0, "q2": 70.5, "q3": 90.0})
            self.assertIn("1 decayed excluded", result.stderr)
            self.assertIn("1 baseline-only", result.stderr)
            self.assertIn("1 candidate-only", result.stderr)
            self.assertEqual(json.loads(result.stdout)["pairs"], 3)

    def test_suite_mismatch_refuses(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = _write_run(root, "pre.json", _run([{"id": "q1"}], suite_id="s1"))
            candidate = _write_run(root, "post.json", _run([{"id": "q1"}], suite_id="s2"))
            result = _export(
                root, "--baseline", str(baseline), "--candidate", str(candidate),
                "--out-dir", str(root / "paired"),
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("different suites", result.stderr)

    def test_no_scoreable_overlap_refuses(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = _write_run(root, "pre.json", _run([{"id": "q1"}]))
            candidate = _write_run(root, "post.json", _run([{"id": "q2"}]))
            result = _export(
                root, "--baseline", str(baseline), "--candidate", str(candidate),
                "--out-dir", str(root / "paired"),
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("no scoreable overlapping questions", result.stderr)

    def test_bare_run_ids_resolve_in_runs_dir(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runs = root / "runs"
            runs.mkdir()
            _write_run(runs, "run-pre.json", _run([{"id": "q1"}, {"id": "q2"}, {"id": "q3"}]))
            _write_run(runs, "run-post.json", _run([{"id": "q1"}, {"id": "q2"}, {"id": "q3"}]))
            result = _export(
                root, "--baseline", "pre", "--candidate", "post",
                "--runs-dir", str(runs), "--out-dir", str(root / "paired"),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["pairs"], 3)

    def test_fingerprint_mismatch_warns_but_exports(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = _write_run(root, "pre.json", _run([{"id": "q1"}], fingerprint="judge-a"))
            candidate = _write_run(root, "post.json", _run([{"id": "q1"}], fingerprint="judge-b"))
            result = _export(
                root, "--baseline", str(baseline), "--candidate", str(candidate),
                "--out-dir", str(root / "paired"),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("WARN fingerprints differ", result.stderr)
            self.assertIn("fewer than 3 pairs", result.stderr)


if __name__ == "__main__":
    unittest.main()
