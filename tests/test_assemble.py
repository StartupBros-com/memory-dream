#!/usr/bin/env python3
"""Cluster/validate/retarget/assemble tests for memory_dream.assemble.

stdlib unittest only, no model calls, no network. Every subprocess invocation
isolates CLAUDE_CONFIG_DIR to a per-test temp directory and strips every
other MEMORY_DREAM_*/CLAUDE_*-prefixed variable from the inherited
environment, so a developer's real ~/.claude/memory-dream.json (and any
scratch-dir env fallback) can never leak into a test (see
docs/EXTRACTION-DESIGN.md, "Tests").
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from memory_dream import assemble as ASM  # noqa: E402
from memory_dream import audit  # noqa: E402
from memory_dream import config  # noqa: E402

# --- Environment isolation for subprocess CLI calls -------------------------

# Every CLAUDE_-prefixed var is stripped (not just CLAUDE_CONFIG_DIR): this is
# a superset of the config-env-var overrides and covers the scratch-dir env
# fallback too, so nothing ambient can leak into an isolated test subprocess.
# CLAUDE_CONFIG_DIR is re-added right after, pointed at the per-test tmp dir.
_ENV_PREFIXES_TO_STRIP = ("MEMORY_DREAM_", "CLAUDE_")


def _strip_leaky_keys(env: dict) -> dict:
    return {key: value for key, value in env.items() if not key.startswith(_ENV_PREFIXES_TO_STRIP)}


def subprocess_env(claude_config_dir: Path) -> dict:
    env = _strip_leaky_keys(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir)
    return env


def run_cli(*args: str, cwd: Path = REPO_ROOT, env: dict, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "memory_dream", *args],
        cwd=str(cwd),
        env=env,
        text=True,
        capture_output=True,
        check=check,
    )


def note(name="note", body="Body.", note_type="project"):
    return (
        "---\n"
        f"name: {name}\n"
        f"description: Fixture note describing {name} in detail\n"
        f"metadata:\n  type: {note_type}\n"
        "---\n"
        f"{body}\n"
    )


def _proposals_by_cluster_id(root):
    """Every drafted cluster's exact proposals payload, keyed by cluster_id — the
    same value run_build's `drafts` dict holds per cluster, so a digest computed
    from it here matches what build recomputes."""
    drafts = json.loads((root / "drafts.json").read_text())
    clusters = drafts.get("clusters", drafts) if isinstance(drafts, dict) else drafts
    if isinstance(clusters, list):
        return {c["cluster_id"]: c.get("proposals", []) for c in clusters}
    return dict(clusters)


def write_findings(root):
    """Stub the mandatory-stage findings file: every drafted cluster marked clean,
    with a real drafts_digest bound to that cluster's exact proposals payload (the
    content-binding gate in run_build recomputes and compares this). Tests
    exercising the findings gate itself write their own partial file."""
    proposals_by_id = _proposals_by_cluster_id(root)
    path = root / "findings.json"
    path.write_text(json.dumps({
        "clusters": {
            cid: {"status": "clean", "drafts_digest": audit.content_id(proposals)}
            for cid, proposals in proposals_by_id.items()
        }
    }))
    return path


def flagged(project, path, body, name=None, score=5, supersessions=0):
    return {
        "project": project,
        "path": path,
        "name": name,
        "body": body,
        "score": score,
        "supersessions": supersessions,
    }


class ClusteringTests(unittest.TestCase):
    def test_sensitive_member_skips_whole_cluster(self):
        shared = "shared token overlap consolidation memory dream pass duplicate content"
        notes = [
            flagged("proj", "a.md", shared + " alpha"),
            flagged("proj", "b.md", shared + " beta"),
        ]
        clusters, _deferred, manual = ASM.build_clusters(notes, {("proj", "b.md")}, 12, 8)
        # a.md and b.md are near-duplicates -> one cluster; b.md is sensitive -> whole
        # cluster is dropped to manual review, nothing left to draft.
        self.assertEqual(clusters, [])
        review_paths = sorted(entry["path"] for entry in manual)
        self.assertEqual(review_paths, ["a.md", "b.md"])
        self.assertTrue(all(entry["reason"] == "sensitive" for entry in manual))

    def test_per_pass_cluster_cap_defers_overflow(self):
        # Six dissimilar notes (disjoint token sets) -> six singleton clusters; a cap
        # of 2 keeps the 2 highest-scoring, defers 4.
        notes = [flagged("proj", f"n{i}.md", f"alpha{i} bravo{i} charlie{i} delta{i}", score=10 - i) for i in range(6)]
        clusters, deferred, _manual = ASM.build_clusters(notes, set(), 2, 8)
        self.assertEqual(len(clusters), 2)
        capped = [entry for entry in deferred if entry["reason"] == "per-pass-cap"]
        self.assertEqual(len(capped), 4)
        # The two highest-scoring notes are kept.
        kept = {note["path"] for cluster in clusters for note in cluster["notes"]}
        self.assertEqual(kept, {"n0.md", "n1.md"})

    def test_cluster_size_cap_defers_extra_notes(self):
        shared = "identical overlapping tokens for a very tight merge cluster here now"
        notes = [flagged("proj", f"d{i}.md", shared, score=10 - i) for i in range(5)]
        clusters, deferred, _manual = ASM.build_clusters(notes, set(), 12, 3)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0]["notes"]), 3)
        size_capped = [entry for entry in deferred if entry["reason"] == "cluster-size-cap"]
        self.assertEqual(len(size_capped), 2)

    def test_stable_ids_deterministic(self):
        self.assertEqual(
            ASM.stable_id("proj", ["b.md", "a.md"], "merge"),
            ASM.stable_id("proj", ["a.md", "b.md"], "merge"),
        )
        self.assertNotEqual(
            ASM.stable_id("proj", ["a.md"], "merge"),
            ASM.stable_id("proj", ["a.md"], "compress"),
        )


class ProposalValidationTests(unittest.TestCase):
    def test_schema_rejects_malformed(self):
        self.assertIsNotNone(ASM.validate_proposal({"action": "nonsense", "justification": "x"}))
        self.assertIsNotNone(ASM.validate_proposal({"action": "compress", "justification": ""}))
        self.assertIsNotNone(
            ASM.validate_proposal({"action": "period-close", "justification": "x", "deletes": [], "survivor": {"path": "a.md", "content": "c"}})
        )
        self.assertIsNotNone(
            ASM.validate_proposal({"action": "compress", "justification": "x", "survivor": {"path": "a.md"}})
        )
        self.assertIsNone(
            ASM.validate_proposal({"action": "leave", "justification": "fine as is", "deletes": []})
        )
        self.assertIsNone(
            ASM.validate_proposal(
                {"action": "compress", "justification": "shrink", "deletes": [], "survivor": {"path": "a.md", "content": "x"}}
            )
        )


class AssembleTests(unittest.TestCase):
    def _project(self, root, files):
        live = root / "live" / "proj" / "memory"
        live.mkdir(parents=True)
        for name, content in files.items():
            (live / name).write_text(content)
        return root / "live", live

    def _cluster(self, paths, live, project="proj"):
        records = audit.scan_project_notes(live)
        notes = [{"path": path, "name": records.get(path, {}).get("name"), "body": records.get(path, {}).get("body", ""), "score": 5, "supersessions": 0} for path in paths]
        return {"cluster_id": ASM.stable_id(project, sorted(paths), "cluster"), "project": project, "top_score": 5, "notes": notes}

    def test_retargets_all_inbound_links_of_closed_note(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(
                root,
                {
                    "old.md": note("old", body="Old fact."),
                    "one.md": note("one", body="Refers to [[old]] here."),
                    "two.md": note("two", body="Also see [[old]] again."),
                    "three.md": note("three", body="Unrelated, no link."),
                },
            )
            cluster = self._cluster(["old.md"], live)
            survivor = note("new", body="Current truth.").replace(
                "Fixture note describing new in detail",
                "canonical current truth after the period close",
            )
            drafts = {cluster["cluster_id"]: [{"action": "period-close", "justification": "closed", "survivor": {"path": "new.md", "content": survivor}, "deletes": ["old.md"]}]}
            proposals, dropped = ASM.assemble_proposals([cluster], drafts, live_root, "2026-07-31")
            self.assertEqual(dropped, [])
            # Proposal carries only the survivor result; retargeting is applied over
            # the approved set (previewed via anticipated_retargets, same logic apply uses).
            self.assertEqual([r["path"] for r in proposals[0]["results"]], ["new.md"])
            self.assertEqual(proposals[0]["survivor"], "new.md")
            edits = ASM.anticipated_retargets(live, proposals)
            self.assertIn("[[new]]", edits["one.md"])
            self.assertIn("[[new]]", edits["two.md"])
            self.assertNotIn("three.md", edits)  # not a linker, untouched
            self.assertNotIn("[[old]]", edits["one.md"])

    def test_audit_dry_run_pulls_failing_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {"a.md": note("a", body="Fact."), "b.md": note("b", body="Fact two.")})
            cluster = self._cluster(["a.md"], live)
            # A bad type value survives frontmatter preservation (the drafter's type
            # overrides the donor's), so the audit dry-run still pulls it.
            bad = "---\nname: a\ndescription: d\nmetadata:\n  type: notarealtype\n---\nBad type.\n"
            drafts = {cluster["cluster_id"]: [{"action": "compress", "justification": "shrink", "survivor": {"path": "a.md", "content": bad}, "deletes": []}]}
            proposals, dropped = ASM.assemble_proposals([cluster], drafts, live_root, "2026-07-31")
            self.assertEqual(proposals, [])
            self.assertEqual(len(dropped), 1)
            self.assertIn("audit dry-run", dropped[0]["reason"])

    def test_preserves_donor_frontmatter_schema_fields(self):
        # A consolidation must keep node_type/originSessionId (and heal a drafter's
        # missing type) rather than strip the harness memory schema.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            donor = (
                "---\nname: a\ndescription: \"old desc\"\nmetadata:\n"
                "  node_type: memory\n  type: project\n  originSessionId: sess-123\n---\nOld body.\n"
            )
            live_root, live = self._project(root, {"a.md": donor})
            cluster = self._cluster(["a.md"], live)
            drafter = "---\nname: a\ndescription: new tighter desc for the compressed note\nmetadata:\n  type: project\n---\nCompressed body.\n"
            drafts = {cluster["cluster_id"]: [{"action": "compress", "justification": "shrink", "survivor": {"path": "a.md", "content": drafter}, "deletes": []}]}
            proposals, dropped = ASM.assemble_proposals([cluster], drafts, live_root, "2026-07-31")
            self.assertEqual(dropped, [])
            content = proposals[0]["results"][0]["content"]
            self.assertIn("node_type: memory", content)  # schema field preserved
            self.assertIn("originSessionId: sess-123", content)  # provenance preserved
            self.assertIn("new tighter desc", content)  # description updated
            self.assertNotIn("old desc", content)
            self.assertIn("Compressed body.", content)
            self.assertNotIn("Old body.", content)

    def test_audit_dry_run_pulls_sensitive_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {"a.md": note("a", body="Fact.")})
            cluster = self._cluster(["a.md"], live)
            leaked = note("a", body="token api_key=highsignalsignaturevalue1234567890 leaked")
            drafts = {cluster["cluster_id"]: [{"action": "compress", "justification": "shrink", "survivor": {"path": "a.md", "content": leaked}, "deletes": []}]}
            proposals, dropped = ASM.assemble_proposals([cluster], drafts, live_root, "2026-07-31")
            self.assertEqual(proposals, [])
            self.assertIn("sensitive_content", dropped[0]["reason"])

    def test_traversal_destination_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {"a.md": note("a", body="Fact.")})
            cluster = self._cluster(["a.md"], live)
            drafts = {cluster["cluster_id"]: [{"action": "compress", "justification": "x", "survivor": {"path": "../../escape.md", "content": note("e")}, "deletes": []}]}
            proposals, dropped = ASM.assemble_proposals([cluster], drafts, live_root, "2026-07-31")
            self.assertEqual(proposals, [])
            self.assertEqual(dropped[0]["reason"], "path-escape")

    def test_preview_html_renders_side_by_side(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            manifest = {"id": "tok123", "proposals": [
                {"id": "p1", "project": "proj", "action": "compress", "justification": "shrink it",
                 "results": [{"path": "a.md", "content": "new"}], "deletes": []}]}
            report = {"dropped": [], "deferred": [], "manual_review": []}
            file_diffs = {"p1": [("a.md", "a.md", ["old line one", "old line two"], ["new line one"])]}
            path = ASM.write_preview_html(out, manifest, report, file_diffs)
            self.assertTrue(path.exists())
            content = path.read_text()
            self.assertIn("tok123", content)  # approval token shown
            self.assertIn("shrink it", content)  # justification
            self.assertIn('table class="diff"', content)  # side-by-side table
            self.assertIn('class="del"', content)  # a removed cell
            self.assertIn('class="add"', content)  # an added cell
            self.assertIn("old line two", content)  # removed content shown on the left
            self.assertIn("<!doctype html>", content.lower())

    def test_build_creates_owner_only_patch_set(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {"a.md": note("a", body="Fact.")})
            cluster = self._cluster(["a.md"], live)
            plan = {"schema_version": 1, "clusters": [cluster], "deferred": [], "manual_review": []}
            (root / "plan.json").write_text(json.dumps(plan))
            drafts = {"clusters": [{"cluster_id": cluster["cluster_id"], "proposals": [{"action": "leave", "justification": "fine", "deletes": []}]}]}
            (root / "drafts.json").write_text(json.dumps(drafts))
            out = root / "logs" / "ts"
            env = subprocess_env(root / "claude-config")
            result = run_cli(
                "build",
                "--live-root", str(live_root),
                "--plan", str(root / "plan.json"), "--drafts", str(root / "drafts.json"),
                "--out", str(out), "--created-at-line", "0",
                "--findings", str(write_findings(root)), "--stamp", "2026-07-31",
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(out.stat().st_mode & 0o777, 0o700)
            self.assertEqual(out.parent.stat().st_mode & 0o777, 0o700)

    def test_plan_shards_dir_writes_one_shard_per_cluster(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(
                root,
                {
                    "a.md": note("a", body="Stale fact from long ago.\nUpdated 2020-01-01.\n" * 40),
                    "b.md": note("b", body="Another stale fact.\nUpdated 2020-01-01.\n" * 40),
                },
            )
            shards = root / "shards"
            env = subprocess_env(root / "claude-config")
            # Snapshot mode (no --mirror-root): this test's intent is shard-file
            # writing, not mirror-freshness behavior, and no mirror is populated.
            result = run_cli(
                "plan",
                "--live-root", str(live_root),
                "--shards-dir", str(shards),
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            shard_files = sorted(shards.glob("*.json"))
            # One shard per cluster, named by cluster_id, carrying that cluster verbatim.
            self.assertEqual(
                sorted(c["cluster_id"] for c in plan["clusters"]),
                [p.stem for p in shard_files],
            )
            for shard_file in shard_files:
                shard = json.loads(shard_file.read_text())
                match = [c for c in plan["clusters"] if c["cluster_id"] == shard["cluster"]["cluster_id"]]
                self.assertEqual(shard["cluster"], match[0])

    def test_build_writes_plain_result_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {"a.md": note("a", body="Fact.")})
            cluster = self._cluster(["a.md"], live)
            plan = {"schema_version": 1, "clusters": [cluster], "deferred": [], "manual_review": []}
            (root / "plan.json").write_text(json.dumps(plan))
            rewritten = note("a", body="Compressed durable fact.")
            drafts = {"clusters": [{"cluster_id": cluster["cluster_id"], "proposals": [
                {"action": "compress", "justification": "shrink", "survivor": {"path": "a.md", "content": rewritten}, "deletes": []},
            ]}]}
            (root / "drafts.json").write_text(json.dumps(drafts))
            out = root / "logs" / "ts"
            env = subprocess_env(root / "claude-config")
            result = run_cli(
                "build",
                "--live-root", str(live_root),
                "--plan", str(root / "plan.json"), "--drafts", str(root / "drafts.json"),
                "--out", str(out), "--created-at-line", "0",
                "--findings", str(write_findings(root)), "--stamp", "2026-07-31",
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((out / "manifest.json").read_text())
            # Every manifest result has a byte-identical plain-file copy under results/.
            for proposal in manifest["proposals"]:
                for res in proposal["results"]:
                    plain = out / "results" / f"{proposal['project']}__{res['path']}"
                    self.assertTrue(plain.exists(), plain)
                    self.assertEqual(plain.read_text(), res["content"])

    def test_build_warns_when_redescribe_target_is_packed_index_line(self):
        # refresh_index_lines never rewrites a multi-target (packed) index line, so a
        # redescribe whose only index presence is a packed line is routing-inert;
        # the build must surface that instead of parking a silent no-op.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {"a.md": note("a", body="Fact."), "b.md": note("b", body="Other.")})
            (live / "MEMORY.md").write_text(
                "# idx\n- Packed: [A](a.md) something; [B](b.md) other\n"
            )
            cluster = self._cluster(["a.md"], live)
            plan = {"schema_version": 1, "clusters": [cluster], "deferred": [], "manual_review": []}
            (root / "plan.json").write_text(json.dumps(plan))
            drafts = {"clusters": [{"cluster_id": cluster["cluster_id"], "proposals": [
                {"action": "redescribe", "justification": "sharpen", "survivor": {"path": "a.md", "description": "a sharper routing description for note a"}, "deletes": []},
            ]}]}
            (root / "drafts.json").write_text(json.dumps(drafts))
            out = root / "logs" / "ts"
            env = subprocess_env(root / "claude-config")
            result = run_cli(
                "build",
                "--live-root", str(live_root),
                "--plan", str(root / "plan.json"), "--drafts", str(root / "drafts.json"),
                "--out", str(out), "--created-at-line", "0",
                "--findings", str(write_findings(root)), "--stamp", "2026-07-31",
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(
                report["redescribe_index_warnings"],
                [{"project": "proj", "path": "a.md", "kind": "packed-line-only"}],
            )
            self.assertIn("WARN redescribe proj/a.md", result.stderr)

    def test_build_no_redescribe_warning_for_sole_target_line(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {"a.md": note("a", body="Fact.")})
            records = audit.scan_project_notes(live)
            rec = records["a.md"]
            (live / "MEMORY.md").write_text(
                "# idx\n" + audit.index_entry_line(rec["name"], rec.get("description") or "", "a.md") + "\n"
            )
            cluster = self._cluster(["a.md"], live)
            plan = {"schema_version": 1, "clusters": [cluster], "deferred": [], "manual_review": []}
            (root / "plan.json").write_text(json.dumps(plan))
            drafts = {"clusters": [{"cluster_id": cluster["cluster_id"], "proposals": [
                {"action": "redescribe", "justification": "sharpen", "survivor": {"path": "a.md", "description": "a sharper routing description for note a"}, "deletes": []},
            ]}]}
            (root / "drafts.json").write_text(json.dumps(drafts))
            out = root / "logs" / "ts"
            env = subprocess_env(root / "claude-config")
            result = run_cli(
                "build",
                "--live-root", str(live_root),
                "--plan", str(root / "plan.json"), "--drafts", str(root / "drafts.json"),
                "--out", str(out), "--created-at-line", "0",
                "--findings", str(write_findings(root)), "--stamp", "2026-07-31",
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(report["redescribe_index_warnings"], [])

    def test_multiple_leaves_in_one_cluster_get_distinct_ids(self):
        # Leave proposals carry no survivor or deletes; ids bound only to cluster
        # paths collide when a cluster yields more than one leave, and apply
        # refuses duplicate manifest ids, so build must assign each leave a
        # distinct id up front.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {"a.md": note("a", body="Fact."), "b.md": note("b", body="Other.")})
            cluster = self._cluster(["a.md", "b.md"], live)
            plan = {"schema_version": 1, "clusters": [cluster], "deferred": [], "manual_review": []}
            (root / "plan.json").write_text(json.dumps(plan))
            drafts = {"clusters": [{"cluster_id": cluster["cluster_id"], "proposals": [
                {"action": "leave", "justification": "a is fine", "deletes": []},
                {"action": "leave", "justification": "b is fine", "deletes": []},
            ]}]}
            (root / "drafts.json").write_text(json.dumps(drafts))
            out = root / "logs" / "ts"
            env = subprocess_env(root / "claude-config")
            result = run_cli(
                "build",
                "--live-root", str(live_root),
                "--plan", str(root / "plan.json"), "--drafts", str(root / "drafts.json"),
                "--out", str(out), "--created-at-line", "0",
                "--findings", str(write_findings(root)), "--stamp", "2026-07-31",
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((out / "manifest.json").read_text())
            ids = [p["id"] for p in manifest["proposals"]]
            self.assertEqual(len(ids), 2)
            self.assertEqual(len(set(ids)), 2)

    def test_build_refuses_duplicate_proposal_ids(self):
        # Build-side twin of apply's manifest integrity gate: a token must
        # never park on a manifest apply would refuse. Two leaves with
        # identical justifications produce identical ids, and build must
        # refuse loudly instead of writing an unlandable manifest.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {"a.md": note("a", body="Fact."), "b.md": note("b", body="Other.")})
            cluster = self._cluster(["a.md", "b.md"], live)
            plan = {"schema_version": 1, "clusters": [cluster], "deferred": [], "manual_review": []}
            (root / "plan.json").write_text(json.dumps(plan))
            drafts = {"clusters": [{"cluster_id": cluster["cluster_id"], "proposals": [
                {"action": "leave", "justification": "fine", "deletes": []},
                {"action": "leave", "justification": "fine", "deletes": []},
            ]}]}
            (root / "drafts.json").write_text(json.dumps(drafts))
            out = root / "logs" / "ts"
            env = subprocess_env(root / "claude-config")
            result = run_cli(
                "build",
                "--live-root", str(live_root),
                "--plan", str(root / "plan.json"), "--drafts", str(root / "drafts.json"),
                "--out", str(out), "--created-at-line", "0",
                "--findings", str(write_findings(root)), "--stamp", "2026-07-31",
                env=env,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("duplicate proposal ids", result.stderr)
            self.assertFalse((out / "manifest.json").exists())

    def test_build_warns_when_anticipated_index_exceeds_load_cap(self):
        # Appends past the 25KB/200-line load cap are routing-invisible; build
        # must WARN and record it.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {"a.md": note("a", body="Fact.")})
            # inflate the existing index to just under the byte cap
            filler = "- [x](x.md) — " + "y" * 200 + "\n"
            (live / "MEMORY.md").write_text("- [a](a.md) — a note\n" + filler * 124)
            cluster = self._cluster(["a.md"], live)
            plan = {"schema_version": 1, "clusters": [cluster], "deferred": [], "manual_review": []}
            (root / "plan.json").write_text(json.dumps(plan))
            body = "Topic one.\n\nSee [[a-extract]]."
            drafts = {"clusters": [{"cluster_id": cluster["cluster_id"], "proposals": [{
                "action": "split", "justification": "split it",
                "survivor": {"path": "a.md", "content": note("a", body=body)},
                "extracts": [{"path": "a-extract.md", "content": note("a-extract", body="Topic two, with a description long enough to push the reconciled index over the byte cap for this test scenario.")}],
                "deletes": []}]}]}
            (root / "drafts.json").write_text(json.dumps(drafts))
            out = root / "logs" / "ts"
            env = subprocess_env(root / "claude-config")
            result = run_cli(
                "build",
                "--live-root", str(live_root),
                "--plan", str(root / "plan.json"), "--drafts", str(root / "drafts.json"),
                "--out", str(out), "--created-at-line", "0",
                "--findings", str(write_findings(root)), "--stamp", "2026-07-31",
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((out / "report.json").read_text())
            if report["proposals"]:
                self.assertIn("index_over_cap", report)
                if report["index_over_cap"]:
                    self.assertIn("WARN anticipated index", result.stderr)

    def test_archive_subcommand_selects_cold_entries(self):
        # Deterministic archive build: entries whose latest date is on/before the
        # cutoff are demoted; undated entries stay hot; manifest is token-ready.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {
                "cold.md": note("cold", body="Resolved 2026-06-01."),
                "hot.md": note("hot", body="Active 2026-07-19."),
                "undated.md": note("undated", body="No dates here."),
            })
            (live / "MEMORY.md").write_text(
                "# Index\n"
                "- [cold](cold.md) — resolved 2026-06-01 work\n"
                "- [hot](hot.md) — active 2026-07-19 work\n"
                "- [undated](undated.md) — timeless convention\n"
            )
            out = root / "logs" / "ts"
            env = subprocess_env(root / "claude-config")
            result = run_cli(
                "archive",
                "--live-root", str(live_root),
                "--project", "proj", "--cutoff", "2026-06-25",
                "--out", str(out), "--created-at-line", "0",
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((out / "manifest.json").read_text())
            self.assertEqual(len(manifest["proposals"]), 1)
            prop = manifest["proposals"][0]
            self.assertEqual(prop["action"], "archive")
            self.assertEqual(len(prop["archive_entries"]), 1)
            self.assertIn("cold.md", prop["archive_entries"][0])
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(report["archive"]["entries"], 1)
            self.assertTrue((out / "index-proj.diff").read_text())

    def test_archive_keep_retains_content_hot_candidates(self):
        # Date is only the candidate filter; --keep retains content-hot entries,
        # and the keep decision lands in the report for audit.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {
                "cold.md": note("cold", body="Resolved 2026-06-01."),
                "doctrine.md": note("doctrine", body="Convention settled 2026-05-01."),
            })
            (live / "MEMORY.md").write_text(
                "# Index\n"
                "- [cold](cold.md) — resolved 2026-06-01 work\n"
                "- [doctrine](doctrine.md) — standing convention, settled 2026-05-01\n"
            )
            out = root / "logs" / "ts"
            env = subprocess_env(root / "claude-config")
            result = run_cli(
                "archive",
                "--live-root", str(live_root),
                "--project", "proj", "--cutoff", "2026-06-25",
                "--keep", "doctrine.md",
                "--out", str(out), "--created-at-line", "0",
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            prop = json.loads((out / "manifest.json").read_text())["proposals"][0]
            self.assertEqual(len(prop["archive_entries"]), 1)
            self.assertIn("cold.md", prop["archive_entries"][0])
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(len(report["archive"]["kept"]), 1)
            self.assertIn("doctrine.md", report["archive"]["kept"][0]["entry"])
            self.assertEqual(report["archive"]["kept"][0]["keep"], "doctrine.md")

    def test_schema_failure_recorded_in_dropped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {"a.md": note("a", body="Fact.")})
            cluster = self._cluster(["a.md"], live)
            drafts = {cluster["cluster_id"]: [{"action": "bogus", "justification": "x"}]}
            proposals, dropped = ASM.assemble_proposals([cluster], drafts, live_root, "2026-07-31")
            self.assertEqual(proposals, [])
            self.assertIn("schema:", dropped[0]["reason"])

    def test_name_stem_mismatch_dropped(self):
        # The index renders "[name](path)"; a frontmatter name that drifts from
        # the filename stem becomes wrong link text, and routing treats it as
        # a pseudo-path.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {"a.md": note("a", body="Fact.")})
            cluster = self._cluster(["a.md"], live)
            drafts = {cluster["cluster_id"]: [{
                "action": "compress", "justification": "tighten",
                "survivor": {"path": "a.md", "content": note("pretty-title", body="Fact, tightened.")},
                "deletes": [],
            }]}
            proposals, dropped = ASM.assemble_proposals([cluster], drafts, live_root, "2026-07-31")
            self.assertEqual(proposals, [])
            self.assertIn("name-stem mismatch", dropped[0]["reason"])

    def test_survivor_outside_cluster_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {"a.md": note("a"), "important.md": note("important")})
            cluster = self._cluster(["a.md"], live)  # important.md is NOT in the cluster
            drafts = {cluster["cluster_id"]: [{"action": "period-close", "justification": "x",
                      "survivor": {"path": "important.md", "content": note("important", body="hijacked")},
                      "deletes": ["a.md"]}]}
            proposals, dropped = ASM.assemble_proposals([cluster], drafts, live_root, "2026-07-31")
            self.assertEqual(proposals, [])
            self.assertEqual(dropped[0]["reason"], "survivor outside cluster")

    def test_anticipated_index_diff_previews_reconciliation(self):
        # A period-close (delete old, add fresh new) previews the MEMORY.md change so
        # the index bytes are in the reviewed patch set, not applied unseen.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {"old.md": note("old", body="Old.")})
            (live / "MEMORY.md").write_text("- [Old](old.md)\n")
            proposals = [{
                "id": "p1", "project": "proj", "action": "period-close", "survivor": "new.md",
                "sources": [], "results": [{"path": "new.md", "content": note("new", body="New.")}],
                "deletes": ["old.md"], "justification": "x", "sensitive": False,
            }]
            diff = ASM.anticipated_index_diff(live, proposals)
            self.assertIsNotNone(diff)
            self.assertIn("-- [Old](old.md)", diff)
            self.assertIn("+- [new](new.md)", diff)


class ConfinementTests(unittest.TestCase):
    def test_confined_path_rejects_escapes(self):
        with tempfile.TemporaryDirectory() as temp:
            memory = Path(temp) / "memory"
            memory.mkdir()
            (memory / "ok.md").write_text("x")
            outside = Path(temp) / "outside"
            outside.mkdir()
            (memory / "escape.md").symlink_to(outside / "target.md")
            self.assertIsNotNone(audit.confined_path(memory, "ok.md"))
            self.assertIsNone(audit.confined_path(memory, "../evil.md"))
            self.assertIsNone(audit.confined_path(memory, "/etc/passwd.md"))
            self.assertIsNone(audit.confined_path(memory, "note.txt"))  # non-.md
            self.assertIsNone(audit.confined_path(memory, "MEMORY.md"))
            self.assertIsNone(audit.confined_path(memory, "escape.md"))  # symlink escaping the dir


class ManifestIdTests(unittest.TestCase):
    def _build_id(self, root, survivor_body):
        live = root / "live" / "proj" / "memory"
        if not live.exists():
            live.mkdir(parents=True)
            (live / "a.md").write_text(note("a", body="Fact."))
        cluster = {"cluster_id": ASM.stable_id("proj", ["a.md"], "cluster"), "project": "proj", "top_score": 5,
                   "notes": [{"path": "a.md", "name": "a", "body": "Fact.", "score": 5, "supersessions": 0}]}
        plan = {"schema_version": 1, "clusters": [cluster], "deferred": [], "manual_review": []}
        (root / "plan.json").write_text(json.dumps(plan))
        drafts = {"clusters": [{"cluster_id": cluster["cluster_id"], "proposals": [
            {"action": "compress", "justification": "x", "survivor": {"path": "a.md", "content": survivor_body}, "deletes": []}]}]}
        (root / "drafts.json").write_text(json.dumps(drafts))
        out = root / "out"
        shutil.rmtree(out, ignore_errors=True)
        env = subprocess_env(root / "claude-config")
        run_cli(
            "build",
            "--live-root", str(root / "live"),
            "--plan", str(root / "plan.json"), "--drafts", str(root / "drafts.json"),
            "--out", str(out), "--created-at-line", "0",
            "--findings", str(write_findings(root)), "--stamp", "2026-07-31",
            env=env,
            check=True,
        )
        return json.loads((out / "manifest.json").read_text())["id"]

    def test_manifest_id_is_content_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            id1 = self._build_id(root, note("a", body="First wording."))
            id2 = self._build_id(root, note("a", body="Different wording entirely."))
            self.assertNotEqual(id1, id2)  # same targets/action, different content -> different id


class SplitRedescribeTests(unittest.TestCase):
    """The v2 actions: split (mega-note to atomic notes) and redescribe (routing fix)."""

    _project = AssembleTests._project
    _cluster = AssembleTests._cluster

    DONOR = (
        "---\nname: mega\ndescription: old hook that no longer routes\nmetadata:\n"
        "  node_type: memory\n  type: project\n  originSessionId: sess-mega\n---\n"
        "Topic one prose.\n\nTopic two prose.\n"
    )

    def _split_draft(self, survivor_body="Core topic. See [[gotcha]] and [[sizing]]."):
        survivor = (
            "---\nname: mega\ndescription: core topic after the split rewrite\n"
            f"metadata:\n  type: project\n---\n{survivor_body}\n"
        )
        extracts = [
            {
                "path": "gotcha.md",
                "content": "---\nname: gotcha\ndescription: reusable operational gotcha worth recalling\nmetadata:\n  type: reference\n---\nThe gotcha.\n",
            },
            {
                "path": "sizing.md",
                "content": "---\nname: sizing\ndescription: durable sizing rule derived from the trial\nmetadata:\n  type: feedback\n---\nThe sizing rule.\n",
            },
        ]
        return {
            "action": "split",
            "justification": "mega-note holds three unrelated topics",
            "survivor": {"path": "mega.md", "content": survivor},
            "extracts": extracts,
            "deletes": [],
        }

    def test_validate_split_and_redescribe_shapes(self):
        ok_extract = [{"path": "e.md", "content": "x"}]
        base = {"action": "split", "justification": "x", "deletes": [], "survivor": {"path": "a.md", "content": "c"}}
        self.assertIsNotNone(ASM.validate_proposal(base))  # split needs extracts
        self.assertIsNotNone(
            ASM.validate_proposal({**base, "extracts": [{"path": f"e{i}.md", "content": "x"} for i in range(7)]})
        )  # over the fan-out cap
        self.assertIsNotNone(
            ASM.validate_proposal({**base, "extracts": [{"path": "e.md", "content": "x"}, {"path": "e.md", "content": "y"}]})
        )  # duplicate extract paths
        self.assertIsNotNone(
            ASM.validate_proposal({**base, "extracts": [{"path": "a.md", "content": "x"}]})
        )  # extract shadows the survivor
        self.assertIsNone(ASM.validate_proposal({**base, "extracts": ok_extract}))
        self.assertIsNotNone(
            ASM.validate_proposal(
                {"action": "compress", "justification": "x", "deletes": [], "survivor": {"path": "a.md", "content": "c"}, "extracts": ok_extract}
            )
        )  # only split carries extracts
        redescribe = {"action": "redescribe", "justification": "x", "deletes": [], "survivor": {"path": "a.md", "description": "a much better routing hook"}}
        self.assertIsNone(ASM.validate_proposal(redescribe))
        self.assertIsNotNone(
            ASM.validate_proposal({**redescribe, "survivor": {"path": "a.md"}})
        )  # description required
        self.assertIsNotNone(
            ASM.validate_proposal({**redescribe, "deletes": ["a.md"]})
        )  # redescribe deletes nothing

    def test_split_assembles_extracts_with_inherited_schema(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {"mega.md": self.DONOR})
            cluster = self._cluster(["mega.md"], live)
            drafts = {cluster["cluster_id"]: [self._split_draft()]}
            proposals, dropped = ASM.assemble_proposals([cluster], drafts, live_root, "2026-07-31")
            self.assertEqual(dropped, [])
            proposal = proposals[0]
            self.assertEqual(proposal["action"], "split")
            self.assertEqual([r["path"] for r in proposal["results"]], ["mega.md", "gotcha.md", "sizing.md"])
            self.assertEqual(proposal["deletes"], [])
            self.assertEqual([s["path"] for s in proposal["sources"]], ["mega.md"])
            for result in proposal["results"]:
                # Every resulting file inherits the split note's schema frontmatter.
                self.assertIn("node_type: memory", result["content"])
                self.assertIn("originSessionId: sess-mega", result["content"])
            gotcha = proposal["results"][1]["content"]
            self.assertIn("name: gotcha", gotcha)
            self.assertIn("type: reference", gotcha)  # drafter's type honored

    def test_split_unlinked_extract_dropped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {"mega.md": self.DONOR})
            cluster = self._cluster(["mega.md"], live)
            draft = self._split_draft(survivor_body="Core topic. See [[gotcha]] only.")
            drafts = {cluster["cluster_id"]: [draft]}
            proposals, dropped = ASM.assemble_proposals([cluster], drafts, live_root, "2026-07-31")
            self.assertEqual(proposals, [])
            self.assertIn("does not link", dropped[0]["reason"])
            self.assertIn("sizing.md", dropped[0]["reason"])

    def test_split_existing_extract_path_dropped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(
                root, {"mega.md": self.DONOR, "gotcha.md": note("gotcha", body="Already here.")}
            )
            cluster = self._cluster(["mega.md"], live)
            drafts = {cluster["cluster_id"]: [self._split_draft()]}
            proposals, dropped = ASM.assemble_proposals([cluster], drafts, live_root, "2026-07-31")
            self.assertEqual(proposals, [])
            self.assertIn("extract path already exists", dropped[0]["reason"])

    def test_blurred_sibling_descriptions_dropped(self):
        # Two extracts whose descriptions cannot be told apart at recall time make
        # routing worse than the mega-note.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {"mega.md": self.DONOR})
            cluster = self._cluster(["mega.md"], live)
            draft = self._split_draft()
            draft["extracts"][0]["content"] = draft["extracts"][0]["content"].replace(
                "reusable operational gotcha worth recalling",
                "reusable MLX serving gotchas from the champion trial",
            )
            draft["extracts"][1]["content"] = draft["extracts"][1]["content"].replace(
                "durable sizing rule derived from the trial",
                "reusable MLX serving lessons from the champion trial",
            )
            drafts = {cluster["cluster_id"]: [draft]}
            proposals, dropped = ASM.assemble_proposals([cluster], drafts, live_root, "2026-07-31")
            self.assertEqual(proposals, [])
            self.assertIn("sibling descriptions not discriminative", dropped[0]["reason"])
            self.assertIn("gotcha.md ~ sizing.md", dropped[0]["reason"])

    def test_extract_description_colliding_with_existing_note_dropped(self):
        # Distractor confusion comes from ALL project siblings: a new description
        # that blurs with an existing (untouched) note's description is rejected.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bystander = note("bystander").replace(
                "Fixture note describing bystander in detail",
                "reusable operational gotcha worth recalling later",
            )
            live_root, live = self._project(root, {"mega.md": self.DONOR, "bystander.md": bystander})
            cluster = self._cluster(["mega.md"], live)
            drafts = {cluster["cluster_id"]: [self._split_draft()]}
            proposals, dropped = ASM.assemble_proposals([cluster], drafts, live_root, "2026-07-31")
            self.assertEqual(proposals, [])
            self.assertIn("sibling descriptions not discriminative", dropped[0]["reason"])
            self.assertIn("existing bystander.md", dropped[0]["reason"])

    def test_split_survivor_must_be_existing_cluster_note(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {"mega.md": self.DONOR})
            cluster = self._cluster(["mega.md"], live)
            draft = self._split_draft()
            draft["survivor"]["path"] = "fresh.md"
            drafts = {cluster["cluster_id"]: [draft]}
            _proposals, dropped = ASM.assemble_proposals([cluster], drafts, live_root, "2026-07-31")
            self.assertIn("split survivor not an existing cluster note", dropped[0]["reason"])

    def test_redescribe_preserves_body_and_quotes_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {"mega.md": self.DONOR})
            cluster = self._cluster(["mega.md"], live)
            new_description = "verdict superseded, near-parity confirmed under tag #followup"
            drafts = {
                cluster["cluster_id"]: [
                    {
                        "action": "redescribe",
                        "justification": "stale verdict in the hook",
                        "survivor": {"path": "mega.md", "description": new_description},
                        "deletes": [],
                    }
                ]
            }
            proposals, dropped = ASM.assemble_proposals([cluster], drafts, live_root, "2026-07-31")
            self.assertEqual(dropped, [])
            content = proposals[0]["results"][0]["content"]
            self.assertIn(f'description: "{new_description}"\n', content)
            # Everything except the description line is byte-identical to the donor.
            self.assertEqual(
                content.replace(
                    f'description: "{new_description}"',
                    "description: old hook that no longer routes",
                ),
                self.DONOR,
            )

    def test_compress_fresh_survivor_path_dropped(self):
        # compress rewrites IN PLACE: a fresh survivor path would leave the flagged
        # note untouched while creating an unreviewed sibling beside it.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {"mega.md": self.DONOR})
            cluster = self._cluster(["mega.md"], live)
            drafts = {
                cluster["cluster_id"]: [
                    {
                        "action": "compress",
                        "justification": "x",
                        "survivor": {"path": "fresh.md", "content": note("fresh", body="Compressed elsewhere.")},
                        "deletes": [],
                    }
                ]
            }
            proposals, dropped = ASM.assemble_proposals([cluster], drafts, live_root, "2026-07-31")
            self.assertEqual(proposals, [])
            self.assertIn("compress survivor not an existing cluster note", dropped[0]["reason"])

    def test_redescribe_fresh_path_dropped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {"mega.md": self.DONOR})
            cluster = self._cluster(["mega.md"], live)
            drafts = {
                cluster["cluster_id"]: [
                    {
                        "action": "redescribe",
                        "justification": "x",
                        "survivor": {"path": "fresh.md", "description": "a fresh note cannot be redescribed"},
                        "deletes": [],
                    }
                ]
            }
            _proposals, dropped = ASM.assemble_proposals([cluster], drafts, live_root, "2026-07-31")
            self.assertIn("redescribe survivor not an existing cluster note", dropped[0]["reason"])

    def test_merge_records_multi_source_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            note_a = (
                "---\nname: a\ndescription: first duplicate note about the outage\nmetadata:\n"
                "  node_type: memory\n  type: project\n  originSessionId: sess-a\n---\nDup one.\n"
            )
            note_b = (
                "---\nname: b\ndescription: second duplicate note about the outage\nmetadata:\n"
                "  node_type: memory\n  type: project\n  originSessionId: sess-b\n---\nDup two.\n"
            )
            live_root, live = self._project(root, {"a.md": note_a, "b.md": note_b})
            cluster = self._cluster(["a.md", "b.md"], live)
            merged = "---\nname: a\ndescription: canonical outage note after the merge\nmetadata:\n  type: project\n---\nMerged truth.\n"
            drafts = {
                cluster["cluster_id"]: [
                    {
                        "action": "merge",
                        "justification": "near duplicates",
                        "survivor": {"path": "a.md", "content": merged},
                        "deletes": ["b.md"],
                    }
                ]
            }
            proposals, dropped = ASM.assemble_proposals([cluster], drafts, live_root, "2026-07-31")
            self.assertEqual(dropped, [])
            content = proposals[0]["results"][0]["content"]
            self.assertIn("originSessionId: sess-a\n", content)  # donor provenance kept
            self.assertIn("originSessionIds: sess-a, sess-b\n", content)  # honest lineage

    def test_index_preview_covers_split_extracts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {"mega.md": self.DONOR})
            (live / "MEMORY.md").write_text("- [mega](mega.md): old hook that no longer routes\n")
            cluster = self._cluster(["mega.md"], live)
            drafts = {cluster["cluster_id"]: [self._split_draft()]}
            proposals, dropped = ASM.assemble_proposals([cluster], drafts, live_root, "2026-07-31")
            self.assertEqual(dropped, [])
            diff = ASM.anticipated_index_diff(live, proposals)
            self.assertIsNotNone(diff)
            self.assertIn("+- [gotcha](gotcha.md): reusable operational gotcha worth recalling", diff)
            self.assertIn("+- [sizing](sizing.md): durable sizing rule derived from the trial", diff)
            # The split note's own entry line is refreshed to the new routing hook.
            self.assertIn("+- [mega](mega.md): core topic after the split rewrite", diff)
            self.assertIn("-- [mega](mega.md): old hook that no longer routes", diff)


class HardeningTests(unittest.TestCase):
    """Findings gate (verification coverage + content-binding), index-growth
    refusal, decay-frontmatter stamping, and casing-drift lint enforcement."""

    _project = AssembleTests._project
    _cluster = AssembleTests._cluster
    DONOR = SplitRedescribeTests.DONOR

    DECAY_DONOR = (
        "---\nname: mined\ndescription: mined note carrying the decay pair for bump tests\nmetadata:\n"
        "  node_type: memory\n  type: project\n  originSessionId: sess-mined\n"
        "  confidence: 0.9\n  maturity: candidate\n  last_validated: 2026-01-01\n---\n"
        "Mined fact.\n"
    )

    def _build_cli(self, root, live_root, out, extra=()):
        env = subprocess_env(root / "claude-config")
        return run_cli(
            "build",
            "--live-root", str(live_root),
            "--plan", str(root / "plan.json"), "--drafts", str(root / "drafts.json"),
            "--out", str(out), "--created-at-line", "0",
            "--findings", str(root / "findings.json"), "--stamp", "2026-07-31", *extra,
            env=env,
        )

    def _compress_setup(self, root, donor):
        live_root, live = self._project(root, {"a.md": donor})
        cluster = self._cluster(["a.md"], live)
        plan = {"schema_version": 1, "clusters": [cluster], "deferred": [], "manual_review": []}
        (root / "plan.json").write_text(json.dumps(plan))
        drafts = {"clusters": [{"cluster_id": cluster["cluster_id"], "proposals": [{
            "action": "compress", "justification": "compress it",
            "survivor": {"path": "a.md", "content": note("a", body="Durable fact only.")},
            "deletes": []}]}]}
        (root / "drafts.json").write_text(json.dumps(drafts))
        return live_root, cluster

    def test_findings_gate_refuses_uncovered_cluster(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, _cluster_obj = self._compress_setup(root, note("a", body="Fact."))
            (root / "findings.json").write_text(json.dumps({"clusters": {}}))  # no coverage
            result = self._build_cli(root, live_root, root / "out")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("verification-coverage gate", result.stderr)
            self.assertFalse((root / "out" / "manifest.json").exists())

    def test_findings_gate_requires_clean_or_fixed_status(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, cluster = self._compress_setup(root, note("a", body="Fact."))
            (root / "findings.json").write_text(
                json.dumps({"clusters": {cluster["cluster_id"]: {"status": "pending"}}})
            )
            result = self._build_cli(root, live_root, root / "out")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("verification-coverage gate", result.stderr)

    def test_findings_gate_requires_drafts_digest(self):
        # clean/fixed status alone is not enough: the entry must also carry a
        # drafts_digest binding it to this cluster's exact proposals payload.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, cluster = self._compress_setup(root, note("a", body="Fact."))
            (root / "findings.json").write_text(
                json.dumps({"clusters": {cluster["cluster_id"]: {"status": "clean"}}})
            )
            result = self._build_cli(root, live_root, root / "out")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("verification-coverage gate", result.stderr)
            self.assertIn("drafts_digest", result.stderr)
            self.assertFalse((root / "out" / "manifest.json").exists())

    def test_findings_gate_refuses_stale_drafts_digest(self):
        # A digest computed for an earlier redraft of the same cluster id must
        # not authorize the CURRENT drafts payload.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, cluster = self._compress_setup(root, note("a", body="Fact."))
            (root / "findings.json").write_text(
                json.dumps({"clusters": {cluster["cluster_id"]: {
                    "status": "clean", "drafts_digest": "0" * 16,
                }}})
            )
            result = self._build_cli(root, live_root, root / "out")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("verification-coverage gate", result.stderr)
            self.assertIn("stale content", result.stderr)
            self.assertFalse((root / "out" / "manifest.json").exists())

    def test_findings_gate_passes_with_matching_drafts_digest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, _cluster = self._compress_setup(root, note("a", body="Fact."))
            write_findings(root)  # computes real per-cluster digests
            result = self._build_cli(root, live_root, root / "out")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / "out" / "manifest.json").exists())

    def test_build_refuses_when_source_note_changed_since_plan(self):
        # The findings gate above only binds the drafted PROPOSALS
        # (drafts_digest), not the source bytes those proposals were drafted
        # from. A source note edited after plan/verification but before build
        # must be caught here, or assembly would silently consume the
        # unverified new content (assemble_proposals reads the live donor
        # file, not the plan's snapshot).
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, _cluster = self._compress_setup(root, note("a", body="Fact."))
            write_findings(root)  # findings bound to the ORIGINAL drafts payload
            (live_root / "proj" / "memory" / "a.md").write_text(note("a", body="Fact changed after plan."))
            result = self._build_cli(root, live_root, root / "out")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("source notes changed since plan", result.stderr)
            self.assertFalse((root / "out" / "manifest.json").exists())

    def test_build_refuses_when_source_note_deleted_since_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, _cluster = self._compress_setup(root, note("a", body="Fact."))
            write_findings(root)
            (live_root / "proj" / "memory" / "a.md").unlink()
            result = self._build_cli(root, live_root, root / "out")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("source notes changed since plan", result.stderr)
            self.assertFalse((root / "out" / "manifest.json").exists())

    def test_extracts_get_decay_frontmatter(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {"mega.md": self.DONOR})
            cluster = self._cluster(["mega.md"], live)
            draft = SplitRedescribeTests._split_draft(SplitRedescribeTests())
            drafts = {cluster["cluster_id"]: [draft]}
            proposals, dropped = ASM.assemble_proposals([cluster], drafts, live_root, "2026-07-31")
            self.assertEqual(dropped, [])
            for result in proposals[0]["results"][1:]:  # the extracts
                self.assertIn("confidence: 0.8", result["content"])
                self.assertIn("maturity: candidate", result["content"])
                self.assertIn("last_validated: 2026-07-31", result["content"])

    def test_survivor_decay_bump_only_when_donor_carries_pair(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(
                root, {"mined.md": self.DECAY_DONOR, "plain.md": note("plain", body="No decay fields.")}
            )
            rewrites = {
                "mined": "---\nname: mined\ndescription: durable alpha subsystem retention conclusion\nmetadata:\n  type: project\n---\nRewritten durable fact.\n",
                "plain": "---\nname: plain\ndescription: beta pipeline quirk record without staleness tracking\nmetadata:\n  type: project\n---\nRewritten durable fact.\n",
            }
            for path, donor_name, expects_bump in (("mined.md", "mined", True), ("plain.md", "plain", False)):
                cluster = self._cluster([path], live)
                drafts = {cluster["cluster_id"]: [{
                    "action": "compress", "justification": "x",
                    "survivor": {"path": path, "content": rewrites[donor_name]},
                    "deletes": []}]}
                proposals, dropped = ASM.assemble_proposals([cluster], drafts, live_root, "2026-07-31")
                self.assertEqual(dropped, [], f"{path}: {dropped}")
                content = proposals[0]["results"][0]["content"]
                if expects_bump:
                    self.assertIn("last_validated: 2026-07-31", content)
                    self.assertIn("confidence: 0.9", content)  # confidence untouched
                else:
                    self.assertNotIn("last_validated", content)

    def test_redescribe_survivor_decay_bump_only_when_donor_carries_pair(self):
        # The decay revalidation bump used to live only inside the
        # merge/period-close branch, so a decay-tracked note whose description
        # was corrected via redescribe stayed immediately re-flagged as decayed.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(
                root, {"mined.md": self.DECAY_DONOR, "plain.md": note("plain", body="No decay fields.")}
            )
            new_desc = {
                "mined": "durable alpha subsystem retention conclusion, revalidated",
                "plain": "beta pipeline quirk record without staleness tracking, revalidated",
            }
            for path, donor_name, expects_bump in (("mined.md", "mined", True), ("plain.md", "plain", False)):
                cluster = self._cluster([path], live)
                drafts = {cluster["cluster_id"]: [{
                    "action": "redescribe", "justification": "x",
                    "survivor": {"path": path, "description": new_desc[donor_name]},
                    "deletes": []}]}
                proposals, dropped = ASM.assemble_proposals([cluster], drafts, live_root, "2026-07-31")
                self.assertEqual(dropped, [], f"{path}: {dropped}")
                content = proposals[0]["results"][0]["content"]
                self.assertIn(new_desc[donor_name], content)  # redescribe still applied
                if expects_bump:
                    self.assertIn("last_validated: 2026-07-31", content)
                    self.assertIn("confidence: 0.9", content)  # confidence untouched
                else:
                    self.assertNotIn("last_validated", content)

    def test_index_growth_refused_when_over_cap_then_allowed_by_flag(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {"mega.md": self.DONOR})
            # Index already OVER the byte cap, backed by REAL notes (reconcile
            # prunes entries whose files are gone, which would deflate the index
            # under the cap and mask the gate). A split appends two more entries.
            index_lines = ["- [mega](mega.md) — old hook"]
            for i in range(130):
                stem = f"filler-{i:03d}"
                (live / f"{stem}.md").write_text(note(stem, body="Filler."))
                index_lines.append(f"- [{stem}]({stem}.md) — " + "y" * 180)
            (live / "MEMORY.md").write_text("\n".join(index_lines) + "\n")
            cluster = self._cluster(["mega.md"], live)
            plan = {"schema_version": 1, "clusters": [cluster], "deferred": [], "manual_review": []}
            (root / "plan.json").write_text(json.dumps(plan))
            draft = SplitRedescribeTests._split_draft(SplitRedescribeTests())
            (root / "drafts.json").write_text(
                json.dumps({"clusters": [{"cluster_id": cluster["cluster_id"], "proposals": [draft]}]})
            )
            write_findings(root)
            refused = self._build_cli(root, live_root, root / "out1")
            self.assertEqual(refused.returncode, 2, refused.stderr)
            self.assertIn("GROWS the over-cap index", refused.stderr)
            self.assertFalse((root / "out1" / "manifest.json").exists())
            allowed = self._build_cli(root, live_root, root / "out2", extra=("--allow-index-growth",))
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            report = json.loads((root / "out2" / "report.json").read_text())
            self.assertTrue(report["index_over_cap"])

    def test_index_growth_refused_on_line_growth_when_bytes_shrink(self):
        # The exact bypass a byte-only over-cap check would miss: a long
        # dead-reference line gets dropped (shrinking bytes) while the split
        # appends two short new entries, pushing the LINE count higher even though
        # raw_bytes ends up <= current_bytes. Line growth alone must still refuse.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {"mega.md": self.DONOR})
            index_lines = ["- [mega](mega.md): old hook that no longer routes"]
            for i in range(201):
                stem = f"filler-{i:03d}"
                (live / f"{stem}.md").write_text(note(stem, body="Filler."))
                index_lines.append(f"- [{stem}]({stem}.md)")
            # A single long dead-reference line: reconcile drops it whole, which by
            # itself SHRINKS total bytes more than the two short appended entries add.
            index_lines.append("- [Gone](gone.md): " + "z" * 280)
            current_bytes = len(("\n".join(index_lines) + "\n").encode("utf-8"))
            current_lines = len(index_lines)
            self.assertGreater(current_lines, config.INDEX_LOAD_MAX_LINES)  # over cap by LINES
            self.assertLess(current_bytes, config.INDEX_LOAD_MAX_BYTES)  # NOT over cap by bytes
            (live / "MEMORY.md").write_text("\n".join(index_lines) + "\n")
            cluster = self._cluster(["mega.md"], live)
            plan = {"schema_version": 1, "clusters": [cluster], "deferred": [], "manual_review": []}
            (root / "plan.json").write_text(json.dumps(plan))
            draft = SplitRedescribeTests._split_draft(SplitRedescribeTests())
            (root / "drafts.json").write_text(
                json.dumps({"clusters": [{"cluster_id": cluster["cluster_id"], "proposals": [draft]}]})
            )
            write_findings(root)
            refused = self._build_cli(root, live_root, root / "out1")
            self.assertEqual(refused.returncode, 2, refused.stderr)
            self.assertIn("GROWS the over-cap index", refused.stderr)
            self.assertFalse((root / "out1" / "manifest.json").exists())
            allowed = self._build_cli(root, live_root, root / "out2", extra=("--allow-index-growth",))
            self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_casing_drift_flagged_for_new_minority_separator_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, live = self._project(root, {
                "mega.md": self.DONOR,
                "kebab-one.md": note("kebab-one", body="A.", ),
                "kebab-two.md": note("kebab-two", body="B."),
            })
            cluster = self._cluster(["mega.md"], live)
            plan = {"schema_version": 1, "clusters": [cluster], "deferred": [], "manual_review": []}
            (root / "plan.json").write_text(json.dumps(plan))
            survivor = (
                "---\nname: mega\ndescription: core topic after the split rewrite\n"
                "metadata:\n  type: project\n---\nCore topic. See [[snake_extract]].\n"
            )
            draft = {
                "action": "split", "justification": "x",
                "survivor": {"path": "mega.md", "content": survivor},
                "extracts": [{
                    "path": "snake_extract.md",
                    "content": "---\nname: snake_extract\ndescription: reusable operational gotcha worth recalling\nmetadata:\n  type: reference\n---\nThe gotcha.\n",
                }],
                "deletes": [],
            }
            (root / "drafts.json").write_text(
                json.dumps({"clusters": [{"cluster_id": cluster["cluster_id"], "proposals": [draft]}]})
            )
            write_findings(root)
            result = self._build_cli(root, live_root, root / "out")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("casing drift", result.stderr)
            report = json.loads((root / "out" / "report.json").read_text())
            self.assertEqual(
                [d["path"] for d in report["casing_drift"]], ["snake_extract.md"]
            )


if __name__ == "__main__":
    unittest.main()
