#!/usr/bin/env python3
"""Tests for memory_dream.audit: structural findings (audit), deterministic
consolidation-candidate scoring (triage), and mechanical wikilink/index
repair (fix).

Every CLI-invocation test shells out to ``python3 -m memory_dream <sub>`` in
a child process, so each gets its own clean CLAUDE_CONFIG_DIR (never a
developer's real ~/.claude/memory-dream.json) and never inherits
MEMORY_DREAM_*/CLAUDE_MEMORY_*/CLAUDE_JOB_DIR from the outer environment.
Pure-helper tests (AuditHelperTests) call memory_dream.audit directly.
"""

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from memory_dream import audit, config

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


def note(name="note", note_type="project", extra="", body="Body.", nested=True):
    type_field = f"metadata:\n  type: {note_type}\n" if nested else f"type: {note_type}\n"
    return (
        "---\n"
        f"name: {name}\n"
        f"description: Fixture note describing {name} in detail\n"
        f"{type_field}"
        f"{extra}"
        "---\n"
        f"{body}\n"
    )


class Fixture:
    """A live+mirror tree and a fresh per-fixture CLAUDE_CONFIG_DIR."""

    def __init__(self, root: Path):
        self.live = root / "live"
        self.mirror = root / "mirror"
        self.claude_config_dir = root / "claude-config"
        self.live.mkdir()
        self.mirror.mkdir()
        self.claude_config_dir.mkdir()

    def project(self, key="project", mirror=True):
        live = self.live / key / "memory"
        live.mkdir(parents=True)
        mirrored = self.mirror / key
        if mirror:
            mirrored.mkdir(parents=True)
        return live, mirrored


class MemoryAuditTests(unittest.TestCase):
    def run_audit(self, fixture, *args):
        command = [
            sys.executable, "-m", "memory_dream", "audit",
            "--live-root", str(fixture.live),
            "--mirror-root", str(fixture.mirror),
            "--format", "json",
            *args,
        ]
        return subprocess.run(
            command, text=True, capture_output=True, check=False,
            cwd=REPO_ROOT, env=_clean_env(fixture.claude_config_dir),
        )

    def test_clean_fixture_and_deterministic_json(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, mirror = fixture.project()
            (live / "MEMORY.md").write_text("- [Note](note.md)\n- [Legacy](legacy.md)\n", encoding="utf-8", newline="\n")
            (live / "note.md").write_text(note(), encoding="utf-8", newline="\n")
            (live / "legacy.md").write_text(note("legacy", nested=False), encoding="utf-8", newline="\n")
            for path in live.iterdir():
                (mirror / path.name).write_bytes(path.read_bytes())
            first = self.run_audit(fixture)
            second = self.run_audit(fixture)
            self.assertEqual(first.returncode, 0)
            self.assertEqual(first.stdout, second.stdout)
            self.assertNotIn("timestamp", first.stdout)
            self.assertEqual(json.loads(first.stdout)["findings"], [])

    def test_coverage_drift_orphans_indexes_and_escaped_links(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, mirror = fixture.project()
            (live / "MEMORY.md").write_text("- [Escaped](space\\(copy\\).md)\n"
            "- [Missing](gone.md)\n"
            "- [Outside](../outside.md)\n", encoding="utf-8", newline="\n")
            (live / "space(copy).md").write_text(note("escaped"), encoding="utf-8", newline="\n")
            (live / "unindexed.md").write_text(note("unindexed"), encoding="utf-8", newline="\n")
            (live / "attachment.txt").write_text("live attachment\n", encoding="utf-8", newline="\n")
            (mirror / "MEMORY.md").write_text("old\n", encoding="utf-8", newline="\n")
            (mirror / "orphan.md").write_text(note("orphan"), encoding="utf-8", newline="\n")
            (mirror / "mirror-only.txt").write_text("mirror attachment\n", encoding="utf-8", newline="\n")
            fixture.project("missing-mirror", mirror=False)[0].joinpath("MEMORY.md").write_text("", encoding="utf-8", newline="\n")
            orphan_project = fixture.mirror / "mirror-only"
            orphan_project.mkdir()
            (orphan_project / "MEMORY.md").write_text("", encoding="utf-8", newline="\n")

            result = self.run_audit(fixture)
            kinds = [item["kind"] for item in json.loads(result.stdout)["findings"]]
            self.assertEqual(result.returncode, 1)
            findings = json.loads(result.stdout)["findings"]
            self.assertIn(
                {"kind": "mirror_missing_file", "project": "project", "path": "attachment.txt"},
                findings,
            )
            self.assertIn(
                {"kind": "mirror_only_file", "project": "project", "path": "mirror-only.txt"},
                findings,
            )
            for expected in (
                "mirror_missing_project",
                "mirror_stale_file",
                "mirror_only_file",
                "mirror_only_project",
                "unindexed_markdown",
                "missing_index_target",
                "escaping_index_target",
            ):
                self.assertIn(expected, kinds)
            escaped = [item for item in json.loads(result.stdout)["findings"] if item.get("path") == "space(copy).md"]
            self.assertFalse(any(item["kind"] == "unindexed_markdown" for item in escaped))

    def test_frontmatter_duplicates_sensitive_redaction_and_stale_date(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, _ = fixture.project(mirror=False)
            (live / "MEMORY.md").write_text("[A](a.md) [B](b.md) [Bad](bad.md) [Secret](api-key.md)\n", encoding="utf-8", newline="\n")
            (live / "a.md").write_text(note("same", extra="status: 2025-01-01\n", body="Body date: 2025-01-01"), encoding="utf-8", newline="\n")
            (live / "b.md").write_text(note("same", extra="  api_token: redacted-fixture-value\n"), encoding="utf-8", newline="\n")
            (live / "bad.md").write_text("---\nname: bad\nmetadata:\n  type: wrong\n---\nbody\n", encoding="utf-8", newline="\n")
            (live / "api-key.md").write_text(note("sensitive", body="api_key=highsignalsignaturevalue123456789"), encoding="utf-8", newline="\n")
            result = self.run_audit(fixture, "--as-of", "2025-05-01", "--scan-content")
            payload = json.loads(result.stdout)
            kinds = [item["kind"] for item in payload["findings"]]
            self.assertIn("duplicate_frontmatter_name", kinds)
            self.assertIn("invalid_frontmatter", kinds)
            self.assertIn("sensitive_filename_indicator", kinds)
            self.assertIn("sensitive_frontmatter_indicator", kinds)
            self.assertIn("sensitive_content_indicator", kinds)
            self.assertIn("stale_date_review", kinds)
            self.assertEqual(payload["as_of"], "2025-05-01")
            self.assertNotIn("redacted-fixture-value", result.stdout)
            self.assertNotIn("highsignalsignaturevalue", result.stdout)
            sensitive_only = self.run_audit(fixture, "--sensitive-exit")
            self.assertEqual(sensitive_only.returncode, 1)

    def test_token_project_name_is_not_a_sensitive_filename(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, mirror = fixture.project()
            (live / "MEMORY.md").write_text("[Token eater](project_token_eater.md)\n", encoding="utf-8", newline="\n")
            (live / "project_token_eater.md").write_text(note("token-eater"), encoding="utf-8", newline="\n")
            for path in live.iterdir():
                (mirror / path.name).write_bytes(path.read_bytes())

            result = self.run_audit(fixture)
            self.assertEqual(result.returncode, 0)

    def test_truncated_description_detection(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, mirror = fixture.project()
            (live / "MEMORY.md").write_text("[T](truncated.md) [F](fine.md)\n", encoding="utf-8", newline="\n")
            (live / "truncated.md").write_text("---\nname: truncated\ndescription: shipped as PR\nmetadata:\n  type: project\n---\nBody.\n", encoding="utf-8", newline="\n")
            (live / "fine.md").write_text('---\nname: fine\ndescription: "Delivered patch #7 and merged."\nmetadata:\n  type: project\n---\nBody.\n', encoding="utf-8", newline="\n")
            for name, desc in (
                ("caps", "keeps payload-logging ON"),
                ("flag", "never use git add -A"),
                ("path", "transcripts live in temp/meeting-transcripts/"),
            ):
                (live / f"{name}.md").write_text(f"---\nname: {name}\ndescription: {desc}\nmetadata:\n  type: project\n---\nBody.\n", encoding="utf-8", newline="\n")
            index_lines = "".join(f"[{n}]({n}.md) " for n in ("truncated", "fine", "caps", "flag", "path"))
            (live / "MEMORY.md").write_text(index_lines + "\n", encoding="utf-8", newline="\n")
            for path in live.iterdir():
                (mirror / path.name).write_bytes(path.read_bytes())
            result = self.run_audit(fixture)
            findings = json.loads(result.stdout)["findings"]
            self.assertIn(
                {"kind": "truncated_description", "project": "project", "path": "truncated.md"},
                findings,
            )
            flagged = {f["path"] for f in findings if f["kind"] == "truncated_description"}
            self.assertEqual(flagged, {"truncated.md"})

    def test_sensitive_scan_covers_index_frontmatter_and_attachments(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, _ = fixture.project(mirror=False)
            (live / "MEMORY.md").write_text("[N](note.md)\napi_key=indexleakhighsignalvalue1234567890\n", encoding="utf-8", newline="\n")
            (live / "note.md").write_text(note(), encoding="utf-8", newline="\n")
            (live / "attachment.txt").write_text("ghp_" + "a" * 20 + "\n", encoding="utf-8", newline="\n")
            result = self.run_audit(fixture)
            findings = json.loads(result.stdout)["findings"]
            flagged = {f["path"] for f in findings if f["kind"] == "sensitive_content_indicator"}
            self.assertIn("MEMORY.md", flagged)
            self.assertIn("attachment.txt", flagged)
            self.assertNotIn("indexleakhighsignalvalue", result.stdout)
            sensitive_exit = self.run_audit(fixture, "--sensitive-exit")
            self.assertEqual(sensitive_exit.returncode, 1)

    def test_yaml_literal_name_is_invalid(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, _ = fixture.project(mirror=False)
            (live / "MEMORY.md").write_text("[N](nullname.md)\n", encoding="utf-8", newline="\n")
            (live / "nullname.md").write_text("---\nname: null\ndescription: false\nmetadata:\n  type: project\n---\nBody.\n", encoding="utf-8", newline="\n")
            result = self.run_audit(fixture)
            findings = json.loads(result.stdout)["findings"]
            problems = [
                p for f in findings if f["kind"] == "invalid_frontmatter" for p in f["problems"]
            ]
            self.assertIn("name is a YAML literal, not a string", problems)
            self.assertIn("description is a YAML literal, not a string", problems)

    def test_wikilink_typo_detection(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, _ = fixture.project(mirror=False)
            (live / "MEMORY.md").write_text("[A](project_alpha_note.md) [B](project_beta.md)\n", encoding="utf-8", newline="\n")
            (live / "project_alpha_note.md").write_text(note("alpha"), encoding="utf-8", newline="\n")
            (live / "project_beta.md").write_text(note("beta", body="See [[alpha-note]] and [[totally-unknown-forward-ref]]."), encoding="utf-8", newline="\n")
            result = self.run_audit(fixture)
            findings = json.loads(result.stdout)["findings"]
            typos = [f for f in findings if f["kind"] == "wikilink_typo"]
            self.assertEqual(len(typos), 1)
            self.assertEqual(typos[0]["link"], "alpha-note")
            self.assertEqual(typos[0]["resolves_to"], "project_alpha_note")

    def test_symlinked_memory_root_is_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            outside = Path(temp) / "outside"
            outside.mkdir()
            (outside / "MEMORY.md").write_text("", encoding="utf-8", newline="\n")
            project_parent = fixture.live / "linked"
            project_parent.mkdir()
            (project_parent / "memory").symlink_to(outside)
            result = self.run_audit(fixture)
            findings = json.loads(result.stdout)["findings"]
            self.assertIn(
                {"kind": "live_symlink", "project": "linked", "path": "memory"}, findings
            )
            sensitive_exit = self.run_audit(fixture, "--sensitive-exit")
            self.assertEqual(sensitive_exit.returncode, 1)

    def test_missing_memory_index_and_oversize_metrics(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, _ = fixture.project("missing", mirror=False)
            (live / "note.md").write_text(note(), encoding="utf-8", newline="\n")
            large, _ = fixture.project("large", mirror=False)
            (large / "MEMORY.md").write_text("one\ntwo\n", encoding="utf-8", newline="\n")
            result = self.run_audit(fixture, "--max-index-lines", "1")
            kinds = [item["kind"] for item in json.loads(result.stdout)["findings"]]
            self.assertIn("missing_memory_index", kinds)
            self.assertIn("oversized_memory_index", kinds)


class MemoryTriageTests(unittest.TestCase):
    def run_triage(self, fixture, *args):
        command = [
            sys.executable, "-m", "memory_dream", "triage",
            "--live-root", str(fixture.live),
            "--mirror-root", str(fixture.mirror),
            "--format", "json",
            *args,
        ]
        return subprocess.run(
            command, text=True, capture_output=True, check=False,
            cwd=REPO_ROOT, env=_clean_env(fixture.claude_config_dir),
        )

    def test_stacked_supersession_and_oversized_flagged(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, _ = fixture.project(mirror=False)
            (live / "MEMORY.md").write_text("- [S](stacked.md)\n- [L](lognote.md)\n", encoding="utf-8", newline="\n")
            (live / "stacked.md").write_text(note("stacked", body="SUPERSEDED 2025-01: a.\nCORRECTED 2025-02: b.\nRESOLVED 2025-03: c."), encoding="utf-8", newline="\n")
            (live / "lognote.md").write_text(note("lognote", body="x" * 6500), encoding="utf-8", newline="\n")
            result = self.run_triage(fixture)
            self.assertEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            flagged = {item["path"]: item for item in payload["flagged"]}
            self.assertIn("stacked.md", flagged)
            self.assertTrue(any(r.startswith("supersession:") for r in flagged["stacked.md"]["reasons"]))
            self.assertEqual(flagged["stacked.md"]["supersessions"], 3)
            self.assertIn("lognote.md", flagged)
            self.assertTrue(any(r.startswith("size:") for r in flagged["lognote.md"]["reasons"]))

    def test_fresh_small_linked_note_not_flagged(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, _ = fixture.project(mirror=False)
            (live / "MEMORY.md").write_text("- [A](alpha.md)\n- [B](beta.md)\n", encoding="utf-8", newline="\n")
            (live / "alpha.md").write_text(note("alpha", body="Short durable fact."), encoding="utf-8", newline="\n")
            (live / "beta.md").write_text(note("beta", body="See [[alpha]] for context."), encoding="utf-8", newline="\n")
            result = self.run_triage(fixture)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["summary"]["flagged"], 0)
            self.assertEqual(payload["flagged"], [])

    def test_empty_and_single_note_projects_produce_no_flags(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            empty, _ = fixture.project("empty", mirror=False)
            (empty / "MEMORY.md").write_text("", encoding="utf-8", newline="\n")
            single, _ = fixture.project("single", mirror=False)
            (single / "MEMORY.md").write_text("- [Only](only.md)\n", encoding="utf-8", newline="\n")
            (single / "only.md").write_text(note("only", body="One small durable fact."), encoding="utf-8", newline="\n")
            result = self.run_triage(fixture)
            self.assertEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["summary"]["flagged"], 0)

    def test_age_signal_uses_mtime_not_mirror(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, mirror = fixture.project()
            (live / "MEMORY.md").write_text("- [Old](old.md)\n", encoding="utf-8", newline="\n")
            # Oversized so it flags structurally; the point of the test is that its
            # reported age comes from live mtime, never mirror state.
            (live / "old.md").write_text(note("old", body="A" * 6500), encoding="utf-8", newline="\n")
            # Mirror copy diverges (a mirror_stale_file condition), but triage age
            # must come from the live file's mtime regardless of any mirror state.
            (mirror / "old.md").write_text(note("old", body="Different stale mirror content."), encoding="utf-8", newline="\n")
            old_ts = time.mktime(dt.date(2026, 1, 1).timetuple())
            os.utime(live / "old.md", (old_ts, old_ts))
            result = self.run_triage(fixture, "--now", "2026-07-17")
            payload = json.loads(result.stdout)
            flagged = {item["path"]: item for item in payload["flagged"]}
            self.assertIn("old.md", flagged)
            self.assertEqual(flagged["old.md"]["age_source"], "mtime")
            expected = (dt.date(2026, 7, 17) - dt.date(2026, 1, 1)).days
            self.assertEqual(flagged["old.md"]["age_days"], expected)
            self.assertTrue(any(r.startswith("age:") for r in flagged["old.md"]["reasons"]))

    def test_deterministic_output_and_single_flagged_line(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            for key in ("bravo", "alpha"):
                live, _ = fixture.project(key, mirror=False)
                (live / "MEMORY.md").write_text("- [Log](log.md)\n", encoding="utf-8", newline="\n")
                (live / "log.md").write_text(note("log", body="y" * 6500), encoding="utf-8", newline="\n")
            first = self.run_triage(fixture)
            second = self.run_triage(fixture)
            self.assertEqual(first.stdout, second.stdout)
            human = subprocess.run(
                [
                    sys.executable, "-m", "memory_dream", "triage",
                    "--live-root", str(fixture.live), "--mirror-root", str(fixture.mirror),
                ],
                text=True, capture_output=True, check=False,
                cwd=REPO_ROOT, env=_clean_env(fixture.claude_config_dir),
            )
            self.assertEqual(human.stdout.count("flagged:"), 1)
            self.assertTrue(human.stdout.rstrip().endswith("flagged:2"))
            # Human output is project-level only: no note filenames leak into it.
            self.assertNotIn("log.md", human.stdout)

    def test_json_envelope_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, _ = fixture.project(mirror=False)
            (live / "MEMORY.md").write_text("- [L](log.md)\n", encoding="utf-8", newline="\n")
            (live / "log.md").write_text(note("log", body="z" * 6500), encoding="utf-8", newline="\n")
            payload = json.loads(self.run_triage(fixture).stdout)
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["command"], "triage")
            self.assertIn("roots", payload)
            for field in ("live_projects", "notes_scored", "flagged", "by_project"):
                self.assertIn(field, payload["summary"])
            record = payload["flagged"][0]
            for field in ("project", "path", "score", "reasons", "body_bytes", "age_days", "inbound_links", "supersessions"):
                self.assertIn(field, record)


class MemoryFixTests(unittest.TestCase):
    def run_fix(self, fixture, *args):
        command = [
            sys.executable, "-m", "memory_dream", "fix",
            "--live-root", str(fixture.live),
            "--mirror-root", str(fixture.mirror),
            "--format", "json",
            *args,
        ]
        return subprocess.run(
            command, text=True, capture_output=True, check=False,
            cwd=REPO_ROOT, env=_clean_env(fixture.claude_config_dir),
        )

    def mirror_from_live(self, live, mirror):
        for path in live.iterdir():
            if path.is_file():
                shutil.copy2(path, mirror / path.name)

    def test_single_candidate_rewrite_only(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, _ = fixture.project(mirror=False)
            (live / "MEMORY.md").write_text("- [A](project_alpha_note.md)\n", encoding="utf-8", newline="\n")
            (live / "project_alpha_note.md").write_text(note("alpha"), encoding="utf-8", newline="\n")
            (live / "foo-bar.md").write_text(note("foobar-a"), encoding="utf-8", newline="\n")
            (live / "foo_bar.md").write_text(note("foobar-b"), encoding="utf-8", newline="\n")
            (live / "beta.md").write_text(note("beta", body="Links: [[alpha-note]], [[totally-unknown]], [[foo bar]]."), encoding="utf-8", newline="\n")
            payload = json.loads(self.run_fix(fixture).stdout)
            rewrites = [r for r in payload["repairs"] if r["kind"] == "wikilink_rewrite"]
            self.assertEqual(len(rewrites), 1)
            self.assertEqual(rewrites[0]["link"], "alpha-note")
            self.assertEqual(rewrites[0]["resolves_to"], "project_alpha_note")

    def test_index_reconcile_append_drop_and_format_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, mirror = fixture.project()
            header = "# Memory index\n\nIntro prose line.\n\n"
            (live / "MEMORY.md").write_text(header + "- [Kept](kept.md)\n- [Gone](gone.md)\n", encoding="utf-8", newline="\n")
            (live / "kept.md").write_text(note("kept"), encoding="utf-8", newline="\n")
            (live / "fresh.md").write_text('---\nname: fresh\ndescription: "Delivered milestone #7, tracked"\nmetadata:\n  type: project\n---\nBody.\n', encoding="utf-8", newline="\n")
            self.mirror_from_live(live, mirror)
            dry = json.loads(self.run_fix(fixture).stdout)
            reconcile = [r for r in dry["repairs"] if r["kind"] == "index_reconcile"]
            self.assertEqual(len(reconcile), 1)
            self.assertEqual(reconcile[0]["appended"], ["fresh.md"])
            self.assertEqual(reconcile[0]["dropped"], ["gone.md"])
            # Dry-run writes nothing.
            self.assertIn("- [Gone](gone.md)", (live / "MEMORY.md").read_text(encoding="utf-8"))
            # Apply and verify the reconciliation result.
            self.assertEqual(self.run_fix(fixture, "--apply").returncode, 0)
            text = (live / "MEMORY.md").read_text(encoding="utf-8")
            self.assertTrue(text.startswith(header))  # header + prose preserved byte-for-byte
            self.assertIn("- [Kept](kept.md)", text)
            self.assertNotIn("gone.md", text)
            self.assertIn("(fresh.md)", text)
            self.assertIn("milestone #7", text)  # '#' not truncated

    def test_dry_run_writes_nothing_and_preserves_bodies(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, _ = fixture.project(mirror=False)
            (live / "MEMORY.md").write_text("- [A](project_alpha_note.md)\n", encoding="utf-8", newline="\n")
            (live / "project_alpha_note.md").write_text(note("alpha"), encoding="utf-8", newline="\n")
            beta_text = note("beta", body="See [[alpha-note]].")
            (live / "beta.md").write_text(beta_text, encoding="utf-8", newline="\n")
            before = {p.name: p.read_bytes() for p in live.iterdir()}
            self.run_fix(fixture)
            after = {p.name: p.read_bytes() for p in live.iterdir()}
            self.assertEqual(before, after)

    def test_index_reconcile_keeps_mixed_dead_live_line(self):
        # A line naming both a gone and a surviving note is left intact, and the gone
        # target is NOT claimed as dropped (the record must never lie).
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, mirror = fixture.project()
            mixed = "- see [combo](gone.md) and [live](kept.md)\n"
            (live / "MEMORY.md").write_text(mixed + "- [Solo](solo-gone.md)\n", encoding="utf-8", newline="\n")
            (live / "kept.md").write_text(note("kept"), encoding="utf-8", newline="\n")
            self.mirror_from_live(live, mirror)
            payload = json.loads(self.run_fix(fixture).stdout)
            reconcile = [r for r in payload["repairs"] if r["kind"] == "index_reconcile"][0]
            # solo-gone.md is on an all-dead line -> droppable; gone.md is on a mixed
            # line -> not claimed.
            self.assertIn("solo-gone.md", reconcile["dropped"])
            self.assertNotIn("gone.md", reconcile["dropped"])
            self.assertEqual(self.run_fix(fixture, "--apply").returncode, 0)
            text = (live / "MEMORY.md").read_text(encoding="utf-8")
            self.assertIn(mixed, text)  # mixed line preserved byte-for-byte
            self.assertNotIn("solo-gone.md", text)  # all-dead line removed

    def test_apply_refuses_stale_mirror_and_preserves_mtime(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, mirror = fixture.project()
            (live / "MEMORY.md").write_text("- [A](project_alpha_note.md)\n- [B](beta.md)\n", encoding="utf-8", newline="\n")
            (live / "project_alpha_note.md").write_text(note("alpha"), encoding="utf-8", newline="\n")
            beta = live / "beta.md"
            beta.write_text(note("beta", body="See [[alpha-note]]."), encoding="utf-8", newline="\n")
            self.mirror_from_live(live, mirror)
            # Diverge the mirror: the affected project is now stale.
            (mirror / "project_alpha_note.md").write_text(note("alpha", body="mirror drift"), encoding="utf-8", newline="\n")
            refused = self.run_fix(fixture, "--apply")
            self.assertEqual(refused.returncode, 1)
            self.assertIn("mirror not fresh", refused.stderr)
            self.assertIn("[[alpha-note]]", beta.read_text(encoding="utf-8"))  # nothing written
            # Re-sync the mirror, then apply and confirm mtime is preserved.
            self.mirror_from_live(live, mirror)
            pre = beta.stat().st_mtime_ns
            time.sleep(0.02)
            self.assertEqual(self.run_fix(fixture, "--apply").returncode, 0)
            self.assertIn("[[project_alpha_note]]", beta.read_text(encoding="utf-8"))
            self.assertEqual(beta.stat().st_mtime_ns, pre)


class TriageDescriptionTests(unittest.TestCase):
    """Description-quality flags: recall is description-routed, so triage surfaces
    unroutable descriptions for a redescribe proposal."""

    run_triage = MemoryTriageTests.run_triage

    def test_short_and_duplicate_descriptions_flag(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, _ = fixture.project(mirror=False)
            (live / "MEMORY.md").write_text("- [A](a.md)\n- [B](b.md)\n- [C](c.md)\n- [D](d.md)\n", encoding="utf-8", newline="\n")
            short = note("a").replace("description: Fixture note describing a in detail", "description: Vague words")
            (live / "a.md").write_text(short, encoding="utf-8", newline="\n")
            dup = note("b").replace("describing b in detail", "covering the same exact topic")
            (live / "b.md").write_text(dup, encoding="utf-8", newline="\n")
            dup2 = note("c").replace("describing c in detail", "covering the same exact topic")
            (live / "c.md").write_text(dup2, encoding="utf-8", newline="\n")
            (live / "d.md").write_text(note("d", body="Fine and healthy."), encoding="utf-8", newline="\n")
            result = self.run_triage(fixture)
            self.assertEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            flagged = {item["path"]: item["reasons"] for item in payload["flagged"]}
            self.assertIn("desc_short", flagged["a.md"])
            self.assertIn("desc_dup", flagged["b.md"])
            self.assertIn("desc_dup", flagged["c.md"])
            self.assertNotIn("d.md", flagged)

    def test_index_over_budget_warns(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, _ = fixture.project(mirror=False)
            (live / "a.md").write_text(note("a"), encoding="utf-8", newline="\n")
            # 150 effective lines >= 70% of the 200-line session load cap.
            (live / "MEMORY.md").write_text("- [A](a.md)\n" + "context line\n" * 149, encoding="utf-8", newline="\n")
            result = self.run_triage(fixture)
            payload = json.loads(result.stdout)
            over = payload["summary"]["index_over_budget"]
            self.assertIn("project", over)
            self.assertEqual(over["project"]["lines"], 150)
            human = subprocess.run(
                [
                    sys.executable, "-m", "memory_dream", "triage",
                    "--live-root", str(fixture.live), "--mirror-root", str(fixture.mirror),
                ],
                text=True, capture_output=True, check=False,
                cwd=REPO_ROOT, env=_clean_env(fixture.claude_config_dir),
            )
            self.assertIn("WARN project: MEMORY.md at 150/200 lines", human.stdout)

    def test_index_within_budget_no_warning(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, _ = fixture.project(mirror=False)
            (live / "a.md").write_text(note("a"), encoding="utf-8", newline="\n")
            (live / "MEMORY.md").write_text("- [A](a.md)\n", encoding="utf-8", newline="\n")
            payload = json.loads(self.run_triage(fixture).stdout)
            self.assertEqual(payload["summary"]["index_over_budget"], {})


class DecayTests(unittest.TestCase):
    """Confidence decay (0.5 half-life, 90d, 0.3 flag threshold) through the
    shared decay_effective() helper, exercised at BOTH call sites (audit() and
    triage_project()) so the extracted helper is proven to agree with itself."""

    run_audit = MemoryAuditTests.run_audit
    run_triage = MemoryTriageTests.run_triage

    def _decay_note(self, name, confidence, last_validated, note_type="project"):
        extra = f"  confidence: {confidence}\n  last_validated: {last_validated}\n"
        return note(name, note_type=note_type, extra=extra, body="Body has enough content to avoid other flags.")

    # -- audit() -----------------------------------------------------------

    def test_audit_flags_just_below_threshold(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, mirror = fixture.project()
            last_validated = (dt.date.today() - dt.timedelta(days=157)).isoformat()
            (live / "MEMORY.md").write_text("[N](decayed.md)\n", encoding="utf-8", newline="\n")
            (live / "decayed.md").write_text(self._decay_note("decayed", 1.0, last_validated), encoding="utf-8", newline="\n")
            for path in live.iterdir():
                (mirror / path.name).write_bytes(path.read_bytes())
            result = self.run_audit(fixture)
            findings = json.loads(result.stdout)["findings"]
            decayed = [f for f in findings if f["kind"] == "decayed_confidence"]
            self.assertEqual(len(decayed), 1)
            self.assertEqual(decayed[0]["path"], "decayed.md")
            self.assertLess(decayed[0]["effective"], 0.3)

    def test_audit_does_not_flag_just_above_threshold(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, mirror = fixture.project()
            last_validated = (dt.date.today() - dt.timedelta(days=150)).isoformat()
            (live / "MEMORY.md").write_text("[N](fresh.md)\n", encoding="utf-8", newline="\n")
            (live / "fresh.md").write_text(self._decay_note("fresh", 1.0, last_validated), encoding="utf-8", newline="\n")
            for path in live.iterdir():
                (mirror / path.name).write_bytes(path.read_bytes())
            result = self.run_audit(fixture)
            findings = json.loads(result.stdout)["findings"]
            self.assertFalse([f for f in findings if f["kind"] == "decayed_confidence"])

    def test_audit_unparseable_last_validated_flags_problem_not_decay(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, mirror = fixture.project()
            (live / "MEMORY.md").write_text("[N](broken.md)\n", encoding="utf-8", newline="\n")
            (live / "broken.md").write_text(self._decay_note("broken", 0.9, "not-a-date"), encoding="utf-8", newline="\n")
            for path in live.iterdir():
                (mirror / path.name).write_bytes(path.read_bytes())
            result = self.run_audit(fixture)
            findings = json.loads(result.stdout)["findings"]
            self.assertFalse([f for f in findings if f["kind"] == "decayed_confidence"])
            problems = [p for f in findings if f["kind"] == "invalid_frontmatter" for p in f["problems"]]
            self.assertIn("confidence/last_validated present but unparseable", problems)

    def test_audit_non_numeric_confidence_flags_problem_not_decay(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, mirror = fixture.project()
            (live / "MEMORY.md").write_text("[N](broken.md)\n", encoding="utf-8", newline="\n")
            (live / "broken.md").write_text(self._decay_note("broken", "high", "2026-01-01"), encoding="utf-8", newline="\n")
            for path in live.iterdir():
                (mirror / path.name).write_bytes(path.read_bytes())
            result = self.run_audit(fixture)
            findings = json.loads(result.stdout)["findings"]
            self.assertFalse([f for f in findings if f["kind"] == "decayed_confidence"])
            problems = [p for f in findings if f["kind"] == "invalid_frontmatter" for p in f["problems"]]
            self.assertIn("confidence/last_validated present but unparseable", problems)

    def test_audit_only_one_of_pair_present_no_flag(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, mirror = fixture.project()
            (live / "MEMORY.md").write_text("[N](half.md)\n", encoding="utf-8", newline="\n")
            (live / "half.md").write_text(note("half", extra="  confidence: 0.1\n"), encoding="utf-8", newline="\n")
            for path in live.iterdir():
                (mirror / path.name).write_bytes(path.read_bytes())
            result = self.run_audit(fixture)
            findings = json.loads(result.stdout)["findings"]
            self.assertFalse([f for f in findings if f["kind"] == "decayed_confidence"])
            problems = [p for f in findings if f["kind"] == "invalid_frontmatter" for p in f["problems"]]
            self.assertNotIn("confidence/last_validated present but unparseable", problems)

    # -- triage_project() ---------------------------------------------------

    def test_triage_flags_just_below_threshold(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, _ = fixture.project(mirror=False)
            (live / "MEMORY.md").write_text("- [D](decayed.md)\n", encoding="utf-8", newline="\n")
            (live / "decayed.md").write_text(self._decay_note("decayed", 1.0, "2026-01-01"), encoding="utf-8", newline="\n")
            now = (dt.date(2026, 1, 1) + dt.timedelta(days=157)).isoformat()
            result = self.run_triage(fixture, "--now", now)
            payload = json.loads(result.stdout)
            flagged = {item["path"]: item["reasons"] for item in payload["flagged"]}
            self.assertIn("decayed.md", flagged)
            self.assertTrue(any(r.startswith("decayed_confidence") for r in flagged["decayed.md"]))

    def test_triage_does_not_flag_just_above_threshold(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, _ = fixture.project(mirror=False)
            (live / "MEMORY.md").write_text("- [F](fresh.md)\n", encoding="utf-8", newline="\n")
            (live / "fresh.md").write_text(self._decay_note("fresh", 1.0, "2026-01-01"), encoding="utf-8", newline="\n")
            now = (dt.date(2026, 1, 1) + dt.timedelta(days=150)).isoformat()
            result = self.run_triage(fixture, "--now", now)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["summary"]["flagged"], 0)

    def test_triage_unparseable_last_validated_no_flag_no_crash(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, _ = fixture.project(mirror=False)
            (live / "MEMORY.md").write_text("- [B](broken.md)\n", encoding="utf-8", newline="\n")
            (live / "broken.md").write_text(self._decay_note("broken", 0.9, "not-a-date"), encoding="utf-8", newline="\n")
            result = self.run_triage(fixture)
            self.assertEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["summary"]["flagged"], 0)

    def test_triage_non_numeric_confidence_no_flag_no_crash(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, _ = fixture.project(mirror=False)
            (live / "MEMORY.md").write_text("- [B](broken.md)\n", encoding="utf-8", newline="\n")
            (live / "broken.md").write_text(self._decay_note("broken", "high", "2026-01-01"), encoding="utf-8", newline="\n")
            result = self.run_triage(fixture)
            self.assertEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["summary"]["flagged"], 0)

    def test_triage_only_one_of_pair_present_no_flag(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, _ = fixture.project(mirror=False)
            (live / "MEMORY.md").write_text("- [H](half.md)\n", encoding="utf-8", newline="\n")
            (live / "half.md").write_text(note("half", extra="  last_validated: 2020-01-01\n"), encoding="utf-8", newline="\n")
            result = self.run_triage(fixture)
            self.assertEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["summary"]["flagged"], 0)


class AuditHelperTests(unittest.TestCase):
    """Pure-helper coverage for the index and frontmatter machinery."""

    def test_effective_index_size_matches_load_accounting(self):
        text = "---\nname: idx\n---\n<!-- hidden\ncomment -->\nline one\nline two\n"
        lines, size = audit.effective_index_size(text)
        # Frontmatter and the HTML comment are stripped before counting.
        self.assertEqual(lines, 3)  # blank line left by the stripped comment + two lines
        self.assertEqual(size, len("\nline one\nline two\n".encode()))

    def test_index_reconcile_append_restriction(self):
        # The dream pass appends only the notes it created; corpus-wide healing of
        # pre-existing unindexed notes is a separately gated step (fix mode).
        with tempfile.TemporaryDirectory() as temp:
            memory = Path(temp) / "memory"
            memory.mkdir()
            (memory / "MEMORY.md").write_text("- [a](a.md)\n", encoding="utf-8", newline="\n")
            for name in ("a", "b", "new"):
                (memory / f"{name}.md").write_text(note(name), encoding="utf-8", newline="\n")
            records = audit.scan_project_notes(memory)
            restricted = audit.compute_index_reconcile("p", memory, records, restrict_appends_to={"new.md"})
            self.assertEqual(restricted["appended"], ["new.md"])  # b.md left for fix mode
            healing = audit.compute_index_reconcile("p", memory, records)
            self.assertEqual(healing["appended"], ["b.md", "new.md"])

    def test_description_similarity_content_words(self):
        self.assertGreaterEqual(
            audit.description_similarity(
                "reusable MLX serving gotchas from the champion trial",
                "reusable MLX serving lessons from the champion trial",
            ),
            0.6,
        )
        self.assertLess(
            audit.description_similarity(
                "reusable operational gotcha worth recalling",
                "durable sizing rule derived in the trial",
            ),
            0.3,
        )
        self.assertEqual(audit.description_similarity("", "anything"), 0.0)

    def test_loaded_index_text_truncates_like_the_session_loader(self):
        text = "---\nname: idx\n---\n<!-- hidden -->\n" + "line\n" * 250
        loaded = audit.loaded_index_text(text)
        # Frontmatter/comments stripped, then hard cut at the line cap: entries
        # past it are invisible to a real session, so judges never see them.
        self.assertEqual(len(loaded.splitlines()), config.INDEX_LOAD_MAX_LINES)
        big = "---\nname: idx\n---\n" + ("y" * 90 + "\n") * 400
        self.assertLessEqual(len(audit.loaded_index_text(big).encode()), config.INDEX_LOAD_MAX_BYTES)

    def test_refresh_index_lines_only_sole_target_canonical_lines(self):
        index = "- [A](a.md): stale hook\n- mixed [A](a.md) and [B](b.md)\n- [B](b.md)\n"
        entry = {"a.md": {"old": ("A", "stale hook"), "new": ("A", "fresh hook"), "force": False}}
        refreshed = audit.refresh_index_lines(index, entry)
        self.assertIn("- [A](a.md): fresh hook\n", refreshed)
        self.assertIn("- mixed [A](a.md) and [B](b.md)\n", refreshed)  # mixed line untouched
        self.assertIn("- [B](b.md)\n", refreshed)  # unrefreshed line untouched

    def test_refresh_index_lines_preserves_hand_edited_lines(self):
        # A line that deviates from the canonical rendering of the note's pre-rewrite
        # frontmatter is hand-edited: never clobber it...
        index = "- [A](a.md): stale hook; NOTE also see runbook.md steps\n"
        entry = {"a.md": {"old": ("A", "stale hook"), "new": ("A", "fresh hook"), "force": False}}
        self.assertEqual(audit.refresh_index_lines(index, entry), index)
        # ...unless the action is redescribe, whose approved purpose is exactly to
        # replace the routing hook (and the index diff is in the preview).
        forced = {"a.md": {"old": ("A", "stale hook"), "new": ("A", "fresh hook"), "force": True}}
        self.assertEqual(audit.refresh_index_lines(index, forced), "- [A](a.md): fresh hook\n")

    def test_index_reconcile_strips_dead_fragment_from_packed_line(self):
        with tempfile.TemporaryDirectory() as temp:
            memory = Path(temp)
            (memory / "live.md").write_text("---\nname: live\ndescription: live note here\nmetadata:\n  type: project\n---\nBody.\n", encoding="utf-8", newline="\n")
            (memory / "MEMORY.md").write_text("# idx\n"
            "- Packed: [Live](live.md) still here; [Dead](dead.md) gone note; [Live2](live.md) again\n"
            "- [dead2](dead2.md): a whole-dead line\n", encoding="utf-8", newline="\n")
            records = {"live.md": {}}
            plan = audit.compute_index_reconcile("proj", memory, records, restrict_appends_to=set())
            self.assertEqual(plan["dropped"], ["dead2.md"])
            self.assertEqual(plan["fragment_dropped"], ["dead.md"])
            out = audit.render_index_reconcile((memory / "MEMORY.md").read_text(encoding="utf-8"), memory, plan)
            self.assertIn("- Packed: [Live](live.md) still here; [Live2](live.md) again\n", out)
            self.assertNotIn("dead.md", out)
            self.assertNotIn("dead2.md", out)

    def test_strip_dead_index_fragments_conservative_on_mixed_fragment(self):
        # A single fragment carrying both a dead and a live link is left intact.
        line = "- Packed: [A](a.md) and [Dead](dead.md) together; [B](b.md) alone\n"
        out = audit.strip_dead_index_fragments(line, {"dead.md"})
        self.assertEqual(out, line)

    def test_preserve_metadata_extra_nested_inserts_and_replaces(self):
        donor = (
            "---\nname: a\ndescription: donor description with enough words\nmetadata:\n"
            "  node_type: memory\n  type: project\n  originSessionId: sess-a\n---\nBody.\n"
        )
        drafter = "---\nname: a\ndescription: merged description with enough words\nmetadata:\n  type: project\n---\nMerged.\n"
        merged = audit.preserve_metadata(drafter, donor, {"originSessionIds": "sess-a, sess-b"})
        self.assertIn("originSessionId: sess-a\n", merged)
        self.assertIn("  originSessionIds: sess-a, sess-b\n", merged)
        again = audit.preserve_metadata(merged, merged, {"originSessionIds": "sess-c"})
        self.assertIn("  originSessionIds: sess-c\n", again)
        self.assertNotIn("sess-a, sess-b", again)  # replaced, not duplicated

    def test_preserve_metadata_normalizes_flat_schema_donor(self):
        # A legacy flat-schema donor is normalized during consolidation: its
        # harness fields are lifted (values verbatim) into a nested metadata
        # block, so results follow the canonical shape and extra_nested fields
        # are no longer silently dropped.
        donor = "---\nname: a\ndescription: legacy flat schema donor note here\ntype: project\noriginSessionId: sess-a\n---\nBody.\n"
        drafter = "---\nname: a\ndescription: merged description with enough words\nmetadata:\n  type: reference\n---\nMerged.\n"
        merged = audit.preserve_metadata(drafter, donor, {"originSessionIds": "sess-a, sess-b"})
        # Assert through the parser, not substring presence: the block must sit
        # INSIDE the --- delimiters or the parser drops it (the original bug
        # appended it after the closing delimiter).
        meta, error, _body, _raw = audit.parse_frontmatter(merged)
        self.assertIsNone(error)
        nested = meta.get("metadata")
        self.assertIsInstance(nested, dict)
        self.assertEqual(nested.get("type"), "reference")  # drafter's type overlays lifted donor type
        self.assertEqual(nested.get("originSessionId"), "sess-a")  # lifted, value preserved
        self.assertEqual(nested.get("originSessionIds"), "sess-a, sess-b")  # extra_nested lands
        fm = merged.split("---")[1]
        self.assertNotRegex(fm, r"(?m)^type:")  # no top-level survivors
        self.assertNotRegex(fm, r"(?m)^originSessionId:")

    def test_preserve_metadata_flat_normalization_is_lift_only(self):
        # Lift-only: fields the donor lacks (node_type here) are never invented,
        # and unknown top-level fields stay top-level.
        donor = "---\nname: a\ndescription: legacy flat schema donor note here\ntype: project\ncustom_field: kept\n---\nBody.\n"
        drafter = "---\nname: a\ndescription: merged description with enough words\nmetadata:\n  type: project\n---\nMerged.\n"
        merged = audit.preserve_metadata(drafter, donor)
        meta, error, _body, _raw = audit.parse_frontmatter(merged)
        self.assertIsNone(error)
        self.assertEqual((meta.get("metadata") or {}).get("type"), "project")
        self.assertNotIn("node_type", merged)
        self.assertIn("custom_field: kept\n", merged)
        fm = merged.split("---")[1]
        self.assertNotRegex(fm, r"(?m)^type:")

    def test_origin_session_id_both_schemas(self):
        nested = "---\nname: a\ndescription: nested schema note with words\nmetadata:\n  originSessionId: sess-n\n  type: project\n---\nB.\n"
        flat = "---\nname: a\ndescription: flat schema note with words\ntype: project\noriginSessionId: sess-f\n---\nB.\n"
        self.assertEqual(audit.origin_session_id(nested), "sess-n")
        self.assertEqual(audit.origin_session_id(flat), "sess-f")
        self.assertIsNone(audit.origin_session_id("no frontmatter at all"))

    def test_redescribe_content_replaces_only_description(self):
        donor = "---\nname: a\ndescription: old stale hook\nmetadata:\n  type: project\n---\nBody stays.\n"
        rebuilt = audit.redescribe_content(donor, "closed ticket #7 with the gate green fix")
        self.assertIsNotNone(rebuilt)
        self.assertIn('description: "closed ticket #7 with the gate green fix"\n', rebuilt)  # quoted: contains '#'
        self.assertIn("Body stays.\n", rebuilt)
        self.assertEqual(
            rebuilt.replace('description: "closed ticket #7 with the gate green fix"', "description: old stale hook"),
            donor,
        )

    def test_redescribe_content_neutralizes_newline_injection(self):
        # The redescribe description is the only drafter-controlled scalar that
        # reaches frontmatter raw from JSON; an embedded newline must never become
        # an injected frontmatter line.
        donor = "---\nname: a\ndescription: old\nmetadata:\n  type: project\n---\nBody.\n"
        rebuilt = audit.redescribe_content(donor, "innocent\ninjectedKey: attacker value")
        self.assertNotIn("\ninjectedKey", rebuilt)
        metadata, error, body, _raw = audit.parse_frontmatter(rebuilt)
        self.assertIsNone(error)
        self.assertEqual(body, "Body.")
        self.assertEqual(sorted(metadata.keys()), ["description", "metadata", "name"])
        self.assertIsNone(audit.redescribe_content(donor, " \n "))  # whitespace-only rejected

    def test_unbalanced_paren_description_flags_truncated(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            live, _ = fixture.project()
            content = note("a").replace(
                "description: Fixture note describing a in detail",
                "description: Fixed the gateway stamp (issue",
            )
            (live / "a.md").write_text(content, encoding="utf-8", newline="\n")
            (live / "MEMORY.md").write_text("- [A](a.md)\n", encoding="utf-8", newline="\n")
            result = subprocess.run(
                [
                    sys.executable, "-m", "memory_dream", "audit",
                    "--live-root", str(fixture.live), "--mirror-root", str(fixture.mirror),
                    "--format", "json",
                ],
                text=True, capture_output=True, check=False,
                cwd=REPO_ROOT, env=_clean_env(fixture.claude_config_dir),
            )
            payload = json.loads(result.stdout)
            kinds = {finding["kind"] for finding in payload["findings"]}
            self.assertIn("truncated_description", kinds)


class TriageSuppressionTests(unittest.TestCase):
    def test_recently_applied_notes_suppressed(self):
        # A note an applied dream pass touched within the window is reported
        # under suppressed, not flagged (refire churn on already-consolidated notes).
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            memory = root / "live" / "proj" / "memory"
            memory.mkdir(parents=True)
            big_body = "RESOLVED\n" + ("x" * 7000)
            (memory / "big.md").write_text("---\nname: big\ndescription: a large consolidated note fixture here\n"
            "metadata:\n  type: project\n---\n" + big_body, encoding="utf-8", newline="\n")
            (memory / "MEMORY.md").write_text("# Index\n- [big](big.md) - fixture\n", encoding="utf-8", newline="\n")
            pass_dir = root / "passes" / "20260101-000000"
            pass_dir.mkdir(parents=True)
            (pass_dir / "manifest.json").write_text(json.dumps(
                {"proposals": [{"project": "proj", "results": [{"path": "big.md", "content": "x"}]}]}
            ), encoding="utf-8", newline="\n")
            (pass_dir / "apply-manifest.json").write_text("{}", encoding="utf-8", newline="\n")
            claude_config_dir = root / "claude-config"
            claude_config_dir.mkdir()
            env = _clean_env(claude_config_dir)
            env["MEMORY_DREAM_PASS_ROOT"] = str(root / "passes")
            out = subprocess.run(
                [sys.executable, "-m", "memory_dream", "triage", "--format", "json", "--live-root", str(root / "live")],
                capture_output=True, text=True, env=env, check=False, cwd=REPO_ROOT,
            )
            result = json.loads(out.stdout)
            self.assertEqual(result["summary"]["flagged"], 0)
            self.assertEqual(result["summary"]["suppressed_recently_applied"], 1)
            self.assertEqual(result["suppressed"][0]["path"], "big.md")
            out2 = subprocess.run(
                [
                    sys.executable, "-m", "memory_dream", "triage", "--format", "json",
                    "--live-root", str(root / "live"), "--suppress-applied-days", "0",
                ],
                capture_output=True, text=True, env=env, check=False, cwd=REPO_ROOT,
            )
            result2 = json.loads(out2.stdout)
            self.assertEqual(result2["summary"]["flagged"], 1)


def _flagged_note_fixture(root: Path, project: str = "proj", stem: str = "big") -> None:
    """One project with a single structurally-flagged note (oversized body),
    the same fixture shape TriageSuppressionTests uses."""
    memory = root / "live" / project / "memory"
    memory.mkdir(parents=True)
    big_body = "RESOLVED\n" + ("x" * 7000)
    (memory / f"{stem}.md").write_text(
        "---\nname: big\ndescription: a large consolidated note fixture here\n"
        "metadata:\n  type: project\n---\n" + big_body,
        encoding="utf-8", newline="\n",
    )
    (memory / "MEMORY.md").write_text(f"# Index\n- [big]({stem}.md) - fixture\n", encoding="utf-8", newline="\n")


def _write_rejections(pass_root: Path, entries: list[dict]) -> None:
    pass_root.mkdir(parents=True, exist_ok=True)
    (pass_root / "rejections.json").write_text(
        json.dumps({"schema_version": 1, "entries": entries}), encoding="utf-8", newline="\n"
    )


class TriageRejectionSuppressionTests(unittest.TestCase):
    """Rejection suppression mirrors the applied-side mechanism, reading
    apply's rejections.json (config.pass_root()/rejections.json) instead of
    applied patch-set manifests, and decays after --suppress-rejected-days
    (config.SUPPRESS_REJECTED_DAYS)."""

    def _run(self, root, env, *args):
        return subprocess.run(
            [
                sys.executable, "-m", "memory_dream", "triage", "--format", "json",
                "--live-root", str(root / "live"), *args,
            ],
            capture_output=True, text=True, env=env, check=False, cwd=REPO_ROOT,
        )

    def test_recent_rejection_suppressed_flagged_excludes_it(self):
        # Scenario 1: rejection recorded yesterday, window 14 -> suppressed;
        # flagged count excludes it; summary reports the rejected-suppression count.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _flagged_note_fixture(root)
            pass_root = root / "passes"
            claude_config_dir = root / "claude-config"
            claude_config_dir.mkdir()
            env = _clean_env(claude_config_dir)
            env["MEMORY_DREAM_PASS_ROOT"] = str(pass_root)
            now = dt.date(2026, 7, 17)
            recent = (now - dt.timedelta(days=1)).isoformat() + "T00:00:00+00:00"
            _write_rejections(pass_root, [
                {"recorded_at": recent, "patch_set_id": "ps1", "proposal_id": "prop1",
                 "project": "proj", "paths": ["big.md"]},
            ])
            result = json.loads(self._run(root, env, "--now", now.isoformat()).stdout)
            self.assertEqual(result["summary"]["flagged"], 0)
            self.assertEqual(result["summary"]["suppressed_recently_rejected"], 1)
            self.assertEqual(result["suppressed_rejected"][0]["path"], "big.md")

    def test_old_rejection_decays_and_reflags(self):
        # Scenario 2: rejection recorded 20 days ago -> not suppressed (decay).
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _flagged_note_fixture(root)
            pass_root = root / "passes"
            claude_config_dir = root / "claude-config"
            claude_config_dir.mkdir()
            env = _clean_env(claude_config_dir)
            env["MEMORY_DREAM_PASS_ROOT"] = str(pass_root)
            now = dt.date(2026, 7, 17)
            old = (now - dt.timedelta(days=20)).isoformat() + "T00:00:00+00:00"
            _write_rejections(pass_root, [
                {"recorded_at": old, "patch_set_id": "ps1", "proposal_id": "prop1",
                 "project": "proj", "paths": ["big.md"]},
            ])
            result = json.loads(self._run(root, env, "--now", now.isoformat()).stdout)
            self.assertEqual(result["summary"]["flagged"], 1)
            self.assertEqual(result["summary"]["suppressed_recently_rejected"], 0)
            self.assertEqual(result["suppressed_rejected"], [])

    def test_suppress_rejected_days_flag_disables_suppression(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _flagged_note_fixture(root)
            pass_root = root / "passes"
            claude_config_dir = root / "claude-config"
            claude_config_dir.mkdir()
            env = _clean_env(claude_config_dir)
            env["MEMORY_DREAM_PASS_ROOT"] = str(pass_root)
            now = dt.date(2026, 7, 17)
            recent = (now - dt.timedelta(days=1)).isoformat() + "T00:00:00+00:00"
            _write_rejections(pass_root, [
                {"recorded_at": recent, "patch_set_id": "ps1", "proposal_id": "prop1",
                 "project": "proj", "paths": ["big.md"]},
            ])
            result = json.loads(
                self._run(root, env, "--now", now.isoformat(), "--suppress-rejected-days", "0").stdout
            )
            self.assertEqual(result["summary"]["flagged"], 1)

    def test_no_rejections_file_behaves_as_today(self):
        # Scenario 7: absence of rejections.json is a no-op (covered generally by
        # every unmodified pre-existing triage test, which never create one; this
        # makes the claim explicit for the rejection-suppression code path itself).
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _flagged_note_fixture(root)
            claude_config_dir = root / "claude-config"
            claude_config_dir.mkdir()
            env = _clean_env(claude_config_dir)
            env["MEMORY_DREAM_PASS_ROOT"] = str(root / "passes")  # never created
            result = json.loads(self._run(root, env, "--now", "2026-07-17").stdout)
            self.assertEqual(result["summary"]["flagged"], 1)
            self.assertEqual(result["summary"]["suppressed_recently_rejected"], 0)
            self.assertEqual(result["suppressed_rejected"], [])

    def test_path_both_recently_applied_and_rejected_suppressed_once(self):
        # Scenario 3: no double count. Applied-side suppression runs first (it
        # already existed); a path caught there is never re-counted on the
        # rejected side, so the two suppression buckets never overlap.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _flagged_note_fixture(root)
            pass_root = root / "passes"
            claude_config_dir = root / "claude-config"
            claude_config_dir.mkdir()
            env = _clean_env(claude_config_dir)
            env["MEMORY_DREAM_PASS_ROOT"] = str(pass_root)
            applied_dir = pass_root / "20260101-000000"
            applied_dir.mkdir(parents=True)
            (applied_dir / "manifest.json").write_text(json.dumps(
                {"proposals": [{"project": "proj", "results": [{"path": "big.md", "content": "x"}]}]}
            ), encoding="utf-8", newline="\n")
            (applied_dir / "apply-manifest.json").write_text("{}", encoding="utf-8", newline="\n")
            now = dt.date(2026, 7, 17)
            recent = (now - dt.timedelta(days=1)).isoformat() + "T00:00:00+00:00"
            _write_rejections(pass_root, [
                {"recorded_at": recent, "patch_set_id": "ps1", "proposal_id": "prop1",
                 "project": "proj", "paths": ["big.md"]},
            ])
            result = json.loads(self._run(root, env, "--now", now.isoformat()).stdout)
            self.assertEqual(result["summary"]["flagged"], 0)
            self.assertEqual(result["summary"]["suppressed_recently_applied"], 1)
            self.assertEqual(result["summary"]["suppressed_recently_rejected"], 0)
            self.assertEqual(result["suppressed_rejected"], [])
            self.assertEqual(len(result["suppressed"]), 1)


class TriageDeferralStreakTests(unittest.TestCase):
    """Repeat-deferral visibility. deferral-streaks.json lives directly
    under pass_root() (a sibling of the dated pass dirs, like rejections.json)
    and is advanced once per DISTINCT newest pass dir (by report.json mtime),
    never by repeated triage runs against the same pass."""

    def _run(self, live_root, env, *args):
        return subprocess.run(
            [sys.executable, "-m", "memory_dream", "triage", "--format", "json",
             "--live-root", str(live_root), *args],
            capture_output=True, text=True, env=env, check=False, cwd=REPO_ROOT,
        )

    def _write_report(self, pass_root: Path, name: str, deferred: list[dict], mtime_offset: float) -> None:
        pass_dir = pass_root / name
        pass_dir.mkdir(parents=True)
        report_path = pass_dir / "report.json"
        report_path.write_text(json.dumps({"deferred": deferred}), encoding="utf-8", newline="\n")
        t = time.time() + mtime_offset
        os.utime(report_path, (t, t))

    def _env(self, root: Path, pass_root: Path) -> dict:
        claude_config_dir = root / "claude-config"
        claude_config_dir.mkdir(exist_ok=True)
        env = _clean_env(claude_config_dir)
        env["MEMORY_DREAM_PASS_ROOT"] = str(pass_root)
        return env

    def test_three_consecutive_passes_then_reset(self):
        # Scenario 4: a key deferred in 3 consecutive passes is named with
        # count 3; a subsequent pass without it resets (drops) the streak.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live = root / "live"
            live.mkdir()
            pass_root = root / "passes"
            env = self._env(root, pass_root)
            entry = {"project": "proj", "path": "note.md", "reason": "cluster-size-cap"}

            self._write_report(pass_root, "p1", [entry], 0)
            r1 = json.loads(self._run(live, env).stdout)
            # Count 1 is not yet a "repeat" (>=2 consecutive passes).
            self.assertEqual(r1["repeat_deferral"], [])

            self._write_report(pass_root, "p2", [entry], 5)
            r2 = json.loads(self._run(live, env).stdout)
            by_key2 = {(r["project"], r.get("path")): r["count"] for r in r2["repeat_deferral"]}
            self.assertEqual(by_key2[("proj", "note.md")], 2)

            # A second triage run against the SAME newest pass (p2) must not
            # inflate the streak further (idempotent per pass).
            r2b = json.loads(self._run(live, env).stdout)
            by_key2b = {(r["project"], r.get("path")): r["count"] for r in r2b["repeat_deferral"]}
            self.assertEqual(by_key2b[("proj", "note.md")], 2)

            self._write_report(pass_root, "p3", [entry], 10)
            r3 = json.loads(self._run(live, env).stdout)
            by_key3 = {(r["project"], r.get("path")): r["count"] for r in r3["repeat_deferral"]}
            self.assertEqual(by_key3[("proj", "note.md")], 3)
            human3 = self._run(live, env, "--format", "human").stdout
            self.assertIn("note.md", human3)
            self.assertIn("3", human3)
            self.assertTrue(human3.rstrip().endswith("flagged:0"))

            # Scenario: a pass without the key resets/removes it.
            self._write_report(pass_root, "p4", [], 15)
            r4 = json.loads(self._run(live, env).stdout)
            self.assertEqual(r4["repeat_deferral"], [])

    def test_cluster_size_cap_key_survives_member_growth(self):
        # Scenario 5: cluster-size-cap deferred entries carry a bare path, so
        # a cluster growing by one member (a different note's own path is
        # unaffected) still matches on that unchanged (project, path) pair.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live = root / "live"
            live.mkdir()
            pass_root = root / "passes"
            env = self._env(root, pass_root)
            entry = {"project": "proj", "path": "overflow.md", "reason": "cluster-size-cap"}
            self._write_report(pass_root, "p1", [entry], 0)
            json.loads(self._run(live, env).stdout)
            # p2's cluster grew by one member (an unrelated extra note also
            # deferred), but overflow.md's own path is unchanged.
            self._write_report(
                pass_root, "p2",
                [entry, {"project": "proj", "path": "another.md", "reason": "cluster-size-cap"}],
                5,
            )
            r2 = json.loads(self._run(live, env).stdout)
            by_key = {(r["project"], r.get("path")): r["count"] for r in r2["repeat_deferral"]}
            self.assertEqual(by_key[("proj", "overflow.md")], 2)

    def test_per_pass_cap_cluster_id_fallback_documented_fidelity_limit(self):
        # LEGACY reports only: per-pass-cap entries written before member
        # paths were persisted carry ONLY cluster_id. cluster_id is a hash of
        # the member path set (assemble.stable_id), so for these entries a
        # cluster that gains or loses a member gets a NEW cluster_id and the
        # streak does NOT continue across a membership change. Current builds
        # persist paths (assemble.py per-pass-cap deferral) and take the
        # overlap-matching path tested below; this documents the legacy
        # fidelity limit rather than hiding it.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live = root / "live"
            live.mkdir()
            pass_root = root / "passes"
            env = self._env(root, pass_root)
            self._write_report(
                pass_root, "p1",
                [{"project": "proj", "cluster_id": "cidA", "reason": "per-pass-cap"}], 0,
            )
            r1 = json.loads(self._run(live, env).stdout)
            self.assertEqual(r1["repeat_deferral"], [])
            # Same logical cluster, but its member set (and therefore its
            # stable_id-derived cluster_id) changed between passes.
            self._write_report(
                pass_root, "p2",
                [{"project": "proj", "cluster_id": "cidB", "reason": "per-pass-cap"}], 5,
            )
            r2 = json.loads(self._run(live, env).stdout)
            # cidA's streak is dropped (reset); cidB starts fresh at 1, so
            # nothing reaches the >=2 "repeat" threshold this pass.
            self.assertEqual(r2["repeat_deferral"], [])

    def test_per_pass_cap_paths_overlap_continues_streak_across_membership_change(self):
        # Current builds persist a deferred cluster's member paths, so streak
        # identity is path-set OVERLAP: the same logical cluster keeps its
        # streak even though gaining a member changed its cluster_id hash.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live = root / "live"
            live.mkdir()
            pass_root = root / "passes"
            env = self._env(root, pass_root)
            self._write_report(
                pass_root, "p1",
                [{"project": "proj", "cluster_id": "cidA", "paths": ["a.md", "b.md"], "reason": "per-pass-cap"}], 0,
            )
            r1 = json.loads(self._run(live, env).stdout)
            self.assertEqual(r1["repeat_deferral"], [])
            self._write_report(
                pass_root, "p2",
                [{"project": "proj", "cluster_id": "cidB", "paths": ["a.md", "b.md", "c.md"], "reason": "per-pass-cap"}], 5,
            )
            r2 = json.loads(self._run(live, env).stdout)
            clusters = [r for r in r2["repeat_deferral"] if r.get("cluster_id")]
            self.assertEqual(len(clusters), 1)
            self.assertEqual(clusters[0]["count"], 2)
            self.assertEqual(clusters[0]["cluster_id"], "cidB")
            self.assertEqual(clusters[0]["paths"], ["a.md", "b.md", "c.md"])

    def test_deleting_old_pass_dirs_preserves_streak_file(self):
        # Scenario 6: pruning dated pass dirs (the retention advisory's
        # suggested cleanup) must not affect the durable streak file, which
        # lives directly under pass_root(), a sibling of the dated dirs.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live = root / "live"
            live.mkdir()
            pass_root = root / "passes"
            env = self._env(root, pass_root)
            entry = {"project": "proj", "path": "note.md", "reason": "cluster-size-cap"}
            self._write_report(pass_root, "p1", [entry], 0)
            json.loads(self._run(live, env).stdout)
            self._write_report(pass_root, "p2", [entry], 5)
            r2 = json.loads(self._run(live, env).stdout)
            by_key2 = {(r["project"], r.get("path")): r["count"] for r in r2["repeat_deferral"]}
            self.assertEqual(by_key2[("proj", "note.md")], 2)

            streak_path = pass_root / "deferral-streaks.json"
            self.assertTrue(streak_path.is_file())
            shutil.rmtree(pass_root / "p1")
            shutil.rmtree(pass_root / "p2")
            self.assertTrue(streak_path.is_file())

            r3 = json.loads(self._run(live, env).stdout)
            by_key3 = {(r["project"], r.get("path")): r["count"] for r in r3["repeat_deferral"]}
            self.assertEqual(by_key3[("proj", "note.md")], 2)


if __name__ == "__main__":
    unittest.main()
