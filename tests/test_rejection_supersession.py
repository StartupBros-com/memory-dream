"""A successful apply supersedes earlier rejection decisions, durably.

All notes, approvals, patch sets, and ledger writes here are synthetic fixtures.
"""

import contextlib
import datetime as dt
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from memory_dream import apply as apply_mod, audit, cli, config
from test_apply import Harness, _clean_env, note

REPO_ROOT = Path(__file__).resolve().parents[1]
T0 = dt.datetime(2026, 7, 10, 12, tzinfo=dt.timezone.utc)


def decision(project="proj", paths=None, when=T0, pid="rejected"):
    return {
        "project": project, "paths": paths if paths is not None else ["a.md"],
        "recorded_at": when.isoformat(), "patch_set_id": "synthetic-pass",
        "proposal_id": pid,
    }


class RejectionSupersessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.h = Harness(self.root)
        self.passes = self.h.claude_config_dir / "logs" / "memory-dream" / "passes"
        self.passes.mkdir(parents=True)
        self.ledger = self.passes / "rejections.json"
        self.counter = 0
        for project in ["proj", "other"]:
            live = self.h.project(project, mirror=False)
            for path in ["a.md", "b.md"]:
                (live / path).write_text(note(path, body="RESOLVED\n" + "x" * 7000), encoding="utf-8")
            (live / "MEMORY.md").write_text("- [A](a.md)\n- [B](b.md)\n", encoding="utf-8")

    def _proposal(self, path="a.md", pid="applied", project="proj", **changes):
        proposal = {
            "id": pid, "project": project, "action": "compress",
            "sources": [{"path": path, "digest": self.h.digest(project, path)}],
            "results": [{"path": path, "content": note(path, body="RESOLVED\n" + "y" * 7000)}],
            "deletes": [], "survivor": path,
        }
        proposal.update(changes)
        return proposal

    def _prepare(self, proposals, approved):
        self.counter += 1
        self.h.patch_set = self.passes / f"pass-{self.counter}"
        self.h.patch_set.mkdir()
        self.h._proposals = proposals
        self.h.write()
        return self.h.selection(approved)

    def _apply(self, proposals, approved, when, *extra):
        selection = self._prepare(proposals, approved)
        result = self.h.run(selection, "--now-ts", str(when.timestamp()), *extra, mirror=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        path = self.h.patch_set / "apply-manifest.json"
        if path.exists():
            os.utime(path, (when.timestamp(), when.timestamp()))
        return result

    def _write_ledger(self, entries, supersessions=None, **extra):
        payload = {"schema_version": 1, "entries": entries, **extra}
        if supersessions is not None:
            payload["supersessions"] = supersessions
        self.ledger.write_text(json.dumps(payload), encoding="utf-8")

    def _read_rejected(self, now=T0.date()):
        with mock.patch.object(config, "pass_root", return_value=self.passes):
            return audit.recently_rejected_paths(30, now)

    def _triage(self, applied_days=0, now=T0.date()):
        result = subprocess.run(
            [sys.executable, "-m", "memory_dream", "triage", "--format", "json",
             "--live-root", str(self.h.live_root), "--now", now.isoformat(),
             "--suppress-applied-days", str(applied_days), "--suppress-rejected-days", "30"],
            cwd=REPO_ROOT, env=_clean_env(self.h.claude_config_dir),
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_real_reject_then_apply_survives_disabled_expired_windows_and_cleanup(self):
        rejected = self._proposal(pid="rejected")
        rejected["sources"].append({"path": "b.md", "digest": self.h.digest("proj", "b.md")})
        self._apply([rejected], [], T0)
        before = json.loads(self.ledger.read_text())["entries"]
        self._apply([self._proposal()], ["applied"], T0 + dt.timedelta(days=1))
        payload = json.loads(self.ledger.read_text())
        self.assertEqual(payload["entries"], before)  # the rejection history is intact
        self.assertEqual(payload.get("supersessions"), [decision(
            when=T0 + dt.timedelta(days=1), pid="applied",
        ) | {"patch_set_id": self.h.manifest_id}])
        for days, now in [(0, T0.date() + dt.timedelta(days=1)), (1, T0.date() + dt.timedelta(days=3))]:
            with self.subTest(applied_days=days):
                result = self._triage(days, now)
                self.assertIn(("proj", "a.md"), {(r["project"], r["path"]) for r in result["flagged"]})
                self.assertEqual([(r["project"], r["path"]) for r in result["suppressed_rejected"]], [("proj", "b.md")])
                self.assertEqual(result["summary"]["suppressed_recently_rejected"], 1)
        normal = self._triage(14, T0.date() + dt.timedelta(days=1))
        self.assertEqual(normal["summary"]["suppressed_recently_applied"], 1)
        self.assertEqual(normal["summary"]["suppressed_recently_rejected"], 1)
        for path in self.passes.iterdir():
            if path.is_dir():
                shutil.rmtree(path)
        self.assertEqual(self._read_rejected(), {("proj", "b.md")})

    def test_later_rejection_wins_until_another_successful_apply(self):
        self._write_ledger([decision()])
        self._apply([self._proposal()], ["applied"], T0 + dt.timedelta(hours=1))
        self.assertEqual(self._read_rejected(), set())
        self._apply([self._proposal(pid="rejected-again")], [], T0 + dt.timedelta(hours=2))
        self.assertEqual(self._read_rejected(), {("proj", "a.md")})
        self._apply([self._proposal(pid="applied-again")], ["applied-again"], T0 + dt.timedelta(hours=3))
        self.assertEqual(self._read_rejected(), set())
        self.assertEqual(len(json.loads(self.ledger.read_text())["entries"]), 2)

    def test_partial_paths_and_projects_do_not_cross_suppress(self):
        entries = [decision(paths=["a.md", "b.md"]), decision(project="other")]
        self._write_ledger(entries, metadata={"keep": "unchanged"})
        self._apply([self._proposal()], ["applied"], T0 + dt.timedelta(hours=1))
        self.assertEqual(self._read_rejected(), {("proj", "b.md"), ("other", "a.md")})
        payload = json.loads(self.ledger.read_text())
        self.assertEqual(payload["entries"], entries)
        self.assertEqual(payload["metadata"], {"keep": "unchanged"})

    def test_skipped_left_unapproved_and_preflight_do_not_supersede(self):
        self._write_ledger([decision()])
        original = self.ledger.read_bytes()
        stale = self._proposal(sources=[{"path": "a.md", "digest": "stale"}])
        for proposal, extra in [(stale, ()), (self._proposal(action="leave"), ()), (self._proposal(), ("--preflight",))]:
            with self.subTest(proposal=proposal["action"], extra=extra):
                self._apply([proposal], ["applied"], T0 + dt.timedelta(hours=1), *extra)
                self.assertEqual(self._read_rejected(), {("proj", "a.md")})
                self.assertEqual(self.ledger.read_bytes(), original)
        self._apply([self._proposal(pid="unapproved")], [], T0 + dt.timedelta(hours=1))
        self.assertEqual(self._read_rejected(), {("proj", "a.md")})
        self.assertNotIn("supersessions", json.loads(self.ledger.read_text()))

    def test_failed_project_does_not_supersede(self):
        self._write_ledger([decision()])
        selection = self._prepare([self._proposal()], ["applied"])
        args = cli.build_parser().parse_args([
            "apply", "--patch-set", str(self.h.patch_set), "--selection", str(selection),
            "--transcript", str(self.h.transcript), "--live-root", str(self.h.live_root),
            "--now-ts", str((T0 + dt.timedelta(hours=1)).timestamp()),
        ])
        with mock.patch.dict(os.environ, _clean_env(self.h.claude_config_dir), clear=True), \
             mock.patch.object(apply_mod, "apply_project", side_effect=OSError("synthetic failure")), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(apply_mod.run_apply(args), 0)
        self.assertEqual(self._read_rejected(), {("proj", "a.md")})
        self.assertNotIn("supersessions", json.loads(self.ledger.read_text()))

    def test_older_and_equal_success_cannot_override_newer_rejection(self):
        for delta in [dt.timedelta(hours=-1), dt.timedelta(0)]:
            with self.subTest(delta=delta):
                self._write_ledger([decision()])
                self._apply([self._proposal()], ["applied"], T0 + delta)
                self.assertEqual(self._read_rejected(), {("proj", "a.md")})

    def test_success_is_retained_before_rejections_or_with_unrelated_history(self):
        self._apply([self._proposal()], ["applied"], T0)
        payload = json.loads(self.ledger.read_text())
        self.assertEqual(payload["entries"], [])
        self.assertEqual(payload["supersessions"][0]["paths"], ["a.md"])
        self._write_ledger([decision(project="other")])
        self._apply([self._proposal()], ["applied"], T0 + dt.timedelta(hours=1))
        payload = json.loads(self.ledger.read_text())
        self.assertEqual(payload["entries"], [decision(project="other")])
        self.assertEqual(payload["supersessions"][0]["project"], "proj")
        self.assertEqual(self._read_rejected(), {("other", "a.md")})

    def test_delayed_older_rejection_cannot_hide_a_later_success_without_history(self):
        selection = self._prepare([self._proposal(pid="older-rejection")], [])
        # Distinct patch-set parents give these real applies different outer
        # locks while they still share the configured rejection ledger.
        older_patch = self.root / "other-parent" / "older-pass"
        older_patch.parent.mkdir()
        shutil.move(str(self.h.patch_set), older_patch)
        ready = self.root / "rejection-ready"
        script = """
import sys
from pathlib import Path
from memory_dream import apply, cli
record = apply.record_rejections

def delayed(*args, **kwargs):
    Path(sys.argv[1]).write_text("ready", encoding="utf-8")
    if sys.stdin.readline().strip() != "release":
        raise RuntimeError("writer was not released")
    return record(*args, **kwargs)

apply.record_rejections = delayed
raise SystemExit(cli.main(sys.argv[2:]))
"""
        older = subprocess.Popen(
            [sys.executable, "-c", script, str(ready), "apply",
             "--patch-set", str(older_patch), "--selection", str(selection),
             "--transcript", str(self.h.transcript), "--live-root", str(self.h.live_root),
             "--now-ts", str(T0.timestamp())],
            cwd=REPO_ROOT, env=_clean_env(self.h.claude_config_dir),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 20
            while not ready.is_file() and older.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.is_file(), "older apply never reached ledger recording")
            self._apply([self._proposal()], ["applied"], T0 + dt.timedelta(hours=1))
        finally:
            try:
                stdout, stderr = older.communicate(input="release\n", timeout=20)
            except subprocess.TimeoutExpired:
                older.kill()
                older.communicate()
                raise
        self.assertEqual(older.returncode, 0, stdout + stderr)
        self.assertEqual(self._read_rejected(), set())
        payload = json.loads(self.ledger.read_text())
        self.assertEqual(payload["entries"][0]["proposal_id"], "older-rejection")
        self.assertEqual(payload["supersessions"][0]["proposal_id"], "applied")

    def test_reader_uses_latest_per_path_time_and_keeps_malformed_siblings(self):
        old = decision(when=T0 - dt.timedelta(hours=1), pid="old")
        newer = decision(when=T0 + dt.timedelta(hours=1), pid="new")
        # Different UTC offset, same real instant as T0 + 1 hour.
        newer["recorded_at"] = "2026-07-10T15:00:00+02:00"
        malformed = [None, {}, {"project": []}, decision(paths=None) | {"paths": {}},
                     decision() | {"recorded_at": "bad"}, decision() | {"recorded_at": "2026-07-11"}]
        for events in [[newer, old], [old, newer], [old, *malformed, newer]]:
            with self.subTest(events=events):
                self._write_ledger([decision(paths=["a.md", "b.md"]), None], events)
                self.assertEqual(self._read_rejected(), {("proj", "b.md")})
        for events in [None, {}, "bad", [old], malformed]:
            with self.subTest(events=events):
                self._write_ledger([decision()], events)
                self.assertEqual(self._read_rejected(), {("proj", "a.md")})

    def test_same_pass_rejection_is_not_overridden_by_a_success(self):
        self._write_ledger([decision()])
        self._apply([self._proposal(), self._proposal(pid="declined")], ["applied"], T0 + dt.timedelta(hours=1))
        self.assertEqual(self._read_rejected(), {("proj", "a.md")})
        payload = json.loads(self.ledger.read_text())
        self.assertEqual(len(payload["entries"]), 2)
        self.assertEqual(len(payload["supersessions"]), 1)

    def test_successful_outcomes_join_project_and_id_and_include_source_result_paths(self):
        proposals = [
            self._proposal(pid="same-id", sources=[{"path": "old.md"}],
                           results=[{"path": "new.md", "content": "synthetic"}]),
            self._proposal(pid="same-id", project="other"),
            self._proposal(pid="unmatched"),
        ]
        outcomes = [
            {"project": "proj", "committed": False, "proposals": [
                {"id": "same-id", "status": "applied"},
                {"id": "unmatched", "status": "failed"},
            ]},
            {"project": "other", "committed": True, "proposals": [
                {"id": "same-id", "status": "skipped"},
            ]},
        ]
        manifest = {"id": "synthetic-pass", "proposals": proposals}
        self.assertEqual(apply_mod.applied_proposal_entries(manifest, outcomes, T0.isoformat()), [
            decision(paths=["new.md", "old.md"], pid="same-id"),
        ])

    def test_malformed_supersession_container_preserves_history_on_write(self):
        self._write_ledger([decision()], {"do-not-discard": "unknown data"})
        original = self.ledger.read_bytes()
        result = self._apply([self._proposal()], ["applied"], T0 + dt.timedelta(hours=1))
        self.assertIn("WARNING could not record rejections", result.stderr)
        self.assertEqual(self.ledger.read_bytes(), original)
        self.assertEqual(self._read_rejected(), {("proj", "a.md")})

    def test_corrupt_history_is_not_overwritten_when_backup_rename_fails(self):
        self.ledger.write_bytes(b"{ corrupt history")
        original = self.ledger.read_bytes()
        with mock.patch.object(config, "pass_root", return_value=self.passes), \
             mock.patch.object(Path, "replace", side_effect=OSError("synthetic rename failure")), \
             mock.patch.object(audit, "atomic_write") as write:
            with self.assertRaises(OSError):
                apply_mod.record_rejections([decision()])
            write.assert_not_called()
        self.assertEqual(self.ledger.read_bytes(), original)

    def test_legacy_or_invalid_rejection_time_cannot_be_superseded_by_guessing(self):
        for when in ["2026-07-10", "2026-07-10T12:00:00", "2026-07-10-invalid"]:
            with self.subTest(when=when):
                self._write_ledger([decision() | {"recorded_at": when}], [decision(when=T0 + dt.timedelta(days=1))])
                self.assertEqual(self._read_rejected(), {("proj", "a.md")})

    def test_concurrent_ledger_writer_cannot_erase_success_or_new_rejection(self):
        self._write_ledger([decision()])
        original = self.ledger.read_bytes()
        ready = self.root / "writer-ready"
        script = """
import json
import sys
from pathlib import Path
from memory_dream import apply, audit
original_write = audit.atomic_write

def pause(path, data):
    Path(sys.argv[1]).write_text("ready", encoding="utf-8")
    if sys.stdin.readline().strip() != "release":
        raise RuntimeError("writer was not released")
    original_write(path, data)

audit.atomic_write = pause
apply.record_rejections([json.loads(sys.argv[2])])
"""
        newer_rejection = decision(paths=["b.md"], when=T0 + dt.timedelta(hours=2))
        writer = subprocess.Popen(
            [sys.executable, "-c", script, str(ready), json.dumps(newer_rejection)],
            cwd=REPO_ROOT, env=_clean_env(self.h.claude_config_dir),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 20
            while not ready.is_file() and writer.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.is_file(), "writer never reached atomic replacement")
            result = self._apply([self._proposal()], ["applied"], T0 + dt.timedelta(hours=1))
            while_paused = self.ledger.read_bytes()
        finally:
            try:
                stdout, stderr = writer.communicate(input="release\n", timeout=20)
            except subprocess.TimeoutExpired:
                writer.kill()
                writer.communicate()
                raise
        self.assertEqual(writer.returncode, 0, stdout + stderr)
        self.assertEqual(while_paused, original, "contending apply wrote around the ledger lock")
        self.assertIn("WARNING could not record rejections", result.stderr)
        self.assertIn("rejections.json.lock", result.stderr)
        self.assertEqual(self._read_rejected(), {("proj", "a.md"), ("proj", "b.md")})
        self._apply([self._proposal()], ["applied"], T0 + dt.timedelta(hours=3))
        self.assertEqual(self._read_rejected(), {("proj", "b.md")})

    def test_ledger_lock_covers_read_and_write_and_releases_after_failure(self):
        self._write_ledger([decision()])
        lock_path = self.passes / "rejections.json.lock"
        calls = []
        read = Path.read_text
        write = audit.atomic_write

        def assert_locked():
            with self.assertRaises(apply_mod.compat.LockHeld):
                with apply_mod.compat.FileLock(lock_path):
                    pass

        def checked_read(path, *args, **kwargs):
            if path == self.ledger:
                assert_locked()
                calls.append("read")
            return read(path, *args, **kwargs)

        def checked_write(path, data):
            assert_locked()
            calls.append("write")
            return write(path, data)

        with mock.patch.object(config, "pass_root", return_value=self.passes), \
             mock.patch.object(Path, "read_text", checked_read), \
             mock.patch.object(audit, "atomic_write", checked_write):
            apply_mod.record_rejections([decision(paths=["b.md"])])
        self.assertEqual(calls, ["read", "write"])
        with mock.patch.object(config, "pass_root", return_value=self.passes), \
             mock.patch.object(audit, "atomic_write", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                apply_mod.record_rejections([decision()])
        with apply_mod.compat.FileLock(lock_path):
            pass

    def test_ledger_write_failure_is_advisory_and_preserves_old_history(self):
        self._write_ledger([decision()])
        original = self.ledger.read_bytes()
        selection = self._prepare([self._proposal()], ["applied"])
        args = cli.build_parser().parse_args([
            "apply", "--patch-set", str(self.h.patch_set), "--selection", str(selection),
            "--transcript", str(self.h.transcript), "--live-root", str(self.h.live_root),
            "--now-ts", str((T0 + dt.timedelta(hours=1)).timestamp()),
        ])
        real_write = audit.atomic_write
        def fail_ledger(path, data):
            if path == self.ledger:
                raise OSError("synthetic ledger failure")
            return real_write(path, data)
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, _clean_env(self.h.claude_config_dir), clear=True), \
             mock.patch.object(audit, "atomic_write", side_effect=fail_ledger), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            self.assertEqual(apply_mod.run_apply(args), 0)
        self.assertIn("WARNING could not record rejections", stderr.getvalue())
        self.assertEqual(self.ledger.read_bytes(), original)
        self.assertEqual(self._read_rejected(), {("proj", "a.md")})
        self.assertEqual(json.loads((self.h.patch_set / "apply-manifest.json").read_text())["applied"], 1)


if __name__ == "__main__":
    unittest.main()
