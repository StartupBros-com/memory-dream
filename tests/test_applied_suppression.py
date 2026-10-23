"""Applied-outcome suppression at the triage/plan CLI boundaries.

All notes, pass evidence, consent transcripts, and apply writes are synthetic.
"""

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_apply import Harness
from test_audit import _clean_env, note


REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = dt.date(2026, 7, 17)


class AppliedOutcomeSuppressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = _clean_env(self.root / "config")
        self.env["MEMORY_DREAM_PASS_ROOT"] = str(self.root / "passes")
        self.notes = {}

    def _notes(self, *pairs):
        for project, path in pairs:
            memory = self.root / "live" / project / "memory"
            memory.mkdir(parents=True, exist_ok=True)
            content = note(path[:-3], body="RESOLVED\n" + "x" * 7000)
            (memory / path).write_text(content, encoding="utf-8", newline="\n")
            self.notes[project, path] = content.encode("utf-8")
        for project, _path in self.notes:
            index = self.root / "live" / project / "memory" / "MEMORY.md"
            index.write_text("".join(
                f"- [{path}]({path})\n" for p, path in self.notes if p == project
            ), encoding="utf-8", newline="\n")

    def _proposal(self, pid="p1", project="proj", path="big.md"):
        return {"id": pid, "project": project, "results": [{"path": path}]}

    def _outcomes(self, *statuses, project="proj"):
        return {"projects": [{
            "project": project,
            "proposals": [{"id": pid, "status": status} for pid, status in statuses],
        }]}

    def _pass(self, manifest, outcomes, name="pass", age=1):
        patch = self.root / "passes" / name
        patch.mkdir(parents=True, exist_ok=True)
        for filename, payload in [("manifest.json", manifest), ("apply-manifest.json", outcomes)]:
            path = patch / filename
            if payload is None:
                path.unlink(missing_ok=True)
                continue
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            path.write_bytes(data)
        applied = patch / "apply-manifest.json"
        if applied.exists():
            timestamp = dt.datetime.combine(NOW - dt.timedelta(days=age), dt.time(12)).timestamp()
            os.utime(applied, (timestamp, timestamp))
        return patch

    def _run(self, command, *args):
        return subprocess.run(
            [sys.executable, "-m", "memory_dream", command, *args],
            text=True, capture_output=True, check=False, cwd=REPO_ROOT, env=self.env,
        )

    def _assert_candidates(self, suppressed):
        expected = set(self.notes) - suppressed
        common = ["--live-root", str(self.root / "live"), "--now", NOW.isoformat()]
        with self.subTest(command="triage"):
            triage = self._run("triage", *common, "--format", "json", "--suppress-rejected-days", "0")
            self.assertEqual(triage.returncode, 0, triage.stderr)
            data = json.loads(triage.stdout)
            self.assertEqual({(n["project"], n["path"]) for n in data["flagged"]}, expected)
            self.assertEqual({(n["project"], n["path"]) for n in data["suppressed"]}, suppressed)
            self.assertEqual(data["summary"]["suppressed_recently_applied"], len(suppressed))
        with self.subTest(command="plan"):
            plan = self._run(
                "plan", *common, "--max-clusters", "100", "--max-notes", "100",
                "--shards-dir", str(self.root / "shards"),
            )
            self.assertEqual(plan.returncode, 0, plan.stderr)
            data = json.loads(plan.stdout)
            self.assertEqual({
                (cluster["project"], n["path"])
                for cluster in data["clusters"] for n in cluster["notes"]
            }, expected)
            self.assertEqual(data["deferred"], [])
            self.assertEqual(data["manual_review"], [])
        for (project, path), content in self.notes.items():
            self.assertEqual((self.root / "live" / project / "memory" / path).read_bytes(), content)

    def test_mixed_statuses_only_suppress_applied_proposals(self):
        statuses = ["applied", "skipped", "failed", "left", "unknown", "unapproved"]
        self._notes(*[("proj", status + ".md") for status in statuses])
        self._pass(
            {"proposals": [self._proposal(status, path=status + ".md") for status in statuses]},
            self._outcomes(*[(status, status) for status in statuses if status != "unapproved"]),
        )
        self._assert_candidates({("proj", "applied.md")})

    def test_all_skipped_and_summary_counts_are_not_application_evidence(self):
        self._notes(("proj", "big.md"))
        outcomes = self._outcomes(("p1", "skipped"))
        outcomes["applied"] = 42  # A summary cannot override a proposal outcome.
        outcomes["projects"][0]["committed"] = True
        self._pass({"proposals": [self._proposal()]}, outcomes)
        self._assert_candidates(set())

    def test_outcomes_are_joined_on_both_project_and_proposal_id(self):
        self._notes(("proj", "big.md"), ("other", "big.md"), ("proj", "unmatched.md"))
        self._pass({"proposals": [
            self._proposal(), self._proposal(project="other"),
            self._proposal("p2", path="unmatched.md"),
        ]}, self._outcomes(("p1", "applied"), ("unmatched-id", "applied")))
        self._assert_candidates({("proj", "big.md")})

    def test_applied_result_paths_and_legacy_survivors_remain_supported(self):
        paths = ["big.md", "extract.md", "bare.md", "object.md", "skipped.md"]
        self._notes(*[("proj", path) for path in paths])
        current = self._proposal()
        current["results"].append({"path": "extract.md"})
        self._pass({"proposals": [
            current,
            {"id": "bare", "project": "proj", "survivor": "bare.md"},
            {"id": "object", "project": "proj", "survivor": {"path": "object.md"}},
            {"id": "skipped", "project": "proj", "survivor": "skipped.md"},
        ]}, self._outcomes(("p1", "applied"), ("bare", "applied"), ("object", "applied"), ("skipped", "skipped")))
        self._assert_candidates({("proj", path) for path in paths if path != "skipped.md"})

    def test_missing_or_malformed_apply_evidence_cannot_suppress(self):
        self._notes(("proj", "big.md"))
        malformed = [
            None, b"{", b"\xff", {}, [], "applied", 1,
            {"projects": None}, {"projects": {}}, {"projects": "proj"},
            {"projects": [None, 1, "proj"]},
            {"projects": [{"project": "proj", "proposals": None}]},
            {"projects": [{"project": "proj", "proposals": {"p1": "applied"}}]},
            {"projects": [{"project": "proj", "proposals": [None, 1, "applied"]}]},
        ]
        for project in [None, "", [], {}]:
            malformed.append(self._outcomes(("p1", "applied"), project=project))
        for pid in [None, "", [], {}]:
            malformed.append(self._outcomes((pid, "applied")))
        for outcomes in malformed:
            with self.subTest(outcomes=outcomes):
                self._pass({"proposals": [self._proposal()]}, outcomes)
                self._assert_candidates(set())

    def test_missing_or_malformed_proposals_cannot_suppress(self):
        self._notes(("proj", "big.md"))
        malformed = [
            None, b"{", b"\xff", {}, [], "manifest", 1,
            {"proposals": None}, {"proposals": {}}, {"proposals": "proposal"},
            {"proposals": [None, 1, "proposal"]},
        ]
        for field in ["project", "id", "results"]:
            for value in [None, [], {}, ""]:
                proposal = self._proposal()
                proposal[field] = value
                malformed.append({"proposals": [proposal]})
        malformed.append({"proposals": [{
            "project": "proj", "id": "p1",
            "results": [None, 1, "big.md", {}, {"path": []}, {"path": {}}, {"path": ""}],
            "survivor": {"path": []},
        }]})
        for manifest in malformed:
            with self.subTest(manifest=manifest):
                self._pass(manifest, self._outcomes(("p1", "applied")))
                self._assert_candidates(set())

    def test_malformed_siblings_do_not_hide_valid_applied_evidence(self):
        self._notes(("proj", "big.md"))
        outcomes = self._outcomes(("p1", "applied"))
        outcomes["projects"].extend([None, {"project": "other", "proposals": False}])
        outcomes["projects"][0]["proposals"].append(None)
        proposal = self._proposal()
        proposal["results"].extend([None, {"path": []}])
        self._pass({"proposals": [None, proposal]}, outcomes)
        self._assert_candidates({("proj", "big.md")})

    def test_suppression_window_keeps_inclusive_cutoff(self):
        self._notes(("proj", "big.md"), ("proj", "old.md"))
        self._pass({"proposals": [self._proposal()]}, self._outcomes(("p1", "applied")), age=14)
        self._pass(
            {"proposals": [self._proposal(path="old.md")]},
            self._outcomes(("p1", "applied")), name="old", age=15,
        )
        self._assert_candidates({("proj", "big.md")})

    def test_actual_partial_apply_only_suppresses_the_written_note(self):
        self._actual_apply(all_skipped=False)

    def test_actual_all_skipped_apply_leaves_note_eligible(self):
        self._actual_apply(all_skipped=True)

    def _actual_apply(self, all_skipped):
        harness = Harness(self.root)
        states = ["skipped"] if all_skipped else ["applied", "skipped", "left", "unapproved"]
        self._notes(*[("proj", state + ".md") for state in states])
        for state in states:
            path = state + ".md"
            proposal = self._proposal(state, path=path)
            proposal.update({
                "action": "leave" if state == "left" else "compress",
                "sources": [{"path": path, "digest": "changed" if state == "skipped" else harness.digest("proj", path)}],
                "deletes": [], "survivor": path,
            })
            content = note(state, body="RESOLVED\n" + "y" * 7000)
            proposal["results"][0]["content"] = content
            harness.add_proposal(proposal)
            if state == "applied":
                self.notes["proj", path] = content.encode("utf-8")
        harness.write()
        selection = harness.selection([state for state in states if state != "unapproved"])
        result = harness.run(selection, mirror=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = json.loads((harness.patch_set / "apply-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(evidence["applied"], 0 if all_skipped else 1)
        self.assertEqual({p["id"]: p["status"] for p in evidence["projects"][0]["proposals"]}, {
            state: state for state in states if state != "unapproved"
        })
        self.env["MEMORY_DREAM_PASS_ROOT"] = str(harness.logs)
        self._assert_candidates(set() if all_skipped else {("proj", "applied.md")})


if __name__ == "__main__":
    unittest.main()
