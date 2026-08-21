#!/usr/bin/env python3
"""Tests for memory_dream.apply: the operator-approval safety gate stack.

Every Harness.run() call shells out to ``python3 -m memory_dream apply`` in a
child process, so each gets its own clean CLAUDE_CONFIG_DIR (never a
developer's real ~/.claude/memory-dream.json) and never inherits
MEMORY_DREAM_*/CLAUDE_MEMORY_*/CLAUDE_JOB_DIR from the outer environment.
"""

import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from memory_dream import apply as apply_mod
from memory_dream import audit
from memory_dream import transcript
from memory_dream.compat import FileLock

REPO_ROOT = Path(__file__).resolve().parents[1]


def _clean_env(claude_config_dir: Path) -> dict:
    """A subprocess env with no leaked MEMORY_DREAM_*/CLAUDE_MEMORY_*/CLAUDE_JOB_DIR
    and a per-test CLAUDE_CONFIG_DIR, so a developer's real config can never leak in."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("MEMORY_DREAM_", "CLAUDE_MEMORY_")) and key != "CLAUDE_JOB_DIR"
    }
    env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir)
    return env


def note(name="note", body="Body.", note_type="project"):
    return (
        "---\n"
        f"name: {name}\n"
        f"description: Fixture note describing {name} in detail\n"
        f"metadata:\n  type: {note_type}\n"
        "---\n"
        f"{body}\n"
    )


class Harness:
    """A live+mirror tree, a transcript with an operator approval, and a patch set."""

    def __init__(self, root: Path, approval=None, created_at_line=2, turn_index=2):
        self.root = root
        self.live_root = root / "live"
        self.mirror_root = root / "mirror"
        self.claude_config_dir = root / "claude-config"
        self.claude_config_dir.mkdir(parents=True, exist_ok=True)
        self.logs = root / "logs"
        self.patch_set = self.logs / "20260717-000000"
        self.patch_set.mkdir(parents=True)
        self._approval_override = approval  # None => derive from the content-bound id
        self.created_at_line = created_at_line
        self.turn_index = turn_index
        self._proposals: list = []

    def project(self, key="proj", mirror=True):
        live = self.live_root / key / "memory"
        live.mkdir(parents=True)
        if mirror:
            (self.mirror_root / key).mkdir(parents=True)
        return live

    def mirror_sync(self, key="proj"):
        dest = self.mirror_root / key
        dest.mkdir(parents=True, exist_ok=True)
        for path in (self.live_root / key / "memory").iterdir():
            if path.is_file():
                shutil.copy2(path, dest / path.name)

    def add_proposal(self, proposal):
        self._proposals.append(proposal)

    def digest(self, key, rel):
        return audit.digest(self.live_root / key / "memory" / rel)

    def write(self, manifest_id=None):
        # The manifest id is content-bound (apply recomputes it), so derive it from
        # the proposals unless a test forces a specific (mismatching) value.
        self.manifest_id = manifest_id if manifest_id is not None else audit.content_id(self._proposals)
        # The approval message must carry the id token so the trace is consent to
        # this patch set; tests can override the text to exercise the token gate.
        self.approval = self._approval_override if self._approval_override is not None else f"approve all {self.manifest_id}"
        transcript_lines = [
            {"type": "user", "message": {"role": "user", "content": "run the dream pass"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "preview"}]}},
            {"type": "user", "message": {"role": "user", "content": self.approval}},
        ]
        manifest = {
            "schema_version": 1,
            "id": self.manifest_id,
            "created_at_line": self.created_at_line,
            "proposals": self._proposals,
        }
        (self.patch_set / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        transcript_path = self.live_root / "proj" / "sess.jsonl"
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text("\n".join(json.dumps(entry) for entry in transcript_lines), encoding="utf-8", newline="\n")
        self.transcript = transcript_path
        return self

    def selection(self, approved, trace=None, path_name="selection.json", patch_set_id=None):
        if trace is None:
            trace = {
                "message_sha256": hashlib.sha256(self.approval.encode()).hexdigest(),
                "turn_index": self.turn_index,
            }
        selection = {
            "approved": approved,
            "operator_trace": trace,
            "patch_set_id": self.manifest_id if patch_set_id is None else patch_set_id,
        }
        path = self.root / path_name
        path.write_text(json.dumps(selection), encoding="utf-8", newline="\n")
        return path

    def run(self, selection_path, *extra, mirror=True):
        command = [
            sys.executable, "-m", "memory_dream", "apply",
            "--patch-set", str(self.patch_set),
            "--selection", str(selection_path),
            "--transcript", str(self.transcript),
            "--live-root", str(self.live_root),
        ]
        if mirror:
            command += ["--mirror-root", str(self.mirror_root)]
        command += list(extra)
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            cwd=REPO_ROOT,
            env=_clean_env(self.claude_config_dir),
        )


class ApplyFixtureMixin:
    """Shared fixture builders for apply-focused test classes (a mixin, not a
    TestCase, so subclassing never duplicates inherited test methods)."""

    def _close_old_proposal(self, harness, key="proj", pid="p1"):
        harness.add_proposal(
            {
                "id": pid,
                "project": key,
                "action": "period-close",
                "sources": [{"path": "old.md", "digest": harness.digest(key, "old.md")}],
                "results": [],
                "deletes": ["old.md"],
                "justification": "superseded",
                "sensitive": False,
            }
        )

    def _standard_project(self, harness, key="proj"):
        live = harness.project(key)
        (live / "MEMORY.md").write_text("- [Old](old.md)\n- [New](new.md)\n", encoding="utf-8", newline="\n")
        (live / "old.md").write_text(note("old", body="SUPERSEDED: see new."), encoding="utf-8", newline="\n")
        (live / "new.md").write_text(note("new", body="Current truth."), encoding="utf-8", newline="\n")
        harness.mirror_sync(key)
        return live


class MemoryDreamApplyTests(ApplyFixtureMixin, unittest.TestCase):

    def test_archive_proposal_moves_index_entries(self):
        # Archive is index-only: entries move from MEMORY.md to MEMORY-archive.md
        # with a pointer line added; note bodies are untouched; digest-gated.
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            live = harness.project()
            (live / "old.md").write_text(note("old"), encoding="utf-8", newline="\n")
            (live / "hot.md").write_text(note("hot"), encoding="utf-8", newline="\n")
            (live / "MEMORY.md").write_text("# Index\n- [old](old.md) — resolved 2026-06-01 thing\n- [hot](hot.md) — active thing\n", encoding="utf-8", newline="\n")
            harness.mirror_sync()
            entry = "- [old](old.md) — resolved 2026-06-01 thing"
            harness.add_proposal({
                "id": "a1", "project": "proj", "action": "archive",
                "justification": "demote cold entry",
                "sources": [{"path": "MEMORY.md", "digest": harness.digest("proj", "MEMORY.md")}],
                "results": [], "deletes": [], "archive_entries": [entry],
                "sensitive": False, "survivor": None,
            })
            harness.write()
            result = harness.run(harness.selection(["a1"]))
            self.assertEqual(result.returncode, 0, result.stderr)
            index = (live / "MEMORY.md").read_text(encoding="utf-8")
            self.assertNotIn("- [old](old.md)", index)
            self.assertIn("- [hot](hot.md)", index)
            self.assertIn("MEMORY-archive.md", index)  # pointer line
            archive = (live / "MEMORY-archive.md").read_text(encoding="utf-8")
            self.assertIn(entry, archive)
            self.assertEqual((live / "old.md").read_text(encoding="utf-8"), note("old"))  # body untouched

    def test_archive_skips_on_stale_index_digest(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            live = harness.project()
            (live / "old.md").write_text(note("old"), encoding="utf-8", newline="\n")
            (live / "MEMORY.md").write_text("# Index\n- [old](old.md) — cold 2026-06-01\n", encoding="utf-8", newline="\n")
            harness.mirror_sync()
            harness.add_proposal({
                "id": "a1", "project": "proj", "action": "archive",
                "justification": "demote",
                "sources": [{"path": "MEMORY.md", "digest": "0" * 64}],
                "results": [], "deletes": [], "archive_entries": ["- [old](old.md) — cold 2026-06-01"],
                "sensitive": False, "survivor": None,
            })
            harness.write()
            result = harness.run(harness.selection(["a1"]))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("source-changed-since-draft", result.stdout + result.stderr + (harness.patch_set / "apply-manifest.json").read_text(encoding="utf-8"))
            self.assertFalse((live / "MEMORY-archive.md").exists())

    def test_refuses_missing_and_mismatched_trace(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            self._standard_project(harness)
            self._close_old_proposal(harness)
            harness.write()
            no_trace = harness.selection(["p1"], trace={}, path_name="no_trace.json")
            result = harness.run(no_trace)
            self.assertEqual(result.returncode, 1)
            self.assertIn("operator trace invalid", result.stderr)
            bad = harness.selection(
                ["p1"],
                trace={"message_sha256": "deadbeef", "turn_index": 2},
                path_name="bad.json",
            )
            result = harness.run(bad)
            self.assertEqual(result.returncode, 1)
            self.assertIn("hash does not match", result.stderr)
            self.assertTrue((harness.live_root / "proj" / "memory" / "old.md").exists())

    def test_refuses_automated_sequence_without_post_preview_approval(self):
        # The only human turn precedes the preview (created_at_line), so no
        # post-preview approval exists and apply refuses.
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp), created_at_line=3, turn_index=0)
            self._standard_project(harness)
            self._close_old_proposal(harness)
            harness.write()
            trace = {
                "message_sha256": hashlib.sha256(b"run the dream pass").hexdigest(),
                "turn_index": 0,
            }
            result = harness.run(harness.selection(["p1"], trace=trace))
            self.assertEqual(result.returncode, 1)
            self.assertIn("precedes the preview", result.stderr)

    def test_refuses_stale_mirror_and_names_kind(self):
        # Mirror mode: the freshness gate only runs when --mirror-root is passed
        # (the Harness default), so this exercises the mirror-mode refusal path.
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            live = self._standard_project(harness)
            self._close_old_proposal(harness)
            harness.write()
            # Diverge the mirror after the digest was captured.
            (harness.mirror_root / "proj" / "new.md").write_text(note("new", body="mirror drift"), encoding="utf-8", newline="\n")
            result = harness.run(harness.selection(["p1"]))
            self.assertEqual(result.returncode, 1)
            self.assertIn("mirror not fresh", result.stderr)
            self.assertIn("mirror_stale_file", result.stderr)
            self.assertTrue((live / "old.md").exists())

    def test_skips_path_escape_but_applies_rest(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            live = self._standard_project(harness)
            self._close_old_proposal(harness)
            harness.add_proposal(
                {
                    "id": "escape",
                    "project": "proj",
                    "action": "compress",
                    "sources": [{"path": "new.md", "digest": harness.digest("proj", "new.md")}],
                    "results": [{"path": "../../../escape.md", "content": "x"}],
                    "deletes": [],
                    "justification": "malicious",
                    "sensitive": False,
                }
            )
            harness.write()
            result = harness.run(harness.selection(["p1", "escape"]))
            self.assertEqual(result.returncode, 0)
            manifest = json.loads((harness.patch_set / "apply-manifest.json").read_text(encoding="utf-8"))
            statuses = {s["id"]: s for entry in manifest["projects"] for s in entry["proposals"]}
            self.assertEqual(statuses["escape"]["status"], "skipped")
            self.assertEqual(statuses["escape"]["reason"], "path-escape")
            self.assertEqual(statuses["p1"]["status"], "applied")
            self.assertFalse((live / "old.md").exists())
            self.assertFalse((harness.root / "escape.md").exists())

    def test_skips_changed_source_but_applies_rest(self):
        # A source edited after drafting is skipped; the rest of the batch applies.
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            live = self._standard_project(harness)
            (live / "extra.md").write_text(note("extra", body="Another note."), encoding="utf-8", newline="\n")
            harness.mirror_sync("proj")
            harness.add_proposal(
                {
                    "id": "stale",
                    "project": "proj",
                    "action": "compress",
                    "sources": [{"path": "old.md", "digest": "0" * 64}],  # digest will not match
                    "results": [{"path": "old.md", "content": note("old", body="compressed")}],
                    "deletes": [],
                    "justification": "compress",
                    "sensitive": False,
                }
            )
            harness.add_proposal(
                {
                    "id": "good",
                    "project": "proj",
                    "action": "compress",
                    "sources": [{"path": "extra.md", "digest": harness.digest("proj", "extra.md")}],
                    "results": [{"path": "extra.md", "content": note("extra", body="compressed extra")}],
                    "deletes": [],
                    "justification": "compress",
                    "sensitive": False,
                }
            )
            harness.write()
            result = harness.run(harness.selection(["stale", "good"]))
            self.assertEqual(result.returncode, 0)
            manifest = json.loads((harness.patch_set / "apply-manifest.json").read_text(encoding="utf-8"))
            statuses = {s["id"]: s for entry in manifest["projects"] for s in entry["proposals"]}
            self.assertEqual(statuses["stale"]["status"], "skipped")
            self.assertEqual(statuses["stale"]["reason"], "source-changed-since-draft")
            self.assertEqual(statuses["good"]["status"], "applied")
            self.assertIn("compressed extra", (live / "extra.md").read_text(encoding="utf-8"))
            self.assertNotIn("compressed", (live / "old.md").read_text(encoding="utf-8"))

    def test_sensitive_proposal_refused_even_when_selected(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            live = self._standard_project(harness)
            harness.add_proposal(
                {
                    "id": "sens",
                    "project": "proj",
                    "action": "compress",
                    "sources": [{"path": "new.md", "digest": harness.digest("proj", "new.md")}],
                    "results": [{"path": "new.md", "content": note("new", body="rewritten")}],
                    "deletes": [],
                    "justification": "compress",
                    "sensitive": True,
                }
            )
            harness.write()
            result = harness.run(harness.selection(["sens"]))
            self.assertEqual(result.returncode, 0)
            manifest = json.loads((harness.patch_set / "apply-manifest.json").read_text(encoding="utf-8"))
            statuses = {s["id"]: s for entry in manifest["projects"] for s in entry["proposals"]}
            self.assertEqual(statuses["sens"]["status"], "skipped")
            self.assertEqual(statuses["sens"]["reason"], "sensitive")
            self.assertNotIn("rewritten", (live / "new.md").read_text(encoding="utf-8"))

    def test_vanished_project_is_skipped_not_aborted(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            self._standard_project(harness, "proj")
            self._close_old_proposal(harness, "proj")
            # A proposal targeting a project that does not exist in live.
            harness.add_proposal(
                {
                    "id": "ghost",
                    "project": "ghost-project",
                    "action": "compress",
                    "sources": [],
                    "results": [{"path": "x.md", "content": "x"}],
                    "deletes": [],
                    "justification": "ghost",
                    "sensitive": False,
                }
            )
            harness.write()
            result = harness.run(harness.selection(["p1", "ghost"]))
            self.assertEqual(result.returncode, 0)
            manifest = json.loads((harness.patch_set / "apply-manifest.json").read_text(encoding="utf-8"))
            statuses = {s["id"]: s for entry in manifest["projects"] for s in entry["proposals"]}
            self.assertEqual(statuses["ghost"]["status"], "skipped")
            self.assertEqual(statuses["ghost"]["reason"], "project-vanished")
            self.assertEqual(statuses["p1"]["status"], "applied")

    def test_second_invocation_refuses_on_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            self._standard_project(harness)
            self._close_old_proposal(harness)
            harness.write()
            lock_path = harness.root / "held.lock"
            # Hold the lock through compat.FileLock itself so the conflict is
            # exercised with the platform's real primitive (flock / msvcrt).
            with FileLock(lock_path):
                result = harness.run(harness.selection(["p1"]), "--lock", str(lock_path))
                self.assertEqual(result.returncode, 1)
                # compat.FileLock's message ("lock <path> is held by another
                # process"), not the pre-extraction script's own wording.
                self.assertIn("is held by another process", result.stderr)
                self.assertTrue((harness.live_root / "proj" / "memory" / "old.md").exists())

    def test_partial_failure_leaves_project_untouched_and_records_split(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            good = self._standard_project(harness, "good")
            self._close_old_proposal(harness, "good")
            bad = self._standard_project(harness, "bad")
            # A path component that exists as a FILE makes staging raise before any
            # rename, so the bad project stays untouched (temp-rename discipline).
            (bad / "sub").write_text("i am a file, not a dir", encoding="utf-8", newline="\n")
            harness.mirror_sync("bad")
            harness.add_proposal(
                {
                    "id": "bad1",
                    "project": "bad",
                    "action": "compress",
                    "sources": [{"path": "new.md", "digest": harness.digest("bad", "new.md")}],
                    "results": [{"path": "sub/note.md", "content": "will not land"}],
                    "deletes": [],
                    "justification": "compress",
                    "sensitive": False,
                }
            )
            harness.write()
            result = harness.run(harness.selection(["p1", "bad1"]))
            self.assertEqual(result.returncode, 0)
            manifest = json.loads((harness.patch_set / "apply-manifest.json").read_text(encoding="utf-8"))
            statuses = {s["id"]: s for entry in manifest["projects"] for s in entry["proposals"]}
            self.assertEqual(statuses["p1"]["status"], "applied")
            self.assertEqual(statuses["bad1"]["status"], "failed")
            # good project applied, bad project untouched.
            self.assertFalse((good / "old.md").exists())
            self.assertTrue((bad / "old.md").exists())
            self.assertFalse((bad / "sub" / "note.md").exists())

    def _close_proposal(self, harness, pid, key, deleted, survivor_path, survivor_name):
        harness.add_proposal(
            {
                "id": pid,
                "project": key,
                "action": "period-close",
                "survivor": survivor_path,
                "sources": [{"path": deleted, "digest": harness.digest(key, deleted)}],
                "results": [{"path": survivor_path, "content": note(survivor_name, body="Consolidated truth.")}],
                "deletes": [deleted],
                "justification": "superseded",
                "sensitive": False,
            }
        )

    def test_retargets_bystander_at_apply(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            live = harness.project("proj")
            (live / "MEMORY.md").write_text("- [Old](old.md)\n- [Linker](linker.md)\n", encoding="utf-8", newline="\n")
            (live / "old.md").write_text(note("old", body="Old fact."), encoding="utf-8", newline="\n")
            (live / "linker.md").write_text(note("linker", body="See [[old]] for history."), encoding="utf-8", newline="\n")
            harness.mirror_sync("proj")
            self._close_proposal(harness, "p1", "proj", "old.md", "new.md", "new")
            harness.write()
            self.assertEqual(harness.run(harness.selection(["p1"])).returncode, 0)
            self.assertFalse((live / "old.md").exists())
            self.assertTrue((live / "new.md").exists())
            self.assertIn("[[new]]", (live / "linker.md").read_text(encoding="utf-8"))
            self.assertNotIn("[[old]]", (live / "linker.md").read_text(encoding="utf-8"))

    def test_two_closes_sharing_bystander_no_corruption(self):
        # Regression coverage: two proposals close notes a third note links to.
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            live = harness.project("proj")
            (live / "MEMORY.md").write_text("- [O1](old1.md)\n- [O2](old2.md)\n- [X](x.md)\n", encoding="utf-8", newline="\n")
            (live / "old1.md").write_text(note("old1", body="First."), encoding="utf-8", newline="\n")
            (live / "old2.md").write_text(note("old2", body="Second."), encoding="utf-8", newline="\n")
            (live / "x.md").write_text(note("x", body="Links [[old1]] and [[old2]]."), encoding="utf-8", newline="\n")
            harness.mirror_sync("proj")
            self._close_proposal(harness, "p1", "proj", "old1.md", "new1.md", "new1")
            self._close_proposal(harness, "p2", "proj", "old2.md", "new2.md", "new2")
            harness.write()
            result = harness.run(harness.selection(["p1", "p2"]))
            self.assertEqual(result.returncode, 0)
            statuses = {
                s["id"]: s["status"]
                for entry in json.loads((harness.patch_set / "apply-manifest.json").read_text(encoding="utf-8"))["projects"]
                for s in entry["proposals"]
            }
            self.assertEqual(statuses, {"p1": "applied", "p2": "applied"})
            body = (live / "x.md").read_text(encoding="utf-8")
            self.assertIn("[[new1]]", body)
            self.assertIn("[[new2]]", body)
            self.assertNotIn("[[old1]]", body)
            self.assertNotIn("[[old2]]", body)

    def test_exclusion_leaves_no_dangling_link(self):
        # Approving only one of two shared-bystander closes must keep the excluded
        # note's link intact (its note is not deleted), never dangle to a non-survivor.
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            live = harness.project("proj")
            (live / "MEMORY.md").write_text("- [O1](old1.md)\n- [O2](old2.md)\n- [X](x.md)\n", encoding="utf-8", newline="\n")
            (live / "old1.md").write_text(note("old1", body="First."), encoding="utf-8", newline="\n")
            (live / "old2.md").write_text(note("old2", body="Second."), encoding="utf-8", newline="\n")
            (live / "x.md").write_text(note("x", body="Links [[old1]] and [[old2]]."), encoding="utf-8", newline="\n")
            harness.mirror_sync("proj")
            self._close_proposal(harness, "p1", "proj", "old1.md", "new1.md", "new1")
            self._close_proposal(harness, "p2", "proj", "old2.md", "new2.md", "new2")
            harness.write()
            self.assertEqual(harness.run(harness.selection(["p1"])).returncode, 0)  # exclude p2
            body = (live / "x.md").read_text(encoding="utf-8")
            self.assertIn("[[new1]]", body)
            self.assertIn("[[old2]]", body)  # old2 kept, not retargeted
            self.assertTrue((live / "old2.md").exists())
            self.assertFalse((live / "old1.md").exists())

    def test_refuses_patch_set_id_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            self._standard_project(harness)
            self._close_old_proposal(harness)
            harness.write()
            selection = harness.selection(["p1"], patch_set_id="a-different-patch-set")
            result = harness.run(selection)
            self.assertEqual(result.returncode, 1)
            self.assertIn("patch_set_id does not match", result.stderr)
            self.assertTrue((harness.live_root / "proj" / "memory" / "old.md").exists())

    def test_symlinked_project_skipped_never_written_through(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            # Build a normal project, then replace its memory dir with a symlink to
            # an outside tree that has matching content (so freshness would pass).
            live = self._standard_project(harness, "proj")
            outside = harness.root / "outside"
            outside.mkdir()
            for path in live.iterdir():
                (outside / path.name).write_bytes(path.read_bytes())
            shutil.rmtree(live)
            (harness.live_root / "proj" / "memory").symlink_to(outside)
            self._close_old_proposal(harness, "proj")
            harness.write()
            result = harness.run(harness.selection(["p1"]))
            self.assertEqual(result.returncode, 0)
            statuses = {
                s["id"]: s["reason"]
                for entry in json.loads((harness.patch_set / "apply-manifest.json").read_text(encoding="utf-8"))["projects"]
                for s in entry["proposals"]
            }
            self.assertEqual(statuses["p1"], "live-symlink")
            self.assertTrue((outside / "old.md").exists())  # never written through

    def _statuses(self, harness):
        manifest = json.loads((harness.patch_set / "apply-manifest.json").read_text(encoding="utf-8"))
        return {s["id"]: s for entry in manifest["projects"] for s in entry["proposals"]}

    def test_conflicting_survivor_across_proposals_skipped(self):
        # One proposal's survivor is another's delete target: applying both would
        # write-then-delete the survivor (data loss). Both must be skipped.
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            live = harness.project("proj")
            (live / "MEMORY.md").write_text("- [A](a.md)\n- [S](surv.md)\n", encoding="utf-8", newline="\n")
            (live / "a.md").write_text(note("a", body="Alpha."), encoding="utf-8", newline="\n")
            (live / "surv.md").write_text(note("surv", body="Survivor."), encoding="utf-8", newline="\n")
            harness.mirror_sync("proj")
            harness.add_proposal({
                "id": "p1", "project": "proj", "action": "period-close", "survivor": "surv.md",
                # The assembler always digests an existing survivor as a source; the
                # destination-appeared guard relies on that invariant.
                "sources": [
                    {"path": "a.md", "digest": harness.digest("proj", "a.md")},
                    {"path": "surv.md", "digest": harness.digest("proj", "surv.md")},
                ],
                "results": [{"path": "surv.md", "content": note("surv", body="Rewritten.")}],
                "deletes": ["a.md"], "justification": "x", "sensitive": False,
            })
            harness.add_proposal({
                "id": "p2", "project": "proj", "action": "period-close", "survivor": "b2.md",
                "sources": [{"path": "surv.md", "digest": harness.digest("proj", "surv.md")}],
                "results": [{"path": "b2.md", "content": note("b2", body="New.")}],
                "deletes": ["surv.md"], "justification": "x", "sensitive": False,
            })
            harness.write()
            self.assertEqual(harness.run(harness.selection(["p1", "p2"])).returncode, 0)
            statuses = self._statuses(harness)
            self.assertEqual(statuses["p1"]["reason"], "conflicting-survivor")
            self.assertEqual(statuses["p2"]["reason"], "conflicting-survivor")
            self.assertTrue((live / "a.md").exists())  # nothing lost
            self.assertIn("Survivor.", (live / "surv.md").read_text(encoding="utf-8"))  # unchanged
            self.assertFalse((live / "b2.md").exists())

    def test_refuses_approval_without_token(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp), approval="looks good, go ahead")  # lacks the manifest id
            self._standard_project(harness)
            self._close_old_proposal(harness)
            harness.write()
            result = harness.run(harness.selection(["p1"]))
            self.assertEqual(result.returncode, 1)
            self.assertIn("approval token", result.stderr)
            self.assertTrue((harness.live_root / "proj" / "memory" / "old.md").exists())

    def test_synthetic_turn_not_accepted_as_operator_message(self):
        for synthetic in ("<bash-input>rm -rf x ps1</bash-input>", "[Request interrupted by user]"):
            entry = {"type": "user", "message": {"role": "user", "content": synthetic}}
            self.assertIsNone(transcript.extract_user_text(entry))
        # A genuine operator message is still returned.
        entry = {"type": "user", "message": {"role": "user", "content": "approve all ps1"}}
        self.assertEqual(transcript.extract_user_text(entry), "approve all ps1")

    def test_refuses_duplicate_proposal_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            self._standard_project(harness)
            harness.add_proposal({"id": "dup", "project": "proj", "action": "compress", "survivor": "new.md",
                                  "sources": [], "results": [{"path": "new.md", "content": note("new")}],
                                  "deletes": [], "justification": "x", "sensitive": False})
            harness.add_proposal({"id": "dup", "project": "proj", "action": "compress", "survivor": "evil.md",
                                  "sources": [], "results": [{"path": "evil.md", "content": note("evil")}],
                                  "deletes": [], "justification": "x", "sensitive": False})
            harness.write()
            result = harness.run(harness.selection(["dup"]))
            self.assertEqual(result.returncode, 1)
            self.assertIn("duplicate proposal ids", result.stderr)

    def test_refuses_missing_created_at_line(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            self._standard_project(harness)
            self._close_old_proposal(harness)
            harness.write()
            manifest = json.loads((harness.patch_set / "manifest.json").read_text(encoding="utf-8"))
            del manifest["created_at_line"]
            (harness.patch_set / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
            result = harness.run(harness.selection(["p1"]))
            self.assertEqual(result.returncode, 1)
            self.assertIn("missing created_at_line", result.stderr)

    def test_malformed_proposal_skipped_batch_continues(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            live = self._standard_project(harness)
            self._close_old_proposal(harness)  # p1, valid
            # results of a wrong type must skip cleanly, never crash the batch mid-apply.
            harness.add_proposal({"id": "bad", "project": "proj", "action": "compress",
                                  "survivor": "bad.md", "sources": [], "results": "not-a-list",
                                  "deletes": [], "justification": "x", "sensitive": False})
            harness.write()
            self.assertEqual(harness.run(harness.selection(["p1", "bad"])).returncode, 0)
            statuses = self._statuses(harness)
            self.assertEqual(statuses["bad"]["reason"], "malformed-proposal")
            self.assertEqual(statuses["p1"]["status"], "applied")
            self.assertFalse((live / "old.md").exists())

    def test_non_md_survivor_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            self._standard_project(harness)
            harness.add_proposal({"id": "p1", "project": "proj", "action": "compress", "survivor": "new.txt",
                                  "sources": [], "results": [{"path": "new.txt", "content": "x"}],
                                  "deletes": [], "justification": "x", "sensitive": False})
            harness.write()
            self.assertEqual(harness.run(harness.selection(["p1"])).returncode, 0)
            self.assertEqual(self._statuses(harness)["p1"]["reason"], "path-escape")

    def test_refuses_content_tampered_manifest(self):
        # Editing a proposal after approval (id kept) must be caught: apply recomputes
        # the content-bound id from the proposals and refuses on mismatch.
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            live = self._standard_project(harness)
            self._close_old_proposal(harness)
            harness.write()
            manifest = json.loads((harness.patch_set / "manifest.json").read_text(encoding="utf-8"))
            manifest["proposals"][0]["justification"] = "TAMPERED after approval"
            (harness.patch_set / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
            result = harness.run(harness.selection(["p1"]))
            self.assertEqual(result.returncode, 1)
            self.assertIn("does not match its proposal content", result.stderr)
            self.assertTrue((live / "old.md").exists())

    def test_apply_manifest_lists_deletions_for_mirror_pruning(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            self._standard_project(harness)
            self._close_proposal(harness, "p1", "proj", "old.md", "new2.md", "new2")
            harness.write()
            self.assertEqual(harness.run(harness.selection(["p1"])).returncode, 0)
            manifest = json.loads((harness.patch_set / "apply-manifest.json").read_text(encoding="utf-8"))
            self.assertIn({"project": "proj", "path": "old.md"}, manifest["deleted"])

    def test_stage_and_commit_rolls_back_on_commit_failure(self):
        # Force a commit-phase failure after the first rename lands and assert the
        # project is fully restored: overwrite reverted, new file removed, temps gone.
        from unittest import mock

        with tempfile.TemporaryDirectory() as temp:
            memory = Path(temp) / "memory"
            memory.mkdir()
            existing = memory / "a.md"
            existing.write_text("original A", encoding="utf-8", newline="\n")
            fresh = memory / "b.md"
            real_replace = apply_mod.os.replace
            calls = {"n": 0}

            def flaky_replace(src, dst):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("simulated commit failure")
                return real_replace(src, dst)

            with mock.patch.object(apply_mod.os, "replace", flaky_replace):
                with self.assertRaises(apply_mod.RolledBack):
                    apply_mod.stage_and_commit({existing: "rewritten A", fresh: "new B"}, [])
            self.assertEqual(existing.read_text(encoding="utf-8"), "original A")  # overwrite rolled back
            self.assertFalse(fresh.exists())  # newly-created file removed
            self.assertFalse((memory / "a.md.dream-tmp").exists())
            self.assertFalse((memory / "b.md.dream-tmp").exists())

    def test_completion_line_is_machine_parseable(self):
        # Snapshot mode (no --mirror-root): the default MIRROR_PUSH_HINT is a
        # multi-word human hint (see skipped_or_changed / MODULE BUG note), which
        # would break the naive whitespace-tokenized parse this test exercises.
        # Snapshot mode keeps the "next" field a single safe token ("none") and
        # preserves this test's actual intent: the line format itself.
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            self._standard_project(harness)
            self._close_old_proposal(harness)
            harness.write()
            result = harness.run(harness.selection(["p1"]), mirror=False)
            line = [line for line in result.stdout.splitlines() if line.startswith("DREAM-APPLY-COMPLETE")]
            self.assertEqual(len(line), 1)
            fields = dict(token.split("=", 1) for token in line[0].split()[1:])
            self.assertEqual(fields["applied"], "1")
            self.assertEqual(fields["next"], "none")

    def test_active_session_warned_and_preflight_writes_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            live = self._standard_project(harness)
            self._close_old_proposal(harness)
            harness.write()
            # A sibling transcript touched "now" simulates another active session.
            other = harness.live_root / "proj" / "other-session.jsonl"
            other.write_text("{}", encoding="utf-8", newline="\n")
            selection = harness.selection(["p1"])
            preflight = harness.run(selection, "--preflight", "--now-ts", str(other.stat().st_mtime))
            self.assertEqual(preflight.returncode, 0)
            self.assertIn("other Claude session appears active", preflight.stderr)
            self.assertIn("proj/other-session.jsonl", preflight.stderr)
            # Preflight writes nothing.
            self.assertTrue((live / "old.md").exists())
            self.assertFalse((harness.patch_set / "apply-manifest.json").exists())
            # A real apply proceeds despite the warning (warn-only, never blocks).
            apply_result = harness.run(selection, "--now-ts", str(other.stat().st_mtime))
            self.assertEqual(apply_result.returncode, 0)
            self.assertIn("other Claude session appears active", apply_result.stderr)
            self.assertFalse((live / "old.md").exists())


class SplitApplyTests(unittest.TestCase):
    """Multi-result proposals (split) through the apply safety stack."""

    _statuses = MemoryDreamApplyTests._statuses

    def _split_proposal(self, harness, pid="p1", key="proj"):
        survivor = (
            "---\nname: mega\ndescription: core topic after the split rewrite\nmetadata:\n  type: project\n---\n"
            "Core topic. See [[gotcha]].\n"
        )
        extract = (
            "---\nname: gotcha\ndescription: reusable operational gotcha worth recalling\nmetadata:\n  type: reference\n---\n"
            "The gotcha.\n"
        )
        harness.add_proposal(
            {
                "id": pid,
                "project": key,
                "action": "split",
                "survivor": "mega.md",
                "sources": [{"path": "mega.md", "digest": harness.digest(key, "mega.md")}],
                "results": [
                    {"path": "mega.md", "content": survivor},
                    {"path": "gotcha.md", "content": extract},
                ],
                "deletes": [],
                "justification": "split the mega-note",
                "sensitive": False,
            }
        )

    def _mega_project(self, harness, key="proj"):
        live = harness.project(key)
        # The index line matches the note's canonical entry, so the conservative
        # refresh (old-entry match) applies to it.
        (live / "MEMORY.md").write_text("- [mega](mega.md): Fixture note describing mega in detail\n", encoding="utf-8", newline="\n")
        (live / "mega.md").write_text(note("mega", body="Topic one. Topic two."), encoding="utf-8", newline="\n")
        harness.mirror_sync(key)
        return live

    def test_split_writes_extracts_and_refreshes_index(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            live = self._mega_project(harness)
            # A pre-existing unindexed bystander: healing it into the index is a
            # separately gated step (fix mode), never a consolidation side effect.
            (live / "unindexed_bystander.md").write_text(note("unindexed_bystander", body="Quiet."), encoding="utf-8", newline="\n")
            harness.mirror_sync("proj")
            self._split_proposal(harness)
            harness.write()
            self.assertEqual(harness.run(harness.selection(["p1"])).returncode, 0)
            self.assertEqual(self._statuses(harness)["p1"]["status"], "applied")
            self.assertIn("The gotcha.", (live / "gotcha.md").read_text(encoding="utf-8"))
            index = (live / "MEMORY.md").read_text(encoding="utf-8")
            # New atomic note indexed; the split note's entry line refreshed to the
            # new routing hook (recall is description-routed).
            self.assertIn("- [gotcha](gotcha.md): reusable operational gotcha worth recalling", index)
            self.assertIn("- [mega](mega.md): core topic after the split rewrite", index)
            self.assertNotIn("Fixture note describing mega in detail", index)
            self.assertNotIn("unindexed_bystander", index)  # healing stays unbundled

    def test_destination_appeared_since_draft_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            live = self._mega_project(harness)
            self._split_proposal(harness)
            # The extract's destination appears between build and apply: applying
            # would silently overwrite a file the digest gate never covered.
            (live / "gotcha.md").write_text(note("gotcha", body="Appeared meanwhile."), encoding="utf-8", newline="\n")
            harness.mirror_sync("proj")
            harness.write()
            self.assertEqual(harness.run(harness.selection(["p1"])).returncode, 0)
            status = self._statuses(harness)["p1"]
            self.assertEqual(status["status"], "skipped")
            self.assertEqual(status["reason"], "destination-appeared-since-draft")
            self.assertIn("Appeared meanwhile.", (live / "gotcha.md").read_text(encoding="utf-8"))
            self.assertIn("Topic one.", (live / "mega.md").read_text(encoding="utf-8"))  # untouched

    def test_conflicting_extract_writes_both_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            live = harness.project("proj")
            (live / "MEMORY.md").write_text("- [A](a.md)\n- [B](b.md)\n", encoding="utf-8", newline="\n")
            (live / "a.md").write_text(note("a", body="Alpha."), encoding="utf-8", newline="\n")
            (live / "b.md").write_text(note("b", body="Beta."), encoding="utf-8", newline="\n")
            harness.mirror_sync("proj")
            shared = {"path": "shared.md", "content": note("shared", body="Contested.")}
            for pid, rel in (("p1", "a.md"), ("p2", "b.md")):
                harness.add_proposal(
                    {
                        "id": pid,
                        "project": "proj",
                        "action": "split",
                        "survivor": rel,
                        "sources": [{"path": rel, "digest": harness.digest("proj", rel)}],
                        "results": [
                            {"path": rel, "content": note(rel[:-3], body=f"Rewritten {rel}. See [[shared]].")},
                            dict(shared),
                        ],
                        "deletes": [],
                        "justification": "x",
                        "sensitive": False,
                    }
                )
            harness.write()
            self.assertEqual(harness.run(harness.selection(["p1", "p2"])).returncode, 0)
            statuses = self._statuses(harness)
            self.assertEqual(statuses["p1"]["reason"], "conflicting-survivor")
            self.assertEqual(statuses["p2"]["reason"], "conflicting-survivor")
            self.assertFalse((live / "shared.md").exists())
            self.assertIn("Alpha.", (live / "a.md").read_text(encoding="utf-8"))  # untouched

    def test_index_error_never_masks_a_committed_apply(self):
        # Index maintenance failing after the note commit must not report the
        # proposals as failed or drop the deleted[] list the mirror-prune needs.
        with tempfile.TemporaryDirectory() as temp:
            memory_dir = Path(temp) / "proj" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "MEMORY.md").write_text("- [old](old.md)\n- [new](new.md)\n", encoding="utf-8", newline="\n")
            (memory_dir / "old.md").write_text(note("old", body="SUPERSEDED: see new."), encoding="utf-8", newline="\n")
            (memory_dir / "new.md").write_text(note("new", body="Current truth."), encoding="utf-8", newline="\n")
            proposal = {
                "id": "p1", "project": "proj", "action": "period-close", "survivor": "new.md",
                "sources": [
                    {"path": "old.md", "digest": audit.digest(memory_dir / "old.md")},
                    {"path": "new.md", "digest": audit.digest(memory_dir / "new.md")},
                ],
                "results": [{"path": "new.md", "content": note("new", body="Closed truth.")}],
                "deletes": ["old.md"], "justification": "x", "sensitive": False,
            }

            def boom(*_args, **_kwargs):
                raise OSError("disk full")

            # AUDIT is the shared memory_dream.audit module (apply.py does
            # `from memory_dream import audit as AUDIT`), so patching either name
            # mutates the same function object; restore it in finally.
            original = apply_mod.AUDIT.compute_index_reconcile
            apply_mod.AUDIT.compute_index_reconcile = boom
            try:
                result = apply_mod.apply_project("proj", memory_dir, [proposal])
            finally:
                apply_mod.AUDIT.compute_index_reconcile = original
            self.assertTrue(result["committed"])
            self.assertEqual(result["proposals"][0]["status"], "applied")
            self.assertIn("disk full", result["index_error"])
            self.assertEqual(result["deleted"], ["old.md"])
            self.assertIn("Closed truth.", (memory_dir / "new.md").read_text(encoding="utf-8"))
            self.assertFalse((memory_dir / "old.md").exists())

    def test_split_preserves_hand_annotated_index_line(self):
        # A non-redescribe rewrite must not clobber a hand-annotated index line.
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            live = self._mega_project(harness)
            annotated = "- [mega](mega.md): Fixture note describing mega in detail; NOTE see runbook\n"
            (live / "MEMORY.md").write_text(annotated, encoding="utf-8", newline="\n")
            harness.mirror_sync("proj")
            self._split_proposal(harness)
            harness.write()
            self.assertEqual(harness.run(harness.selection(["p1"])).returncode, 0)
            index = (live / "MEMORY.md").read_text(encoding="utf-8")
            self.assertIn(annotated.strip(), index)  # hand-edited line preserved
            self.assertIn("- [gotcha](gotcha.md):", index)  # extract still appended

    def test_redescribe_apply_refreshes_both_surfaces(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            live = harness.project("proj")
            (live / "MEMORY.md").write_text("- [a](a.md): stale index hook\n", encoding="utf-8", newline="\n")
            (live / "a.md").write_text(note("a", body="Body stays."), encoding="utf-8", newline="\n")
            harness.mirror_sync("proj")
            redescribed = note("a", body="Body stays.").replace(
                "description: Fixture note describing a in detail",
                "description: fresh routing hook after the correction",
            )
            harness.add_proposal(
                {
                    "id": "p1",
                    "project": "proj",
                    "action": "redescribe",
                    "survivor": "a.md",
                    "sources": [{"path": "a.md", "digest": harness.digest("proj", "a.md")}],
                    "results": [{"path": "a.md", "content": redescribed}],
                    "deletes": [],
                    "justification": "stale hook",
                    "sensitive": False,
                }
            )
            harness.write()
            self.assertEqual(harness.run(harness.selection(["p1"])).returncode, 0)
            self.assertEqual(self._statuses(harness)["p1"]["status"], "applied")
            self.assertIn("fresh routing hook after the correction", (live / "a.md").read_text(encoding="utf-8"))
            # The index line and the note frontmatter can no longer diverge.
            self.assertIn("- [a](a.md): fresh routing hook after the correction", (live / "MEMORY.md").read_text(encoding="utf-8"))
            self.assertNotIn("stale index hook", (live / "MEMORY.md").read_text(encoding="utf-8"))


class ApplySnapshotAndArchiveSecurityTests(unittest.TestCase):
    """Regression coverage for the gates hardened after the adversarial review:
    snapshot/restore path confinement, backup merge on a resumed apply,
    fail-closed restore drift, and the archive loop's sensitive-note skip."""

    def _statuses(self, harness):
        manifest = json.loads((harness.patch_set / "apply-manifest.json").read_text(encoding="utf-8"))
        return {s["id"]: s for entry in manifest["projects"] for s in entry["proposals"]}

    def _restore(self, harness, *extra):
        command = [
            sys.executable, "-m", "memory_dream", "restore",
            "--patch-set", str(harness.patch_set),
            "--live-root", str(harness.live_root),
            *extra,
        ]
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            cwd=REPO_ROOT,
            env=_clean_env(harness.claude_config_dir),
        )

    def _standard_project(self, harness, key="proj"):
        live = harness.project(key)
        (live / "MEMORY.md").write_text("- [Old](old.md)\n- [New](new.md)\n", encoding="utf-8", newline="\n")
        (live / "old.md").write_text(note("old", body="SUPERSEDED: see new."), encoding="utf-8", newline="\n")
        (live / "new.md").write_text(note("new", body="Current truth."), encoding="utf-8", newline="\n")
        harness.mirror_sync(key)
        return live

    def test_snapshot_never_copies_an_out_of_bounds_path(self):
        # The snapshot copies files that already exist before the first live
        # write, so a delete path escaping the project's memory dir would be a
        # read-arbitrary-file-into-a-traversed-backup-path primitive running
        # BEFORE the per-proposal apply gate. It must be dropped at snapshot; the
        # escaping proposal is skipped path-escape and the rest of the batch still
        # applies.
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            self._standard_project(harness)
            secret = harness.root / "outside_secret.md"
            secret.write_text("SECRET-CONTENT\n", encoding="utf-8", newline="\n")
            harness.add_proposal({
                "id": "p1", "project": "proj", "action": "period-close",
                "sources": [{"path": "old.md", "digest": harness.digest("proj", "old.md")}],
                "results": [], "deletes": ["old.md"],
                "justification": "superseded", "sensitive": False,
            })
            harness.add_proposal({
                "id": "escape", "project": "proj", "action": "period-close",
                "sources": [], "results": [], "deletes": ["../../../outside_secret.md"],
                "justification": "malicious", "sensitive": False,
            })
            harness.write()
            result = harness.run(harness.selection(["p1", "escape"]))
            self.assertEqual(result.returncode, 0, result.stderr)
            statuses = self._statuses(harness)
            self.assertEqual(statuses["escape"]["reason"], "path-escape")
            self.assertEqual(statuses["p1"]["status"], "applied")
            # The outside file was neither deleted nor copied into the backup.
            self.assertTrue(secret.is_file())
            self.assertEqual(secret.read_text(encoding="utf-8"), "SECRET-CONTENT\n")
            backup_root = harness.patch_set / "backup"
            for path in backup_root.rglob("*"):
                if path.is_file():
                    self.assertNotIn("SECRET-CONTENT", path.read_text(encoding="utf-8", errors="replace"))
            entries = json.loads((backup_root / "backup-manifest.json").read_text(encoding="utf-8"))["entries"]
            self.assertTrue(all(".." not in entry["path"] for entry in entries))
            self.assertFalse(any(entry["path"].endswith("outside_secret.md") for entry in entries))

    def test_restore_refuses_out_of_bounds_backup_path(self):
        # A hand-edited backup-manifest.json path that escapes the memory dir must
        # be refused at restore (the backup dir has only POSIX-owner protection),
        # never writing or deleting outside the project. Same gate the snapshot used.
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            live = self._standard_project(harness)
            original = (live / "new.md").read_text(encoding="utf-8")
            harness.add_proposal({
                "id": "p1", "project": "proj", "action": "compress",
                "sources": [{"path": "new.md", "digest": harness.digest("proj", "new.md")}],
                "results": [{"path": "new.md", "content": note("new", body="V1 compressed")}],
                "deletes": [], "justification": "compress", "sensitive": False,
            })
            harness.write()
            self.assertEqual(harness.run(harness.selection(["p1"])).returncode, 0)
            self.assertIn("V1 compressed", (live / "new.md").read_text(encoding="utf-8"))
            bm_path = harness.patch_set / "backup" / "backup-manifest.json"
            bm = json.loads(bm_path.read_text(encoding="utf-8"))
            bm["entries"].append({"project": "proj", "path": "../../../evil.md", "sha256": "a" * 64})
            bm_path.write_text(json.dumps(bm), encoding="utf-8", newline="\n")
            result = self._restore(harness)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("refusing out-of-bounds backup path", result.stderr)
            self.assertFalse((harness.root / "evil.md").exists())
            # The legitimate entry still restored new.md to its pre-apply bytes.
            self.assertEqual((live / "new.md").read_text(encoding="utf-8"), original)

    def test_reapply_preserves_the_original_pre_first_apply_backup(self):
        # A resumed second apply on the same patch set (a wider approval) MERGES
        # into the existing backup; it must never overwrite a file's true
        # pre-first-apply bytes with the now post-first-apply live copy. Snapshot
        # mode so the live edit does not trip the mirror-freshness gate on pass 2.
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            live = self._standard_project(harness)
            (live / "extra.md").write_text(note("extra", body="Extra original"), encoding="utf-8", newline="\n")
            harness.mirror_sync("proj")
            new_original = (live / "new.md").read_text(encoding="utf-8")
            extra_original = (live / "extra.md").read_text(encoding="utf-8")
            harness.add_proposal({
                "id": "p1", "project": "proj", "action": "compress",
                "sources": [{"path": "new.md", "digest": harness.digest("proj", "new.md")}],
                "results": [{"path": "new.md", "content": note("new", body="V1 compressed")}],
                "deletes": [], "justification": "compress", "sensitive": False,
            })
            harness.add_proposal({
                "id": "p2", "project": "proj", "action": "compress",
                "sources": [{"path": "extra.md", "digest": harness.digest("proj", "extra.md")}],
                "results": [{"path": "extra.md", "content": note("extra", body="V2 compressed")}],
                "deletes": [], "justification": "compress", "sensitive": False,
            })
            harness.write()
            # Pass 1: approve only p1. The backup captures new.md at ORIGINAL bytes.
            self.assertEqual(harness.run(harness.selection(["p1"]), mirror=False).returncode, 0)
            backup_new = harness.patch_set / "backup" / "proj" / "new.md"
            self.assertEqual(backup_new.read_text(encoding="utf-8"), new_original)
            self.assertIn("V1 compressed", (live / "new.md").read_text(encoding="utf-8"))
            # Pass 2: approve p1 + p2. p1's source digest is now stale (skipped),
            # p2 applies, and the merge must NOT re-copy the (now V1) live new.md
            # over the original backup.
            second = harness.run(harness.selection(["p1", "p2"]), mirror=False)
            self.assertEqual(second.returncode, 0, second.stderr)
            statuses = self._statuses(harness)
            self.assertEqual(statuses["p1"]["reason"], "source-changed-since-draft")
            self.assertEqual(statuses["p2"]["status"], "applied")
            self.assertEqual(backup_new.read_text(encoding="utf-8"), new_original)  # preserved, not V1
            entries = {
                (entry["project"], entry["path"]): entry
                for entry in json.loads((harness.patch_set / "backup" / "backup-manifest.json").read_text(encoding="utf-8"))["entries"]
            }
            self.assertEqual(
                entries[("proj", "new.md")]["sha256"],
                hashlib.sha256(new_original.encode()).hexdigest(),
            )
            # Restore reverses BOTH files to the pre-first-apply state.
            self.assertEqual(self._restore(harness).returncode, 0)
            self.assertEqual((live / "new.md").read_text(encoding="utf-8"), new_original)
            self.assertEqual((live / "extra.md").read_text(encoding="utf-8"), extra_original)

    def test_restore_fails_closed_when_apply_manifest_missing(self):
        # If apply-manifest.json is gone (apply crashed after snapshot, or it was
        # deleted), the drift check cannot run — restore must refuse without
        # --force rather than silently overwrite unverifiable live content.
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            live = self._standard_project(harness)
            original = (live / "new.md").read_text(encoding="utf-8")
            harness.add_proposal({
                "id": "p1", "project": "proj", "action": "compress",
                "sources": [{"path": "new.md", "digest": harness.digest("proj", "new.md")}],
                "results": [{"path": "new.md", "content": note("new", body="V1 compressed")}],
                "deletes": [], "justification": "compress", "sensitive": False,
            })
            harness.write()
            self.assertEqual(harness.run(harness.selection(["p1"])).returncode, 0)
            (harness.patch_set / "apply-manifest.json").unlink()
            refused = self._restore(harness)
            self.assertEqual(refused.returncode, 1)
            self.assertIn("apply-manifest.json missing", refused.stderr)
            self.assertIn("--force", refused.stderr)
            self.assertIn("V1 compressed", (live / "new.md").read_text(encoding="utf-8"))  # not restored
            forced = self._restore(harness, "--force")
            self.assertEqual(forced.returncode, 0, forced.stderr)
            self.assertIn("proceeding over drift", forced.stderr)
            self.assertEqual((live / "new.md").read_text(encoding="utf-8"), original)

    def test_sensitive_archive_proposal_is_skipped(self):
        # The archive loop runs before the generic loop; a sensitive archive
        # proposal must not move entries into the (still-readable) archive index.
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            live = harness.project()
            (live / "old.md").write_text(note("old"), encoding="utf-8", newline="\n")
            (live / "MEMORY.md").write_text("# Index\n- [old](old.md) — resolved 2026-06-01 thing\n", encoding="utf-8", newline="\n")
            harness.mirror_sync()
            entry = "- [old](old.md) — resolved 2026-06-01 thing"
            harness.add_proposal({
                "id": "a1", "project": "proj", "action": "archive",
                "justification": "demote cold entry",
                "sources": [{"path": "MEMORY.md", "digest": harness.digest("proj", "MEMORY.md")}],
                "results": [], "deletes": [], "archive_entries": [entry],
                "sensitive": True, "survivor": None,
            })
            harness.write()
            result = harness.run(harness.selection(["a1"]))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self._statuses(harness)["a1"]["reason"], "sensitive")
            self.assertFalse((live / "MEMORY-archive.md").exists())
            self.assertIn(entry, (live / "MEMORY.md").read_text(encoding="utf-8"))  # index untouched


class ApplyCompactionCanaryPreflightTests(ApplyFixtureMixin, unittest.TestCase):
    """Apply's preflight runs the same compaction canary doctor runs, against
    the same CLAUDE_CONFIG_DIR/projects/<cwd-slug> transcripts directory
    (harness.run() always shells out with cwd=REPO_ROOT). Warn-only: it never
    changes apply's exit code, and it prints before the consent-trace check
    that follows it."""

    def _write_drift_transcripts_dir(self, harness):
        transcripts_dir = harness.claude_config_dir / "projects" / transcript.cwd_slug(REPO_ROOT)
        transcripts_dir.mkdir(parents=True)
        (transcripts_dir / "session.jsonl").write_text(
            json.dumps(
                {
                    "type": "user",
                    "isSummaryRenamedUpstream": True,
                    "message": {
                        "role": "user",
                        "content": (
                            "This session is being continued from a previous conversation "
                            "that ran out of context."
                        ),
                    },
                }
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def test_drift_warning_printed_but_does_not_change_success_exit_code(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            self._standard_project(harness)
            self._close_old_proposal(harness)
            harness.write()
            self._write_drift_transcripts_dir(harness)
            result = harness.run(harness.selection(["p1"]))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("consent-gate canary reports transcript schema drift", result.stderr)
            self.assertIn("memory-dream doctor", result.stderr)

    def test_drift_warning_precedes_consent_trace_refusal_and_exit_code_unchanged(self):
        # Same fixture as test_refuses_missing_and_mismatched_trace (no
        # operator_trace at all) but with the drift fixture layered on top:
        # the refusal reason and exit code must be identical to the
        # no-canary case, and the warning must appear before it in stderr.
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            self._standard_project(harness)
            self._close_old_proposal(harness)
            harness.write()
            self._write_drift_transcripts_dir(harness)
            no_trace = harness.selection(["p1"], trace={}, path_name="no_trace.json")
            result = harness.run(no_trace)
            self.assertEqual(result.returncode, 1)
            self.assertIn("operator trace invalid", result.stderr)
            warning_at = result.stderr.index("consent-gate canary reports transcript schema drift")
            refusal_at = result.stderr.index("operator trace invalid")
            self.assertLess(warning_at, refusal_at, result.stderr)

    def test_no_drift_fixture_prints_no_canary_warning(self):
        # Baseline: with no CLAUDE_CONFIG_DIR/projects/<slug> directory at
        # all (the default for every other apply test in this file), the
        # canary is "unverified" and prints nothing.
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            self._standard_project(harness)
            self._close_old_proposal(harness)
            harness.write()
            result = harness.run(harness.selection(["p1"]))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("consent-gate canary", result.stderr)


class ApplyRejectionRecordingTests(ApplyFixtureMixin, unittest.TestCase):
    """rejections.json durably records which PRESENTED proposals the operator
    did not approve: a sibling of the dated patch-set directories
    under config.pass_root(), never inside one, so pruning old pass dirs
    (the retention advisory's suggested cleanup) can never erase it."""

    def _rejections_path(self, harness):
        return harness.claude_config_dir / "logs" / "memory-dream" / "passes" / "rejections.json"

    def test_rejected_proposal_recorded_with_union_of_source_and_result_paths(self):
        # 3 presented (p1, p2 approved; p3 not) -> exactly 1 rejection entry,
        # for p3, whose paths union its OWN sources[].path and results[].path
        # (p3's fake paths are never touched by apply -- rejected proposals
        # are excluded before apply_project ever runs, only used here).
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            self._standard_project(harness)
            self._close_old_proposal(harness)  # p1
            harness.add_proposal(
                {
                    "id": "p2",
                    "project": "proj",
                    "action": "compress",
                    "sources": [{"path": "new.md", "digest": harness.digest("proj", "new.md")}],
                    "results": [{"path": "new.md", "content": note("new", body="compressed")}],
                    "deletes": [],
                    "justification": "compress",
                    "sensitive": False,
                }
            )
            harness.add_proposal(
                {
                    "id": "p3",
                    "project": "proj",
                    "action": "merge",
                    "sources": [{"path": "a.md", "digest": "deadbeef"}],
                    "results": [{"path": "b.md", "content": "merged content"}],
                    "deletes": ["a.md"],
                    "justification": "merge",
                    "sensitive": False,
                }
            )
            harness.write()
            result = harness.run(harness.selection(["p1", "p2"]), "--now-ts", "1700000000")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(self._rejections_path(harness).read_text(encoding="utf-8"))
            self.assertEqual(len(payload["entries"]), 1)
            entry = payload["entries"][0]
            self.assertEqual(entry["proposal_id"], "p3")
            self.assertEqual(entry["project"], "proj")
            self.assertEqual(sorted(entry["paths"]), ["a.md", "b.md"])
            self.assertEqual(entry["patch_set_id"], harness.manifest_id)
            expected_recorded_at = dt.datetime.fromtimestamp(1700000000, tz=dt.timezone.utc).isoformat()
            self.assertEqual(entry["recorded_at"], expected_recorded_at)

    def test_all_approved_writes_no_rejections_file(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            self._standard_project(harness)
            self._close_old_proposal(harness)
            harness.write()
            result = harness.run(harness.selection(["p1"]))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(self._rejections_path(harness).exists())

    def test_two_applies_append_entries_in_order(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            self._standard_project(harness)
            self._close_old_proposal(harness)  # p1, approved
            harness.add_proposal(
                {
                    "id": "reject-1",
                    "project": "proj",
                    "action": "merge",
                    "sources": [{"path": "x.md", "digest": "aaa"}],
                    "results": [{"path": "y.md", "content": "..."}],
                    "deletes": ["x.md"],
                    "justification": "merge",
                    "sensitive": False,
                }
            )
            harness.write()
            result1 = harness.run(harness.selection(["p1"]), "--now-ts", "1700000000", mirror=False)
            self.assertEqual(result1.returncode, 0, result1.stderr)

            # A second, later pass: a fresh dated patch-set directory, same
            # CLAUDE_CONFIG_DIR (=> same config.pass_root()).
            harness.patch_set = harness.logs / "20260718-000000"
            harness.patch_set.mkdir(parents=True)
            harness._proposals = []
            harness.add_proposal(
                {
                    "id": "p2",
                    "project": "proj",
                    "action": "compress",
                    "sources": [{"path": "new.md", "digest": harness.digest("proj", "new.md")}],
                    "results": [{"path": "new.md", "content": note("new", body="v2")}],
                    "deletes": [],
                    "justification": "compress",
                    "sensitive": False,
                }
            )
            harness.add_proposal(
                {
                    "id": "reject-2",
                    "project": "proj",
                    "action": "merge",
                    "sources": [{"path": "m.md", "digest": "bbb"}],
                    "results": [{"path": "n.md", "content": "..."}],
                    "deletes": ["m.md"],
                    "justification": "merge",
                    "sensitive": False,
                }
            )
            harness.write()
            result2 = harness.run(harness.selection(["p2"]), "--now-ts", "1700003600", mirror=False)
            self.assertEqual(result2.returncode, 0, result2.stderr)

            payload = json.loads(self._rejections_path(harness).read_text(encoding="utf-8"))
            self.assertEqual([entry["proposal_id"] for entry in payload["entries"]], ["reject-1", "reject-2"])

    def test_deferred_and_manual_review_items_never_recorded_as_rejections(self):
        # report.json's deferred/manual_review lists (plan-level: per-pass cap
        # overflow, sensitive filenames) never became manifest proposals at
        # all -- they were never presented for approval, so they must never
        # surface in rejections.json even when it's alongside a real rejection.
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            self._standard_project(harness)
            self._close_old_proposal(harness)  # p1, approved
            harness.add_proposal(
                {
                    "id": "p2",
                    "project": "proj",
                    "action": "merge",
                    "sources": [{"path": "p2-source.md", "digest": "cafe"}],
                    "results": [{"path": "p2-result.md", "content": "..."}],
                    "deletes": ["p2-source.md"],
                    "justification": "merge",
                    "sensitive": False,
                }
            )
            harness.write()
            (harness.patch_set / "report.json").write_text(
                json.dumps(
                    {
                        "dropped": [],
                        "deferred": [{"project": "proj", "path": "deferred-note.md", "reason": "per-pass-cap"}],
                        "manual_review": [{"project": "proj", "path": "sensitive-note.md", "reason": "sensitive"}],
                    }
                ),
                encoding="utf-8",
                newline="\n",
            )
            result = harness.run(harness.selection(["p1"]))
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(self._rejections_path(harness).read_text(encoding="utf-8"))
            self.assertEqual([entry["proposal_id"] for entry in payload["entries"]], ["p2"])
            recorded_paths = {path for entry in payload["entries"] for path in entry["paths"]}
            self.assertNotIn("deferred-note.md", recorded_paths)
            self.assertNotIn("sensitive-note.md", recorded_paths)

    def test_corrupt_rejections_file_renamed_aside_apply_still_succeeds(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            self._standard_project(harness)
            self._close_old_proposal(harness)  # p1, approved
            harness.add_proposal(
                {
                    "id": "p2",
                    "project": "proj",
                    "action": "merge",
                    "sources": [{"path": "p2-source.md", "digest": "cafe"}],
                    "results": [{"path": "p2-result.md", "content": "..."}],
                    "deletes": ["p2-source.md"],
                    "justification": "merge",
                    "sensitive": False,
                }
            )
            harness.write()
            passes_dir = harness.claude_config_dir / "logs" / "memory-dream" / "passes"
            passes_dir.mkdir(parents=True)
            rejections_path = passes_dir / "rejections.json"
            rejections_path.write_text("{ not valid json at all", encoding="utf-8", newline="\n")

            result = harness.run(harness.selection(["p1"]))
            self.assertEqual(result.returncode, 0, result.stderr)

            corrupt_files = list(passes_dir.glob("rejections.json.corrupt-*"))
            self.assertEqual(len(corrupt_files), 1, corrupt_files)
            self.assertEqual(corrupt_files[0].read_text(encoding="utf-8"), "{ not valid json at all")
            payload = json.loads(rejections_path.read_text(encoding="utf-8"))
            self.assertEqual([entry["proposal_id"] for entry in payload["entries"]], ["p2"])

    def test_rejections_file_lands_under_pass_root_not_inside_patch_set(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = Harness(Path(temp))
            self._standard_project(harness)
            self._close_old_proposal(harness)  # p1, approved
            harness.add_proposal(
                {
                    "id": "p2",
                    "project": "proj",
                    "action": "merge",
                    "sources": [{"path": "p2-source.md", "digest": "cafe"}],
                    "results": [{"path": "p2-result.md", "content": "..."}],
                    "deletes": ["p2-source.md"],
                    "justification": "merge",
                    "sensitive": False,
                }
            )
            harness.write()
            result = harness.run(harness.selection(["p1"]))
            self.assertEqual(result.returncode, 0, result.stderr)
            pass_root = harness.claude_config_dir / "logs" / "memory-dream" / "passes"
            self.assertTrue((pass_root / "rejections.json").is_file())
            self.assertFalse((harness.patch_set / "rejections.json").exists())
            self.assertNotEqual(harness.patch_set, pass_root)


if __name__ == "__main__":
    unittest.main()
