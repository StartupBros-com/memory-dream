#!/usr/bin/env python3
"""New-surface coverage for the memory-dream extraction (v0.1).

Covers: snapshot backup + restore round trip, --consent token mode,
transcript schema-probe loud failure, config resolution order,
compat.FileLock contention, and cli-as-file invocation.

Doctor-focused coverage (exit codes, index-cap compatibility record,
compaction canary, config overrides, patch-set/preview-copy retention,
aggregate drift line / --strict) lives in test_doctor.py.

House rules (see docs/EXTRACTION-DESIGN.md "Tests" and the port task brief):
  - stdlib unittest only, no model calls, no network.
  - every subprocess env sets CLAUDE_CONFIG_DIR to a per-test temp dir and
    strips MEMORY_DREAM_* / CLAUDE_MEMORY_* / CLAUDE_JOB_DIR from the outer
    environment, so a developer's real ~/.claude/memory-dream.json (or a
    leaked scratch/job dir) can never leak into a test.
  - in-process tests that read module-level config state save and restore it
    in finally/tearDown.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from memory_dream import audit as AUDIT  # noqa: E402
from memory_dream import compat, config, transcript  # noqa: E402

# --- Environment isolation helpers ------------------------------------------

_ENV_PREFIXES_TO_STRIP = ("MEMORY_DREAM_", "CLAUDE_MEMORY_")
_ENV_EXACT_TO_STRIP = ("CLAUDE_JOB_DIR",)


def _strip_leaky_keys(env: dict) -> dict:
    return {
        key: value
        for key, value in env.items()
        if not key.startswith(_ENV_PREFIXES_TO_STRIP) and key not in _ENV_EXACT_TO_STRIP
    }


def subprocess_env(claude_config_dir: Path) -> dict:
    """env = {**os.environ minus leaky keys, CLAUDE_CONFIG_DIR: <tmp>}."""
    env = _strip_leaky_keys(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir)
    return env


def run_cli(*args: str, cwd: Path = REPO_ROOT, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "memory_dream", *args],
        cwd=str(cwd),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


# --- Fixture helper, stolen from the old suite ------------------------------
# (claude/tests/test_memory_dream_apply.py:note) — same frontmatter shape the
# assembler produces and apply/audit expect on disk.


def note(name="note", body="Body.", note_type="project"):
    return (
        "---\n"
        f"name: {name}\n"
        f"description: Fixture note describing {name} in detail\n"
        f"metadata:\n  type: {note_type}\n"
        "---\n"
        f"{body}\n"
    )


# =============================================================================
# 1. Snapshot backup + restore round trip
# =============================================================================


class SnapshotBackupRestoreRoundTripTests(unittest.TestCase):
    """No --mirror-root (snapshot mode); --consent token to avoid needing a
    transcript fixture. Round trip covers a rewrite (old.md), a brand-new
    file apply creates (extract.md, restore must DELETE it), and a delete
    apply performs (cold.md, restore must recreate its bytes)."""

    def _build(self, root: Path):
        live_root = root / "live"
        memory_dir = live_root / "proj" / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "MEMORY.md").write_text("- [old](old.md): Fixture note describing old in detail\n"
        "- [cold](cold.md): Fixture note describing cold in detail\n", encoding="utf-8", newline="\n")
        (memory_dir / "old.md").write_text(note("old", body="Topic one. Topic two."), encoding="utf-8", newline="\n")
        (memory_dir / "cold.md").write_text(note("cold", body="Cold truth, superseded."), encoding="utf-8", newline="\n")

        proposals = [
            {
                "id": "split1",
                "project": "proj",
                "action": "split",
                "survivor": "old.md",
                "sources": [{"path": "old.md", "digest": AUDIT.digest(memory_dir / "old.md")}],
                "results": [
                    {"path": "old.md", "content": note("old", body="Topic one, rewritten.")},
                    {"path": "extract.md", "content": note("extract", body="Topic two, extracted.")},
                ],
                "deletes": [],
                "justification": "split the mega-note",
                "sensitive": False,
            },
            {
                "id": "close1",
                "project": "proj",
                "action": "period-close",
                "survivor": None,
                "sources": [{"path": "cold.md", "digest": AUDIT.digest(memory_dir / "cold.md")}],
                "results": [],
                "deletes": ["cold.md"],
                "justification": "superseded",
                "sensitive": False,
            },
        ]
        manifest_id = AUDIT.content_id(proposals)
        patch_set = root / "logs" / "patchset"
        patch_set.mkdir(parents=True)
        manifest = {
            "schema_version": 1,
            "id": manifest_id,
            "created_at_line": 0,
            "proposals": proposals,
        }
        (patch_set / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        selection = {"approved": ["split1", "close1"], "patch_set_id": manifest_id}
        selection_path = root / "selection.json"
        selection_path.write_text(json.dumps(selection), encoding="utf-8", newline="\n")
        return live_root, memory_dir, patch_set, selection_path

    def test_apply_snapshot_backup_then_restore_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, memory_dir, patch_set, selection_path = self._build(root)
            pre_old = (memory_dir / "old.md").read_bytes()
            pre_cold = (memory_dir / "cold.md").read_bytes()
            pre_index = (memory_dir / "MEMORY.md").read_bytes()

            env = subprocess_env(root / "claude-config")
            applied = run_cli(
                "apply",
                "--patch-set", str(patch_set),
                "--selection", str(selection_path),
                "--live-root", str(live_root),
                "--consent", "token",
                "--acknowledge-reduced-consent-check",
                env=env,
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertIn("DREAM-APPLY-COMPLETE", applied.stdout)
            # Live state changed: cold.md gone, extract.md created, old.md rewritten.
            self.assertFalse((memory_dir / "cold.md").exists())
            self.assertTrue((memory_dir / "extract.md").exists())
            self.assertIn("rewritten", (memory_dir / "old.md").read_text(encoding="utf-8"))

            # backup/ holds pre-images + a manifest recording every touched path.
            backup_root = patch_set / "backup"
            backup_manifest_path = backup_root / "backup-manifest.json"
            self.assertTrue(backup_manifest_path.is_file())
            backup_manifest = json.loads(backup_manifest_path.read_text(encoding="utf-8"))
            entries = {(e["project"], e["path"]): e["sha256"] for e in backup_manifest["entries"]}
            self.assertEqual(entries[("proj", "old.md")], hashlib.sha256(pre_old).hexdigest())
            self.assertEqual(entries[("proj", "cold.md")], hashlib.sha256(pre_cold).hexdigest())
            self.assertEqual(entries[("proj", "MEMORY.md")], hashlib.sha256(pre_index).hexdigest())
            # extract.md did not exist before this apply: recorded with sha256 null.
            self.assertIn(("proj", "extract.md"), entries)
            self.assertIsNone(entries[("proj", "extract.md")])
            self.assertEqual((backup_root / "proj" / "old.md").read_bytes(), pre_old)
            self.assertEqual((backup_root / "proj" / "cold.md").read_bytes(), pre_cold)
            self.assertFalse((backup_root / "proj" / "extract.md").exists())

            restored = run_cli(
                "restore",
                "--patch-set", str(patch_set),
                "--live-root", str(live_root),
                env=env,
            )
            self.assertEqual(restored.returncode, 0, restored.stderr)
            self.assertIn("RESTORE-COMPLETE", restored.stdout)
            # Live bytes are back to pre-apply state...
            self.assertEqual((memory_dir / "old.md").read_bytes(), pre_old)
            self.assertEqual((memory_dir / "cold.md").read_bytes(), pre_cold)
            self.assertEqual((memory_dir / "MEMORY.md").read_bytes(), pre_index)
            # ...including deletion of the apply-created file.
            self.assertFalse((memory_dir / "extract.md").exists())


# =============================================================================
# 2. Consent token mode
# =============================================================================


class ConsentTokenModeTests(unittest.TestCase):
    def _build(self, root: Path):
        live_root = root / "live"
        memory_dir = live_root / "proj" / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "MEMORY.md").write_text("- [old](old.md): Fixture note describing old in detail\n", encoding="utf-8", newline="\n")
        (memory_dir / "old.md").write_text(note("old"), encoding="utf-8", newline="\n")
        proposals = [
            {
                "id": "p1",
                "project": "proj",
                "action": "period-close",
                "survivor": None,
                "sources": [{"path": "old.md", "digest": AUDIT.digest(memory_dir / "old.md")}],
                "results": [],
                "deletes": ["old.md"],
                "justification": "superseded",
                "sensitive": False,
            }
        ]
        manifest_id = AUDIT.content_id(proposals)
        patch_set = root / "logs" / "patchset"
        patch_set.mkdir(parents=True)
        manifest = {
            "schema_version": 1,
            "id": manifest_id,
            "created_at_line": 0,
            "proposals": proposals,
        }
        (patch_set / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        return live_root, memory_dir, patch_set, manifest_id

    def _selection(self, root: Path, manifest_id: str, patch_set_id=None) -> Path:
        selection = {
            "approved": ["p1"],
            "patch_set_id": patch_set_id if patch_set_id is not None else manifest_id,
        }
        path = root / "selection.json"
        path.write_text(json.dumps(selection), encoding="utf-8", newline="\n")
        return path

    def test_token_without_acknowledge_refuses(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, memory_dir, patch_set, manifest_id = self._build(root)
            selection_path = self._selection(root, manifest_id)
            env = subprocess_env(root / "claude-config")
            result = run_cli(
                "apply",
                "--patch-set", str(patch_set),
                "--selection", str(selection_path),
                "--live-root", str(live_root),
                "--consent", "token",
                env=env,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(
                "acknowledge-reduced-consent-check" in result.stderr or "SECURITY.md" in result.stderr,
                result.stderr,
            )
            self.assertTrue((memory_dir / "old.md").exists())  # refused before any live write

    def test_token_with_both_flags_proceeds_past_consent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, memory_dir, patch_set, manifest_id = self._build(root)
            selection_path = self._selection(root, manifest_id)
            env = subprocess_env(root / "claude-config")
            result = run_cli(
                "apply",
                "--patch-set", str(patch_set),
                "--selection", str(selection_path),
                "--live-root", str(live_root),
                "--consent", "token",
                "--acknowledge-reduced-consent-check",
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((memory_dir / "old.md").exists())

    def test_token_mode_still_verifies_patch_set_id_match(self):
        # "Wrong token": the selection's patch_set_id (the operator-typed token
        # in token mode) does not match the manifest's content-bound id.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_root, memory_dir, patch_set, manifest_id = self._build(root)
            selection_path = self._selection(root, manifest_id, patch_set_id="wrong-token-value")
            env = subprocess_env(root / "claude-config")
            result = run_cli(
                "apply",
                "--patch-set", str(patch_set),
                "--selection", str(selection_path),
                "--live-root", str(live_root),
                "--consent", "token",
                "--acknowledge-reduced-consent-check",
                env=env,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("patch_set_id does not match", result.stderr)
            self.assertTrue((memory_dir / "old.md").exists())


# =============================================================================
# 3. Transcript schema probe + loud failure
# =============================================================================


class TranscriptSchemaTests(unittest.TestCase):
    def test_unrecognized_message_role_raises(self):
        entry = {"type": "user", "message": {"role": "assistant", "content": "oops"}}
        with self.assertRaises(transcript.TranscriptSchemaError):
            transcript.extract_user_text(entry)

    def test_non_dict_message_raises(self):
        entry = {"type": "user", "message": "not-a-dict"}
        with self.assertRaises(transcript.TranscriptSchemaError):
            transcript.extract_user_text(entry)

    def test_non_dict_content_block_raises(self):
        entry = {"type": "user", "message": {"role": "user", "content": ["not-a-block-dict"]}}
        with self.assertRaises(transcript.TranscriptSchemaError):
            transcript.extract_user_text(entry)

    def test_ismeta_entry_skipped_silently(self):
        entry = {"type": "user", "isMeta": True, "message": {"role": "user", "content": "hidden"}}
        self.assertIsNone(transcript.extract_user_text(entry))

    def test_non_user_entry_skipped_silently(self):
        entry = {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        }
        self.assertIsNone(transcript.extract_user_text(entry))

    def test_schema_probe_recognizes_well_formed_transcript(self):
        with tempfile.TemporaryDirectory() as temp:
            tdir = Path(temp)
            lines = [
                {"type": "user", "message": {"role": "user", "content": "run the dream pass"}},
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "preview"}]}},
                {"type": "user", "message": {"role": "user", "content": "approve all xyz"}},
            ]
            (tdir / "sess.jsonl").write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8", newline="\n")
            probe = transcript.schema_probe(tdir)
            self.assertTrue(probe.startswith("recognized"), probe)
            self.assertIn("2 user turn(s)", probe)

    def test_schema_probe_loud_failure_on_bad_shape(self):
        with tempfile.TemporaryDirectory() as temp:
            tdir = Path(temp)
            lines = [
                {"type": "user", "message": {"role": "user", "content": "ok"}},
                {"type": "user", "message": {"role": "nope"}},  # unrecognized shape
            ]
            (tdir / "sess.jsonl").write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8", newline="\n")
            probe = transcript.schema_probe(tdir)
            self.assertTrue(probe.startswith("UNRECOGNIZED"), probe)

    def test_schema_probe_missing_directory(self):
        probe = transcript.schema_probe(Path(tempfile.gettempdir()) / "mem-dream-test-nonexistent-dir-xyz")
        self.assertIn("does not exist", probe)


# =============================================================================
# 4. Config resolution
# =============================================================================


class ConfigResolutionTests(unittest.TestCase):
    def setUp(self):
        self._env_backup = dict(os.environ)
        stripped = _strip_leaky_keys(os.environ)
        os.environ.clear()
        os.environ.update(stripped)
        self._triage_body_bytes_backup = config.TRIAGE_BODY_BYTES
        self._file_config_loaded_backup = config._FILE_CONFIG_LOADED

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_backup)
        config.TRIAGE_BODY_BYTES = self._triage_body_bytes_backup
        config._FILE_CONFIG_LOADED = self._file_config_loaded_backup

    def test_env_var_beats_default_for_pass_root(self):
        with tempfile.TemporaryDirectory() as temp:
            claude_dir = Path(temp) / "claude-config"
            claude_dir.mkdir()
            os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
            self.assertEqual(config.pass_root(), claude_dir / "logs" / "memory-dream" / "passes")

            custom = Path(temp) / "custom-passes"
            os.environ["MEMORY_DREAM_PASS_ROOT"] = str(custom)
            self.assertEqual(config.pass_root(), custom)

    def test_config_file_overrides_threshold(self):
        with tempfile.TemporaryDirectory() as temp:
            claude_dir = Path(temp) / "claude-config"
            claude_dir.mkdir()
            (claude_dir / "memory-dream.json").write_text(json.dumps({"triage_body_bytes": 1234}), encoding="utf-8", newline="\n")
            os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
            config._FILE_CONFIG_LOADED = False
            config.load_file_config()
            self.assertEqual(config.TRIAGE_BODY_BYTES, 1234)

    def test_unknown_config_key_exits_with_naming_error(self):
        with tempfile.TemporaryDirectory() as temp:
            claude_dir = Path(temp) / "claude-config"
            claude_dir.mkdir()
            (claude_dir / "memory-dream.json").write_text(json.dumps({"totally_bogus_key": 1}), encoding="utf-8", newline="\n")
            os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
            config._FILE_CONFIG_LOADED = False
            with self.assertRaises(SystemExit) as cm:
                config.load_file_config()
            self.assertIn("unknown config key", str(cm.exception))
            self.assertIn("totally_bogus_key", str(cm.exception))

    def test_mirror_root_default_is_none(self):
        with tempfile.TemporaryDirectory() as temp:
            claude_dir = Path(temp) / "claude-config"
            claude_dir.mkdir()
            os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
            self.assertIsNone(config.default_mirror_root())


class ConfigNonDefaultValuesTests(unittest.TestCase):
    """config.non_default_values(): every _OVERRIDABLE name whose live value
    differs from its shipped default (config._DEFAULTS), with source
    attribution following the same env-beats-file precedence
    _apply_env_overrides()/load_file_config() already enforce."""

    def setUp(self):
        self._env_backup = dict(os.environ)
        stripped = _strip_leaky_keys(os.environ)
        os.environ.clear()
        os.environ.update(stripped)
        self._merge_jaccard_backup = config.MERGE_JACCARD
        self._file_config_loaded_backup = config._FILE_CONFIG_LOADED

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_backup)
        config.MERGE_JACCARD = self._merge_jaccard_backup
        config._FILE_CONFIG_LOADED = self._file_config_loaded_backup

    def test_no_overrides_reports_empty(self):
        with tempfile.TemporaryDirectory() as temp:
            claude_dir = Path(temp) / "claude-config"
            claude_dir.mkdir()
            os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
            self.assertEqual(config.non_default_values(), {})

    def test_env_override_reports_env_source(self):
        with tempfile.TemporaryDirectory() as temp:
            claude_dir = Path(temp) / "claude-config"
            claude_dir.mkdir()
            os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
            os.environ["MEMORY_DREAM_MERGE_JACCARD"] = "0.9"
            config._FILE_CONFIG_LOADED = False
            config.load_file_config()
            overrides = config.non_default_values()
            self.assertIn("MERGE_JACCARD", overrides)
            current, default, source = overrides["MERGE_JACCARD"]
            self.assertEqual(current, 0.9)
            self.assertEqual(default, config._DEFAULTS["MERGE_JACCARD"])
            self.assertIn("MEMORY_DREAM_MERGE_JACCARD", source)

    def test_file_override_reports_file_source(self):
        with tempfile.TemporaryDirectory() as temp:
            claude_dir = Path(temp) / "claude-config"
            claude_dir.mkdir()
            (claude_dir / "memory-dream.json").write_text(
                json.dumps({"merge_jaccard": 0.75}), encoding="utf-8", newline="\n"
            )
            os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
            config._FILE_CONFIG_LOADED = False
            config.load_file_config()
            overrides = config.non_default_values()
            self.assertIn("MERGE_JACCARD", overrides)
            current, default, source = overrides["MERGE_JACCARD"]
            self.assertEqual(current, 0.75)
            self.assertIn("merge_jaccard", source)
            self.assertNotIn("MEMORY_DREAM_MERGE_JACCARD", source)

    def test_env_wins_over_file_for_source_naming(self):
        with tempfile.TemporaryDirectory() as temp:
            claude_dir = Path(temp) / "claude-config"
            claude_dir.mkdir()
            (claude_dir / "memory-dream.json").write_text(
                json.dumps({"merge_jaccard": 0.75}), encoding="utf-8", newline="\n"
            )
            os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
            os.environ["MEMORY_DREAM_MERGE_JACCARD"] = "0.9"
            config._FILE_CONFIG_LOADED = False
            config.load_file_config()
            overrides = config.non_default_values()
            current, default, source = overrides["MERGE_JACCARD"]
            self.assertEqual(current, 0.9)
            self.assertIn("MEMORY_DREAM_MERGE_JACCARD", source)


# =============================================================================
# 5. compat.FileLock
# =============================================================================


class FileLockTests(unittest.TestCase):
    def test_second_lock_raises_lock_held_while_first_open(self):
        with tempfile.TemporaryDirectory() as temp:
            lock_path = Path(temp) / "test.lock"
            with compat.FileLock(lock_path):
                with self.assertRaises(compat.LockHeld):
                    with compat.FileLock(lock_path):
                        pass  # pragma: no cover - must not be reached

    def test_lock_reacquirable_after_release(self):
        with tempfile.TemporaryDirectory() as temp:
            lock_path = Path(temp) / "test.lock"
            with compat.FileLock(lock_path):
                pass
            # Released: a fresh acquisition must succeed without raising.
            with compat.FileLock(lock_path):
                pass


# =============================================================================
# 6. CLI-as-file invocation (the plugin invocation path)
# =============================================================================


class CliFileInvocationTests(unittest.TestCase):
    def test_version_from_a_different_cwd(self):
        cli_path = REPO_ROOT / "memory_dream" / "cli.py"
        with tempfile.TemporaryDirectory() as temp:
            other_cwd = Path(temp) / "elsewhere"
            other_cwd.mkdir()
            env = subprocess_env(Path(temp) / "claude-config")
            result = subprocess.run(
                [sys.executable, str(cli_path), "--version"],
                cwd=str(other_cwd),
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("memory-dream", result.stdout)

    def test_subcommand_from_a_different_cwd(self):
        cli_path = REPO_ROOT / "memory_dream" / "cli.py"
        with tempfile.TemporaryDirectory() as temp:
            other_cwd = Path(temp) / "elsewhere"
            other_cwd.mkdir()
            live_root = Path(temp) / "live"
            live_root.mkdir()
            env = subprocess_env(Path(temp) / "claude-config")
            result = subprocess.run(
                [sys.executable, str(cli_path), "doctor", "--live-root", str(live_root)],
                cwd=str(other_cwd),
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertIn("doctor:", result.stdout)


if __name__ == "__main__":
    unittest.main()
