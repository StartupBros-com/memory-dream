#!/usr/bin/env python3
"""Tests for memory_dream.verify_findings: the advisory quote-existence gate
(``python3 -m memory_dream verify-findings``).

Reproduces the 2026-07-20 incident shape directly: a panel/verifier finding
that quotes text absent from every file it could cite. Before this gate
existed, that finding reached adjudication un-flagged. These tests assert
the gate now stamps it ``unverified_quote: true`` (and keeps it -- this
gate never drops a finding) rather than trusting the quote silently.

Every Fixture.run() call shells out to the real CLI in a child process, so
each test gets its own clean CLAUDE_CONFIG_DIR (never a developer's real
~/.claude) and never inherits any leaked MEMORY_DREAM_*/CLAUDE_MEMORY_* env
override from the outer environment -- same discipline as test_recall_eval.py.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from memory_dream import verify_findings as VF

REPO_ROOT = Path(__file__).resolve().parents[1]


def _clean_env(claude_config_dir: Path) -> dict:
    """A subprocess env with no leaked MEMORY_DREAM_*/CLAUDE_MEMORY_* override
    and a per-test CLAUDE_CONFIG_DIR, so a developer's real config can never leak in."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("MEMORY_DREAM_", "CLAUDE_MEMORY_"))
    }
    env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir)
    return env


def finding(**overrides):
    entry = {
        "severity": "high",
        "claim": "the champion designation is re-resolved every eval tick",
        "problem": "this contradicts the note's earlier paragraph",
        "fix": "restate as a stable snapshot",
        "quote": "the quoted span",
    }
    entry.update(overrides)
    return entry


class Fixture:
    def __init__(self, root: Path):
        self.root = root
        self.repo = root / "repo"
        self.repo.mkdir()
        self.claude_config_dir = root / "claude-config"
        self.claude_config_dir.mkdir()

    def write_findings(self, files, name="findings.json"):
        path = self.root / name
        path.write_text(json.dumps({"files": files}), encoding="utf-8")
        return path

    def run(self, findings_path, root=None):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "memory_dream",
                "verify-findings",
                "--findings",
                str(findings_path),
                "--root",
                str(root if root is not None else self.repo),
            ],
            text=True,
            capture_output=True,
            check=False,
            cwd=REPO_ROOT,
            env=_clean_env(self.claude_config_dir),
        )

    def load(self, findings_path):
        return json.loads(findings_path.read_text(encoding="utf-8"))


class CLITests(unittest.TestCase):
    def test_happy_path_quote_present(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            (fixture.repo / "a.md").write_text(
                "Body text: the quoted span, plus more prose.", encoding="utf-8"
            )
            findings_path = fixture.write_findings([{"path": "a.md", "findings": [finding()]}])
            result = fixture.run(findings_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("checked 1, unverified 0", result.stdout)
            data = fixture.load(findings_path)
            stamped = data["files"][0]["findings"][0]
            self.assertIs(stamped["quote_checked"], True)
            self.assertNotIn("unverified_quote", stamped)

    def test_fabricated_quote_kept_and_flagged(self):
        """The 2026-07-20 incident shape: a finding quoting text absent from
        every file it could cite. It must still reach the output -- flagged,
        never silently dropped."""
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            (fixture.repo / "a.md").write_text(
                "Body text with no relation to the claim at all.", encoding="utf-8"
            )
            findings_path = fixture.write_findings(
                [{"path": "a.md", "findings": [finding(quote="this text appears nowhere in a.md")]}]
            )
            result = fixture.run(findings_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("checked 1, unverified 1", result.stdout)
            data = fixture.load(findings_path)
            stamped = data["files"][0]["findings"][0]
            self.assertIs(stamped["quote_checked"], False)
            self.assertIs(stamped["unverified_quote"], True)
            # never dropped: the original fields all survive
            self.assertEqual(stamped["claim"], finding()["claim"])

    def test_nonexistent_path_unverifiable_no_exception(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            findings_path = fixture.write_findings(
                [{"path": "ghost.md", "findings": [finding()]}]
            )
            result = fixture.run(findings_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = fixture.load(findings_path)
            stamped = data["files"][0]["findings"][0]
            self.assertIs(stamped["quote_checked"], False)
            self.assertIs(stamped["unverified_quote"], True)

    def test_escaping_path_never_opened(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            # A sentinel file outside the confined root: if the gate ever
            # opened it, this call would fail loudly (permission-style
            # sentinel via a value that would make normalize() explode if
            # read.
            outside = fixture.root / "secret.md"
            outside.write_text("the quoted span", encoding="utf-8")
            findings_path = fixture.write_findings(
                [{"path": "../secret.md", "findings": [finding(quote="the quoted span")]}]
            )
            result = fixture.run(findings_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = fixture.load(findings_path)
            stamped = data["files"][0]["findings"][0]
            # The quote DOES exist in the escaping file -- if the gate had
            # opened it despite the escape, this would wrongly verify true.
            # It must not: confinement rejects the path before any read.
            self.assertIs(stamped["quote_checked"], False)
            self.assertIs(stamped["unverified_quote"], True)

    def test_legacy_finding_with_no_quote_field(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            (fixture.repo / "a.md").write_text("Body text.", encoding="utf-8")
            legacy = finding()
            del legacy["quote"]
            findings_path = fixture.write_findings([{"path": "a.md", "findings": [legacy]}])
            result = fixture.run(findings_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = fixture.load(findings_path)
            stamped = data["files"][0]["findings"][0]
            self.assertIs(stamped["quote_checked"], False)
            self.assertIs(stamped["unverified_quote"], True)

    def test_whitespace_differing_quote_normalizes(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            (fixture.repo / "a.md").write_text(
                "Body text:\n  the   quoted\n  span\nmore prose.", encoding="utf-8"
            )
            findings_path = fixture.write_findings(
                [{"path": "a.md", "findings": [finding(quote="THE QUOTED SPAN")]}]
            )
            result = fixture.run(findings_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = fixture.load(findings_path)
            stamped = data["files"][0]["findings"][0]
            self.assertIs(stamped["quote_checked"], True)
            self.assertNotIn("unverified_quote", stamped)

    def test_malformed_json_errors_but_exits_zero_and_leaves_file_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            findings_path = fixture.root / "findings.json"
            findings_path.write_text("{not valid json", encoding="utf-8")
            before = findings_path.read_text(encoding="utf-8")
            result = fixture.run(findings_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotEqual(result.stderr.strip(), "")
            after = findings_path.read_text(encoding="utf-8")
            self.assertEqual(before, after)

    def test_round_trip_preserves_original_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            (fixture.repo / "a.md").write_text("the quoted span here", encoding="utf-8")
            original = finding(severity="med", claim="c", problem="p", fix="f")
            findings_path = fixture.write_findings([{"path": "a.md", "findings": [original]}])
            result = fixture.run(findings_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = fixture.load(findings_path)
            stamped = data["files"][0]["findings"][0]
            for key in ("severity", "claim", "problem", "fix", "quote"):
                self.assertEqual(stamped[key], original[key])
            self.assertIn("quote_checked", stamped)

    def test_reverification_replaces_stamps_and_preserves_payload(self):
        for wrapped in (True, False):
            with tempfile.TemporaryDirectory() as temp:
                fixture = Fixture(Path(temp))
                source = fixture.repo / "a.md"
                original = finding(extra={"unverified_quote": "unrelated metadata"})
                entry = {"path": "a.md", "findings": [original], "stage": "checker"}
                payload = {"files": [entry], "pass": "synthetic"} if wrapped else [entry]
                findings_path = fixture.root / "findings.json"
                findings_path.write_text(json.dumps(payload), encoding="utf-8")
                steps = [
                    (None, False),
                    (None, False),
                    ("the quoted span", True),
                    ("the quoted span", True),
                    ("the source no longer contains the quote", False),
                    ("the source no longer contains the quote", False),
                ]
                for step, (content, verified) in enumerate(steps):
                    with self.subTest(wrapped=wrapped, step=step):
                        if content is not None:
                            source.write_text(content, encoding="utf-8")
                        result = fixture.run(findings_path)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stderr, "")
                        self.assertIn(f"checked 1, unverified {int(not verified)}", result.stdout)
                        expected_finding = {**original, "quote_checked": verified}
                        if not verified:
                            expected_finding["unverified_quote"] = True
                        expected_entry = {**entry, "findings": [expected_finding]}
                        expected = (
                            {"files": [expected_entry], "pass": "synthetic"}
                            if wrapped else [expected_entry]
                        )
                        self.assertEqual(fixture.load(findings_path), expected)


class HelperTests(unittest.TestCase):
    """Pure-helper tests call memory_dream.verify_findings directly."""

    def test_verify_quote_confinement_blocks_before_open(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "root"
            root.mkdir()
            outside = Path(temp) / "outside.md"
            outside.write_text("the quoted span", encoding="utf-8")

            reads = []
            real_read_text = Path.read_text

            def spy_read_text(self, *args, **kwargs):
                reads.append(self)
                return real_read_text(self, *args, **kwargs)

            Path.read_text = spy_read_text
            try:
                stamps = VF.verify_quote(root, "../outside.md", "the quoted span")
            finally:
                Path.read_text = real_read_text

            self.assertEqual(stamps, {"quote_checked": False, "unverified_quote": True})
            self.assertNotIn(outside, reads)

    def test_verify_quote_nonexistent_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stamps = VF.verify_quote(root, "ghost.md", "anything")
            self.assertEqual(stamps, {"quote_checked": False, "unverified_quote": True})

    def test_verify_quote_missing_and_empty_quote(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a.md").write_text("some body", encoding="utf-8")
            self.assertEqual(
                VF.verify_quote(root, "a.md", None),
                {"quote_checked": False, "unverified_quote": True},
            )
            self.assertEqual(
                VF.verify_quote(root, "a.md", "   "),
                {"quote_checked": False, "unverified_quote": True},
            )

    def test_verify_findings_payload_tolerates_malformed_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = {
                "files": [
                    "not a dict",
                    {"path": "a.md", "findings": "not a list"},
                    {"path": "a.md"},
                ]
            }
            result, checked, unverified = VF.verify_findings_payload(data, root)
            self.assertIs(result, data)
            self.assertEqual(checked, 0)
            self.assertEqual(unverified, 0)

    def test_verify_findings_payload_accepts_bare_list(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a.md").write_text("the quoted span", encoding="utf-8")
            data = [{"path": "a.md", "findings": [finding()]}]
            _result, checked, unverified = VF.verify_findings_payload(data, root)
            self.assertEqual(checked, 1)
            self.assertEqual(unverified, 0)

    def test_success_removes_preexisting_failure_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a.md").write_text("the quoted span", encoding="utf-8")
            for wrapped in (True, False):
                for old_marker in (True, False, None, "stale", {"custom": "value"}):
                    with self.subTest(wrapped=wrapped, old_marker=old_marker):
                        original = finding(extra={"unverified_quote": "keep nested data"})
                        stale = {**original, "quote_checked": "stale", "unverified_quote": old_marker}
                        entry = {"path": "a.md", "findings": [stale]}
                        data = {"files": [entry]} if wrapped else [entry]
                        result, checked, unverified = VF.verify_findings_payload(data, root)
                        self.assertIs(result, data)
                        self.assertEqual((checked, unverified), (1, 0))
                        self.assertEqual(stale, {**original, "quote_checked": True})

    def test_reverification_leaves_unreached_records_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stale = {"quote_checked": False, "unverified_quote": True, "quote": "keep me"}
            data = {
                "files": [
                    "not a file record",
                    {"path": "a.md", "findings": stale},
                    {"path": "a.md", "unverified_quote": "file metadata"},
                    {"path": "a.md", "findings": [None, "not a finding"]},
                ],
                "unverified_quote": "top-level metadata",
            }
            before = json.dumps(data, sort_keys=True)
            for _repeat in range(2):
                result, checked, unverified = VF.verify_findings_payload(data, root)
                self.assertIs(result, data)
                self.assertEqual((checked, unverified), (0, 0))
                self.assertEqual(json.dumps(data, sort_keys=True), before)


if __name__ == "__main__":
    unittest.main()
