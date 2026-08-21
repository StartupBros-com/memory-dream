#!/usr/bin/env python3
"""Doctor-focused coverage for the memory-dream extraction (v0.1).

Split out of test_extraction_surfaces.py (code-review finding): that file
had grown past 1400 lines by mixing new-surface coverage with the much
larger `doctor` test surface. This file carries everything doctor-adjacent:

  - `doctor` exit codes (healthy layout, missing live root, index-cap
    unverifiable when `claude` is absent from PATH) and the cwd-slug helper
    doctor's transcript lookup depends on.
  - the index-cap compatibility record: `cli._detect_installed_claude_version`
    probe behavior, `cli._index_cap_check`, and doctor's "index cap" line.
  - `transcript.compaction_canary` and doctor's "compaction canary" line.
  - `cli._config_overrides_check` / `config.non_default_values()` and
    doctor's "config overrides" line.
  - patch-set retention (`cli._stale_patch_sets`,
    `cli._patch_set_retention_check`) and preview-copy retention
    (`cli._wsl_windows_homes`, `cli._preview_copy_retention_check`), plus
    doctor's "patch-set retention" and "preview copy" lines.
  - doctor's trailing "drift: ..." aggregate line, the `--strict` exit-code
    flag, and the "readiness" advisory line.
  - the compatibility-record single-source guard: the measured version
    literal must appear in exactly one file.

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

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path, PureWindowsPath

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from memory_dream import cli, config, transcript  # noqa: E402

# --- Environment isolation helpers ------------------------------------------
# Duplicated from test_extraction_surfaces.py: used by both files' remaining
# content, and these are private module helpers (not part of the package
# under test), so each file keeps its own copy rather than importing across
# test modules.

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
# 7. doctor
# =============================================================================


def _doctor_line(stdout: str, needle: str) -> str:
    for line in stdout.splitlines():
        if needle in line:
            return line
    raise AssertionError(f"no doctor line contains {needle!r} in:\n{stdout}")


class TranscriptSlugTests(unittest.TestCase):
    def test_cwd_slug_never_escapes_projects_dir(self):
        # Windows drive colons and backslashes must be replaced too: the slug
        # is always one relative component, so ``projects / slug`` can never
        # resolve outside the config directory (the v0.1.0 Windows bug).
        self.assertEqual(
            transcript.cwd_slug(PureWindowsPath("C:\\Users\\x\\proj.app")),
            "C--Users-x-proj-app",
        )
        self.assertEqual(transcript.cwd_slug(Path("/home/x/proj.app")), "-home-x-proj-app")


class DoctorTests(unittest.TestCase):
    def test_healthy_layout_exits_zero(self):
        with tempfile.TemporaryDirectory() as temp:
            claude_dir = Path(temp) / "claude-config"
            projects = claude_dir / "projects"
            (projects / "some-project" / "memory").mkdir(parents=True)
            # transcripts_dir_for slugs the cwd; doctor's subcommand runs with
            # cwd=REPO_ROOT per the house rule for subprocess CLI calls.
            transcripts_dir = projects / transcript.cwd_slug(REPO_ROOT)
            transcripts_dir.mkdir(parents=True)
            (transcripts_dir / "session.jsonl").write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "hello"}}) + "\n", encoding="utf-8", newline="\n")
            env = subprocess_env(claude_dir)
            result = run_cli("doctor", cwd=REPO_ROOT, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(_doctor_line(result.stdout, "live root").startswith("ok"))
            self.assertTrue(_doctor_line(result.stdout, "consent trace").startswith("ok"))
            self.assertIn("recognized", _doctor_line(result.stdout, "consent trace"))

    def test_missing_live_root_exits_one(self):
        with tempfile.TemporaryDirectory() as temp:
            claude_dir = Path(temp) / "claude-config"
            claude_dir.mkdir()  # deliberately no "projects" subdirectory
            env = subprocess_env(claude_dir)
            result = run_cli("doctor", cwd=REPO_ROOT, env=env)
            self.assertEqual(result.returncode, 1)
            self.assertTrue(_doctor_line(result.stdout, "live root").startswith("FAIL"))

    def test_index_cap_line_unverifiable_when_claude_absent_from_path(self):
        # Full integration path: PATH scoped to a dir with no `claude` binary
        # on it, so shutil.which("claude") inside the real doctor subprocess
        # genuinely finds nothing. Exit code must stay 0 -- the check is
        # advisory even when it cannot verify anything.
        with tempfile.TemporaryDirectory() as temp:
            claude_dir = Path(temp) / "claude-config"
            (claude_dir / "projects").mkdir(parents=True)
            empty_path_dir = Path(temp) / "empty-path"
            empty_path_dir.mkdir()
            env = subprocess_env(claude_dir)
            env["PATH"] = str(empty_path_dir)
            result = run_cli("doctor", cwd=REPO_ROOT, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            line = _doctor_line(result.stdout, "index cap")
            self.assertTrue(line.startswith("ok") or line.startswith("warn"), line)
            self.assertIn("unverifiable", line.lower())


class DoctorCompactionCanaryLineTests(unittest.TestCase):
    """`doctor`'s "compaction canary" advisory line: verified on a healthy
    fixture, "warn" (never FAIL) when the flag keys are renamed, and never
    affects doctor's default exit code either way."""

    def test_compaction_canary_verified_on_healthy_layout(self):
        with tempfile.TemporaryDirectory() as temp:
            claude_dir = Path(temp) / "claude-config"
            projects = claude_dir / "projects"
            transcripts_dir = projects / transcript.cwd_slug(REPO_ROOT)
            transcripts_dir.mkdir(parents=True)
            lines = [
                {"type": "user", "message": {"role": "user", "content": "hello"}},
                {
                    "type": "user",
                    "isCompactSummary": True,
                    "isVisibleInTranscriptOnly": True,
                    "message": {
                        "role": "user",
                        "content": (
                            "This session is being continued from a previous conversation "
                            "that ran out of context."
                        ),
                    },
                },
            ]
            (transcripts_dir / "session.jsonl").write_text(
                "\n".join(json.dumps(line) for line in lines), encoding="utf-8", newline="\n"
            )
            env = subprocess_env(claude_dir)
            result = run_cli("doctor", cwd=REPO_ROOT, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            line = _doctor_line(result.stdout, "compaction canary")
            self.assertTrue(line.startswith("ok"), line)
            self.assertIn("verified", line)

    def test_compaction_canary_warns_on_drift_but_exit_stays_zero(self):
        with tempfile.TemporaryDirectory() as temp:
            claude_dir = Path(temp) / "claude-config"
            projects = claude_dir / "projects"
            transcripts_dir = projects / transcript.cwd_slug(REPO_ROOT)
            transcripts_dir.mkdir(parents=True)
            # Boilerplate-shaped turn with the flag keys renamed -- the v0.2.1
            # incident shape: extract_user_text would treat this as an
            # ordinary operator turn.
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
            env = subprocess_env(claude_dir)
            result = run_cli("doctor", cwd=REPO_ROOT, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            line = _doctor_line(result.stdout, "compaction canary")
            self.assertTrue(line.startswith("warn"), line)
            self.assertIn("drift", line)

    def test_compaction_canary_unverified_when_no_sample(self):
        with tempfile.TemporaryDirectory() as temp:
            claude_dir = Path(temp) / "claude-config"
            projects = claude_dir / "projects"
            transcripts_dir = projects / transcript.cwd_slug(REPO_ROOT)
            transcripts_dir.mkdir(parents=True)
            (transcripts_dir / "session.jsonl").write_text(
                json.dumps({"type": "user", "message": {"role": "user", "content": "hello"}}) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            env = subprocess_env(claude_dir)
            result = run_cli("doctor", cwd=REPO_ROOT, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            line = _doctor_line(result.stdout, "compaction canary")
            self.assertTrue(line.startswith("ok"), line)
            self.assertIn("unverified — no compaction sample", line)


# =============================================================================
# 8. index-cap compatibility record (single source of the measured-version claim)
# =============================================================================


def _write_executable(path: Path, script: str, windows_script: str) -> Path:
    """Write a runnable stub for the version probe. POSIX gets `script` as a
    shebang shell file at `path`; Windows gets `windows_script` as
    `path.with_suffix(".bat")` because CreateProcess cannot execute shebang
    text files (WinError 193). Callers keep passing the extensionless `path`
    to the probe -- shutil.which resolves the .bat twin via PATHEXT, the same
    way a real `claude.cmd` install resolves."""
    if os.name == "nt":
        bat = path.with_suffix(".bat")
        bat.write_text(windows_script, encoding="utf-8", newline="\r\n")
        return path
    path.write_text(script, encoding="utf-8", newline="\n")
    path.chmod(0o755)
    return path


class InstalledVersionProbeTests(unittest.TestCase):
    """cli._detect_installed_claude_version: every failure mode degrades to
    None ("unverifiable"), never an exception, never a hang."""

    def test_reports_version_parsed_from_stdout(self):
        # An arbitrary version distinct from config.COMPATIBILITY_RECORD's,
        # to prove this parses whatever the binary prints rather than
        # coincidentally matching the record.
        with tempfile.TemporaryDirectory() as temp:
            stub = _write_executable(
                Path(temp) / "claude",
                "#!/bin/sh\necho '9.8.7 (Claude Code)'\nexit 0\n",
                "@echo 9.8.7 (Claude Code)\n@exit /b 0\n",
            )
            self.assertEqual(cli._detect_installed_claude_version(binary=str(stub)), "9.8.7")

    def test_absent_binary_returns_none(self):
        self.assertIsNone(cli._detect_installed_claude_version(binary="definitely-not-a-real-claude-binary-xyz"))

    def test_nonzero_exit_returns_none(self):
        with tempfile.TemporaryDirectory() as temp:
            stub = _write_executable(
                Path(temp) / "claude",
                "#!/bin/sh\necho 'boom' >&2\nexit 1\n",
                "@echo boom 1>&2\n@exit /b 1\n",
            )
            self.assertIsNone(cli._detect_installed_claude_version(binary=str(stub)))

    def test_garbage_output_returns_none(self):
        with tempfile.TemporaryDirectory() as temp:
            stub = _write_executable(
                Path(temp) / "claude",
                "#!/bin/sh\necho 'banana'\nexit 0\n",
                "@echo banana\n@exit /b 0\n",
            )
            self.assertIsNone(cli._detect_installed_claude_version(binary=str(stub)))

    def test_hang_past_timeout_returns_none_promptly(self):
        with tempfile.TemporaryDirectory() as temp:
            stub = _write_executable(
                Path(temp) / "claude",
                "#!/bin/sh\nsleep 30\necho '9.8.7'\n",
                "@ping -n 31 127.0.0.1 > nul\n@echo 9.8.7\n",
            )
            started = time.monotonic()
            result = cli._detect_installed_claude_version(binary=str(stub), timeout=0.3)
            elapsed = time.monotonic() - started
            self.assertIsNone(result)
            self.assertLess(elapsed, 5.0, "probe did not return promptly on timeout")


class IndexCapCheckTests(unittest.TestCase):
    def test_match_is_ok_and_names_both_versions(self):
        record_version = config.COMPATIBILITY_RECORD["claude_code_version"]
        label, ok, detail, fatal = cli._index_cap_check(record_version)
        self.assertEqual(label, "index cap")
        self.assertTrue(ok)
        self.assertFalse(fatal)
        self.assertIn(record_version, detail)

    def test_mismatch_is_advisory_not_ok_and_names_both_versions(self):
        record_version = config.COMPATIBILITY_RECORD["claude_code_version"]
        label, ok, detail, fatal = cli._index_cap_check("9.9.9")
        self.assertEqual(label, "index cap")
        self.assertFalse(ok)
        self.assertFalse(fatal)  # advisory: never fatal, never changes doctor's exit contract
        self.assertIn(record_version, detail)
        self.assertIn("9.9.9", detail)

    def test_unverifiable_when_version_undetected(self):
        label, ok, detail, fatal = cli._index_cap_check(None)
        self.assertEqual(label, "index cap")
        self.assertTrue(ok)  # advisory-ok: unverifiable never fails doctor
        self.assertFalse(fatal)
        self.assertIn("unverifiable", detail.lower())


class ConfigOverridesCheckTests(unittest.TestCase):
    """cli._config_overrides_check: the doctor "config overrides"
    (label, ok, detail, fatal) tuple, built from an already-computed
    config.non_default_values() mapping so the summary formatting is
    directly unit-testable without mutating real config state."""

    def test_empty_overrides_reports_all_default(self):
        label, ok, detail, fatal = cli._config_overrides_check({})
        self.assertEqual(label, "config overrides")
        self.assertTrue(ok)
        self.assertFalse(fatal)
        self.assertIn("none", detail)
        self.assertIn("all defaults", detail)

    def test_overrides_listed_with_source(self):
        overrides = {"MERGE_JACCARD": (0.9, 0.5, "env:MEMORY_DREAM_MERGE_JACCARD")}
        label, ok, detail, fatal = cli._config_overrides_check(overrides)
        self.assertEqual(label, "config overrides")
        self.assertTrue(ok)
        self.assertFalse(fatal)
        self.assertIn("MERGE_JACCARD", detail)
        self.assertIn("0.9", detail)
        self.assertIn("0.5", detail)
        self.assertIn("MEMORY_DREAM_MERGE_JACCARD", detail)


# =============================================================================
# 9. compaction canary (transcript.compaction_canary)
# =============================================================================


_COMPACTION_BOILERPLATE_TEXT = (
    "This session is being continued from a previous conversation that ran out "
    "of context. The user was shown patch set ps-42 and replied: approve all ps-42"
)


def _compaction_shaped_turn(flags: dict | None = None, content: str | None = None) -> dict:
    entry: dict = {
        "type": "user",
        "message": {"role": "user", "content": content if content is not None else _COMPACTION_BOILERPLATE_TEXT},
    }
    if flags:
        entry.update(flags)
    return entry


def _write_jsonl(path: Path, *entries: dict, mtime: float | None = None) -> Path:
    path.write_text("\n".join(json.dumps(entry) for entry in entries), encoding="utf-8", newline="\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class CompactionCanaryTests(unittest.TestCase):
    """transcript.compaction_canary: samples the newest 5 *.jsonl files for a
    compaction-shaped turn and asserts the consent-gate flag keys on it."""

    def test_verified_when_flag_carrying_compaction_turn_present(self):
        with tempfile.TemporaryDirectory() as temp:
            tdir = Path(temp)
            _write_jsonl(
                tdir / "sess.jsonl",
                {"type": "user", "message": {"role": "user", "content": "run it"}},
                _compaction_shaped_turn({"isCompactSummary": True, "isVisibleInTranscriptOnly": True}),
            )
            status, detail = transcript.compaction_canary(tdir)
            self.assertEqual(status, "verified", detail)

    def test_drift_when_boilerplate_turn_missing_flag_keys(self):
        # The v0.2.1-incident shape: flag keys renamed upstream, so the compaction-shaped turn
        # (matched only on the documented boilerplate prefix) no longer
        # carries isCompactSummary / isVisibleInTranscriptOnly.
        with tempfile.TemporaryDirectory() as temp:
            tdir = Path(temp)
            _write_jsonl(
                tdir / "sess.jsonl",
                {"type": "user", "message": {"role": "user", "content": "run it"}},
                _compaction_shaped_turn({"isSummaryRenamedUpstream": True}),
            )
            status, detail = transcript.compaction_canary(tdir)
            self.assertEqual(status, "drift", detail)

    def test_unverified_when_no_compaction_turn_in_sample(self):
        # No compaction sample at all -- must report "unverified",
        # never "drift".
        with tempfile.TemporaryDirectory() as temp:
            tdir = Path(temp)
            _write_jsonl(
                tdir / "sess.jsonl",
                {"type": "user", "message": {"role": "user", "content": "run it"}},
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "preview"}]}},
            )
            status, detail = transcript.compaction_canary(tdir)
            self.assertEqual(status, "unverified")
            self.assertIn("unverified — no compaction sample", detail)

    def test_unverified_on_missing_directory(self):
        status, detail = transcript.compaction_canary(
            Path(tempfile.gettempdir()) / "mem-dream-canary-test-nonexistent-dir-xyz"
        )
        self.assertEqual(status, "unverified")
        self.assertIn("unverified — no compaction sample", detail)

    def test_unverified_on_empty_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            status, detail = transcript.compaction_canary(Path(temp))
            self.assertEqual(status, "unverified")
            self.assertIn("unverified — no compaction sample", detail)

    def test_sample_bound_respected_sixth_newest_file_ignored(self):
        # A compaction turn in the 6th-newest file only must not be found:
        # the 5-file bound stays as-is.
        with tempfile.TemporaryDirectory() as temp:
            tdir = Path(temp)
            base = time.time() - 1000
            _write_jsonl(
                tdir / "f0-oldest.jsonl",
                _compaction_shaped_turn({"isCompactSummary": True}),
                mtime=base,
            )
            for i in range(1, 6):
                _write_jsonl(
                    tdir / f"f{i}.jsonl",
                    {"type": "user", "message": {"role": "user", "content": f"turn {i}"}},
                    mtime=base + i,
                )
            status, detail = transcript.compaction_canary(tdir)
            self.assertEqual(status, "unverified", detail)


# =============================================================================
# 10. config overrides (doctor)
# =============================================================================


class DoctorConfigOverridesLineTests(unittest.TestCase):
    """`doctor`'s "config overrides" advisory line: reports every config
    value whose live value differs from its shipped default, naming the
    override source (env var or JSON config key), env beating file per the
    same resolution order load_file_config() enforces. Always advisory:
    never affects doctor's exit code."""

    def _healthy_env(self, temp: Path) -> tuple[dict, Path]:
        claude_dir = Path(temp) / "claude-config"
        (claude_dir / "projects").mkdir(parents=True)
        return subprocess_env(claude_dir), claude_dir

    def test_no_overrides_reports_all_default(self):
        with tempfile.TemporaryDirectory() as temp:
            env, _ = self._healthy_env(Path(temp))
            result = run_cli("doctor", cwd=REPO_ROOT, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            line = _doctor_line(result.stdout, "config overrides")
            self.assertTrue(line.startswith("ok"), line)
            self.assertIn("none", line)
            self.assertIn("all defaults", line)

    def test_env_override_listed_with_env_source(self):
        with tempfile.TemporaryDirectory() as temp:
            env, _ = self._healthy_env(Path(temp))
            env["MEMORY_DREAM_MERGE_JACCARD"] = "0.9"
            result = run_cli("doctor", cwd=REPO_ROOT, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            line = _doctor_line(result.stdout, "config overrides")
            self.assertIn("MERGE_JACCARD", line)
            self.assertIn("0.9", line)
            self.assertIn("MEMORY_DREAM_MERGE_JACCARD", line)

    def test_file_override_listed_with_file_source(self):
        with tempfile.TemporaryDirectory() as temp:
            env, claude_dir = self._healthy_env(Path(temp))
            (claude_dir / "memory-dream.json").write_text(
                json.dumps({"merge_jaccard": 0.75}), encoding="utf-8", newline="\n"
            )
            result = run_cli("doctor", cwd=REPO_ROOT, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            line = _doctor_line(result.stdout, "config overrides")
            self.assertIn("MERGE_JACCARD", line)
            self.assertIn("0.75", line)
            self.assertIn("merge_jaccard", line)
            self.assertNotIn("MEMORY_DREAM_MERGE_JACCARD", line)

    def test_env_wins_over_file_for_source_naming(self):
        with tempfile.TemporaryDirectory() as temp:
            env, claude_dir = self._healthy_env(Path(temp))
            (claude_dir / "memory-dream.json").write_text(
                json.dumps({"merge_jaccard": 0.75}), encoding="utf-8", newline="\n"
            )
            env["MEMORY_DREAM_MERGE_JACCARD"] = "0.9"
            result = run_cli("doctor", cwd=REPO_ROOT, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            line = _doctor_line(result.stdout, "config overrides")
            self.assertIn("MERGE_JACCARD", line)
            self.assertIn("0.9", line)
            self.assertIn("MEMORY_DREAM_MERGE_JACCARD", line)


# =============================================================================
# 11. patch-set retention & preview-copy leftover (doctor)
# =============================================================================


class StalePatchSetsTests(unittest.TestCase):
    """cli._stale_patch_sets: read-only discovery of patch-set directories
    older than the retention window -- feeds the doctor "patch-set
    retention" advisory. Never deletes anything."""

    def test_missing_root_reports_no_stale_sets(self):
        missing = Path(tempfile.gettempdir()) / "mem-dream-retention-test-missing-xyz"
        self.assertEqual(cli._stale_patch_sets(missing, 90), [])

    def test_empty_root_reports_no_stale_sets(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(cli._stale_patch_sets(Path(temp), 90), [])

    def test_dir_older_than_window_is_stale_and_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = root / "2026-01-01-ps"
            old.mkdir()
            (old / "preview.html").write_text("stale", encoding="utf-8")
            old_time = time.time() - 91 * 86400
            os.utime(old, (old_time, old_time))
            stale = cli._stale_patch_sets(root, 90)
            self.assertEqual(stale, [old])
            # advisory only -- nothing deleted
            self.assertTrue(old.is_dir())
            self.assertTrue((old / "preview.html").is_file())

    def test_dir_within_window_is_not_stale(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            recent = root / "2026-08-01-ps"
            recent.mkdir()
            recent_time = time.time() - 10 * 86400
            os.utime(recent, (recent_time, recent_time))
            self.assertEqual(cli._stale_patch_sets(root, 90), [])


class PatchSetRetentionCheckTests(unittest.TestCase):
    """cli._patch_set_retention_check: the doctor "patch-set retention"
    (label, ok, detail, fatal) tuple built from an already-computed stale
    list -- same advisory shape as the "staging leftovers" crash-leftover
    check (ok=False/"warn" when something is found, fatal always False,
    since nothing here ever deletes a patch set)."""

    def test_no_stale_sets_reports_clean(self):
        label, ok, detail, fatal = cli._patch_set_retention_check([])
        self.assertEqual(label, "patch-set retention")
        self.assertTrue(ok)
        self.assertFalse(fatal)
        self.assertIn("none", detail)

    def test_stale_sets_report_count_and_total_size(self):
        with tempfile.TemporaryDirectory() as temp:
            ps1 = Path(temp) / "ps-1"
            ps1.mkdir()
            (ps1 / "preview.html").write_bytes(b"x" * 500)
            ps2 = Path(temp) / "ps-2"
            ps2.mkdir()
            (ps2 / "notes.json").write_bytes(b"y" * 250)
            label, ok, detail, fatal = cli._patch_set_retention_check([ps1, ps2])
            self.assertEqual(label, "patch-set retention")
            self.assertFalse(ok)  # advisory warn -- something to review
            self.assertFalse(fatal)  # never fatal: nothing here deletes
            self.assertIn("2 patch set", detail)
            self.assertIn("750", detail)


class WslWindowsHomesTests(unittest.TestCase):
    """cli._wsl_windows_homes: the Windows-home enumeration shared by
    open-preview and the doctor "preview copy" check. `users_root` is a
    parameter so tests never touch the real /mnt/c/Users, and the helper
    never shells out (a cmd.exe interop hang here would make every `doctor`
    invocation slow, not just `open-preview`)."""

    def test_missing_base_returns_no_candidates(self):
        missing = Path(tempfile.gettempdir()) / "mem-dream-wsl-homes-test-missing-xyz"
        self.assertEqual(cli._wsl_windows_homes(missing), [])

    def test_base_dir_lists_real_users_excluding_system_pseudo_accounts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "alice").mkdir()
            (root / "Public").mkdir()
            (root / "Default").mkdir()
            (root / "Default User").mkdir()
            (root / "All Users").mkdir()
            homes = cli._wsl_windows_homes(root)
            names = {h.name for h in homes}
            self.assertEqual(names, {"alice"})


class PreviewCopyRetentionCheckTests(unittest.TestCase):
    """cli._preview_copy_retention_check: the doctor "preview copy"
    (label, ok, detail, fatal) tuple built from an already-resolved homes
    list -- reports a leftover copy open-preview left behind, never removes
    one."""

    def test_no_candidate_homes_reports_nothing_to_check(self):
        label, ok, detail, fatal = cli._preview_copy_retention_check([])
        self.assertEqual(label, "preview copy")
        self.assertTrue(ok)
        self.assertFalse(fatal)

    def test_homes_without_leftover_report_clean(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "alice"
            home.mkdir()
            label, ok, detail, fatal = cli._preview_copy_retention_check([home])
            self.assertEqual(label, "preview copy")
            self.assertTrue(ok)
            self.assertFalse(fatal)
            self.assertIn("none", detail)

    def test_leftover_copy_is_reported_advisory_not_fatal(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "alice"
            home.mkdir()
            (home / "memory-dream-preview.html").write_text("<html></html>", encoding="utf-8")
            label, ok, detail, fatal = cli._preview_copy_retention_check([home])
            self.assertEqual(label, "preview copy")
            self.assertFalse(ok)
            self.assertFalse(fatal)
            self.assertIn("memory-dream-preview.html", detail)


class DoctorPatchSetRetentionLineTests(unittest.TestCase):
    """Full `memory-dream doctor` integration: the "patch-set retention"
    line reports overdue patch sets by count and size and never deletes
    anything; missing/empty pass root reports clean; the default 90-day
    window applies with no override set."""

    def test_default_retention_window_is_90_days(self):
        self.assertEqual(config.PATCH_SET_RETENTION_DAYS, 90)

    def test_no_pass_root_reports_clean(self):
        with tempfile.TemporaryDirectory() as temp:
            claude_dir = Path(temp) / "claude-config"
            (claude_dir / "projects").mkdir(parents=True)
            env = subprocess_env(claude_dir)
            result = run_cli("doctor", cwd=REPO_ROOT, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            line = _doctor_line(result.stdout, "patch-set retention")
            self.assertTrue(line.startswith("ok"), line)
            self.assertIn("none", line)

    def test_stale_patch_set_reported_by_count_and_size_and_not_deleted(self):
        with tempfile.TemporaryDirectory() as temp:
            claude_dir = Path(temp) / "claude-config"
            (claude_dir / "projects").mkdir(parents=True)
            pass_root = claude_dir / "logs" / "memory-dream" / "passes"
            old = pass_root / "2026-01-01-ps"
            old.mkdir(parents=True)
            (old / "preview.html").write_text("stale content", encoding="utf-8")
            old_time = time.time() - 95 * 86400
            os.utime(old, (old_time, old_time))
            env = subprocess_env(claude_dir)
            result = run_cli("doctor", cwd=REPO_ROOT, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            line = _doctor_line(result.stdout, "patch-set retention")
            self.assertTrue(line.startswith("warn"), line)
            self.assertIn("1 patch set", line)
            self.assertIn("90 days", line)
            # advisory only -- nothing deleted by running doctor
            self.assertTrue(old.is_dir())
            self.assertTrue((old / "preview.html").is_file())


class DoctorPreviewCopyLineTests(unittest.TestCase):
    """Full `memory-dream doctor` integration: the "preview copy" line is
    always present and advisory-only (never affects doctor's exit code)."""

    def test_preview_copy_line_present_and_advisory(self):
        with tempfile.TemporaryDirectory() as temp:
            claude_dir = Path(temp) / "claude-config"
            (claude_dir / "projects").mkdir(parents=True)
            env = subprocess_env(claude_dir)
            result = run_cli("doctor", cwd=REPO_ROOT, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            line = _doctor_line(result.stdout, "preview copy")
            self.assertTrue(line.startswith("ok") or line.startswith("warn"), line)


# =============================================================================
# 11a. doctor aggregate drift line, --strict, and the readiness line (U5)
# =============================================================================


def _ambient_preview_copy_label() -> str | None:
    """None if this host's real "preview copy" check is currently clean;
    otherwise "preview copy" -- the label doctor would add to its drift
    line. A leftover memory-dream-preview.html under a real /mnt/c/Users
    is host state, not something a fixture controls, so aggregate-line
    assertions below build their expected string through this rather than
    assuming a clean host on every machine that runs this suite."""
    _label, ok, _detail, _fatal = cli._preview_copy_retention_check(cli._wsl_windows_homes())
    return None if ok else "preview copy"


def _expected_drift_line(*forced_labels: str) -> str:
    """The doctor "drift: ..." line a fixture forcing `forced_labels`
    should produce, in the same order `_run_doctor` appends checks,
    folding in this host's ambient "preview copy" state (see
    `_ambient_preview_copy_label`)."""
    order = ["compaction canary", "staging leftovers", "patch-set retention", "preview copy", "index cap"]
    ambient = _ambient_preview_copy_label()
    present = set(forced_labels) | ({ambient} if ambient else set())
    labels = [label for label in order if label in present]
    if not labels:
        return "drift: none"
    return f"drift: {len(labels)} ({', '.join(labels)})"


class DoctorAggregateAndStrictTests(unittest.TestCase):
    """`doctor`'s trailing "drift: ..." aggregate line (always the last
    line of output) and the `--strict` flag. Drift is recorded only for
    advisories that reported a concrete, unexpected finding: "unverifiable"
    (index cap with no `claude` on PATH), "no sample" (compaction canary
    with no transcripts), and "nothing found because a precondition is
    absent" (consent trace on a fresh checkout -- explicitly documented as
    not drift) never count, and neither does a deliberate config override
    (always ok=True already). The default (flag-less) exit code
    never changes because of drift; only `--strict` recomputes it from the
    recorded drift advisories."""

    def _healthy_env(self, temp: Path) -> dict:
        claude_dir = Path(temp) / "claude-config"
        projects = claude_dir / "projects"
        (projects / "some-project" / "memory").mkdir(parents=True)
        transcripts_dir = projects / transcript.cwd_slug(REPO_ROOT)
        transcripts_dir.mkdir(parents=True)
        (transcripts_dir / "session.jsonl").write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "hello"}}) + "\n",
            encoding="utf-8", newline="\n",
        )
        env = subprocess_env(claude_dir)
        # Scope PATH so a real `claude` binary on the host running this suite
        # (whose version may not match config.COMPATIBILITY_RECORD) can never
        # nondeterministically add "index cap" to these fixtures' drift line --
        # same technique as the pinned
        # test_index_cap_line_unverifiable_when_claude_absent_from_path.
        empty_path_dir = Path(temp) / "empty-path"
        empty_path_dir.mkdir(exist_ok=True)
        env["PATH"] = str(empty_path_dir)
        return env

    def test_drift_none_on_healthy_layout(self):
        with tempfile.TemporaryDirectory() as temp:
            env = self._healthy_env(Path(temp))
            result = run_cli("doctor", cwd=REPO_ROOT, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            lines = result.stdout.rstrip().splitlines()
            self.assertEqual(lines[-1], _expected_drift_line())

    def test_strict_exits_zero_when_nothing_drifted(self):
        with tempfile.TemporaryDirectory() as temp:
            env = self._healthy_env(Path(temp))
            result = run_cli("doctor", "--strict", cwd=REPO_ROOT, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_stale_patch_set_is_drift_but_default_exit_stays_zero(self):
        with tempfile.TemporaryDirectory() as temp:
            env = self._healthy_env(Path(temp))
            claude_dir = Path(env["CLAUDE_CONFIG_DIR"])
            pass_root = claude_dir / "logs" / "memory-dream" / "passes"
            old = pass_root / "2026-01-01-ps"
            old.mkdir(parents=True)
            (old / "preview.html").write_text("stale content", encoding="utf-8")
            old_time = time.time() - 95 * 86400
            os.utime(old, (old_time, old_time))

            result = run_cli("doctor", cwd=REPO_ROOT, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)  # drift never touches the default exit
            lines = result.stdout.rstrip().splitlines()
            self.assertEqual(lines[-1], _expected_drift_line("patch-set retention"))

    def test_strict_exits_nonzero_when_something_drifted(self):
        with tempfile.TemporaryDirectory() as temp:
            env = self._healthy_env(Path(temp))
            claude_dir = Path(env["CLAUDE_CONFIG_DIR"])
            pass_root = claude_dir / "logs" / "memory-dream" / "passes"
            old = pass_root / "2026-01-01-ps"
            old.mkdir(parents=True)
            (old / "preview.html").write_text("stale content", encoding="utf-8")
            old_time = time.time() - 95 * 86400
            os.utime(old, (old_time, old_time))

            result = run_cli("doctor", "--strict", cwd=REPO_ROOT, env=env)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_unverifiable_index_cap_is_not_drift_even_with_strict(self):
        with tempfile.TemporaryDirectory() as temp:
            # _healthy_env already scopes PATH to an empty dir, so `claude`
            # is never found here and "index cap" is unconditionally
            # "unverifiable" -- exactly the case under test.
            env = self._healthy_env(Path(temp))
            result = run_cli("doctor", "--strict", cwd=REPO_ROOT, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("unverifiable", _doctor_line(result.stdout, "index cap").lower())
            lines = result.stdout.rstrip().splitlines()
            self.assertEqual(lines[-1], _expected_drift_line())

    def test_missing_consent_trace_is_not_drift_even_with_strict(self):
        with tempfile.TemporaryDirectory() as temp:
            claude_dir = Path(temp) / "claude-config"
            (claude_dir / "projects" / "some-project" / "memory").mkdir(parents=True)
            # deliberately no transcripts dir for this cwd -- a fresh checkout
            env = subprocess_env(claude_dir)
            empty_path_dir = Path(temp) / "empty-path"  # see _healthy_env: scope out a real `claude` on PATH
            empty_path_dir.mkdir()
            env["PATH"] = str(empty_path_dir)
            result = run_cli("doctor", "--strict", cwd=REPO_ROOT, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(_doctor_line(result.stdout, "consent trace").startswith("warn"))
            lines = result.stdout.rstrip().splitlines()
            self.assertEqual(lines[-1], _expected_drift_line())

    def test_config_override_is_not_drift_even_with_strict(self):
        with tempfile.TemporaryDirectory() as temp:
            env = self._healthy_env(Path(temp))
            env["MEMORY_DREAM_MERGE_JACCARD"] = "0.9"
            result = run_cli("doctor", "--strict", cwd=REPO_ROOT, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("MERGE_JACCARD", _doctor_line(result.stdout, "config overrides"))
            lines = result.stdout.rstrip().splitlines()
            self.assertEqual(lines[-1], _expected_drift_line())


class DoctorReadinessLineTests(unittest.TestCase):
    """`doctor`'s "readiness" advisory line: calls `audit.compute_triage`
    in-process (both suppression windows apply) and surfaces the flagged
    count without shelling out to `memory-dream triage`. Reports
    "unavailable" -- never drift -- when there is nothing to score
    (missing or empty live root); always advisory (never affects doctor's
    exit code)."""

    def test_unavailable_when_live_root_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            claude_dir = Path(temp) / "claude-config"
            claude_dir.mkdir()  # deliberately no "projects" subdirectory
            env = subprocess_env(claude_dir)
            result = run_cli("doctor", cwd=REPO_ROOT, env=env)
            self.assertEqual(result.returncode, 1)  # live root hard failure, unaffected by readiness
            line = _doctor_line(result.stdout, "readiness")
            self.assertTrue(line.startswith("ok"), line)
            self.assertIn("unavailable", line)

    def test_unavailable_when_live_root_empty(self):
        with tempfile.TemporaryDirectory() as temp:
            claude_dir = Path(temp) / "claude-config"
            (claude_dir / "projects").mkdir(parents=True)  # exists, holds zero projects
            env = subprocess_env(claude_dir)
            result = run_cli("doctor", cwd=REPO_ROOT, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            line = _doctor_line(result.stdout, "readiness")
            self.assertTrue(line.startswith("ok"), line)
            self.assertIn("unavailable", line)

    def test_flagged_count_matches_triage(self):
        with tempfile.TemporaryDirectory() as temp:
            claude_dir = Path(temp) / "claude-config"
            live_root = claude_dir / "projects"
            memory_dir = live_root / "proj" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "MEMORY.md").write_text("- [Log](log.md)\n", encoding="utf-8", newline="\n")
            (memory_dir / "log.md").write_text(note("log", body="z" * 6500), encoding="utf-8", newline="\n")
            env = subprocess_env(claude_dir)

            triage_result = run_cli(
                "triage", "--live-root", str(live_root), "--format", "json", cwd=REPO_ROOT, env=env,
            )
            self.assertEqual(triage_result.returncode, 0, triage_result.stdout + triage_result.stderr)
            triage_summary = json.loads(triage_result.stdout)["summary"]
            self.assertGreater(triage_summary["flagged"], 0)  # fixture must actually flag something

            result = run_cli("doctor", cwd=REPO_ROOT, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            line = _doctor_line(result.stdout, "readiness")
            self.assertTrue(line.startswith("ok"), line)
            self.assertIn(f"{triage_summary['flagged']} note(s) flagged", line)
            self.assertIn(f"{triage_summary['live_projects']} project(s)", line)


# =============================================================================
# 12. compatibility record single-source guard
# =============================================================================


class CompatibilityRecordSingleSourceTests(unittest.TestCase):
    """The compatibility record is the only place the measured version
    literal lives; every prose/code site cites it by name instead of
    restating it."""

    def test_version_literal_appears_in_exactly_one_file(self):
        version = config.COMPATIBILITY_RECORD["claude_code_version"]
        excluded_dirs = {".git", ".claude", "docs/plans", "docs/ideation"}
        hits = []
        for path in REPO_ROOT.rglob("*"):
            if not path.is_file() or path.suffix not in (".py", ".md"):
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            if any(rel == d or rel.startswith(d + "/") for d in excluded_dirs):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if version in text:
                hits.append(rel)
        self.assertEqual(hits, ["memory_dream/config.py"], f"version literal leaked into: {hits}")


if __name__ == "__main__":
    unittest.main()
