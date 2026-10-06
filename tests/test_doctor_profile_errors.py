"""Unreadable Windows profiles must not abort the advisory doctor probe."""
from __future__ import annotations

import argparse
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory_dream import cli, config, transcript


class ProfileErrorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.users = self.root / "Users"
        self.users.mkdir()

    def test_denied_profile_does_not_hide_other_candidates(self):
        denied = self.users / "a-denied"
        good = self.users / "b-readable"
        denied.mkdir()
        good.mkdir()
        original = Path.is_dir

        def is_dir(path):
            if path == denied:
                raise PermissionError("fixture profile denied")
            return original(path)

        with mock.patch.object(Path, "is_dir", is_dir):
            self.assertEqual(cli._wsl_windows_homes(self.users), [good])

    def test_excluded_system_profiles_are_never_probed(self):
        system = self.users / "Default User"
        system.mkdir()
        original = Path.is_dir

        def is_dir(path):
            if path == system:
                raise PermissionError("system profile must not be inspected")
            return original(path)

        with mock.patch.object(Path, "is_dir", is_dir):
            self.assertEqual(cli._wsl_windows_homes(self.users), [])

    def test_failed_root_enumeration_is_recorded(self):
        for error in (PermissionError("denied"), FileNotFoundError("vanished")):
            with self.subTest(error=type(error).__name__):
                unreadable = []
                with mock.patch.object(Path, "iterdir", side_effect=error):
                    homes = cli._wsl_windows_homes(self.users, unreadable=unreadable)
                self.assertEqual(homes, [])
                self.assertEqual(unreadable, [self.users])
                label, ok, detail, fatal = cli._preview_copy_retention_check(
                    homes, unreadable=unreadable
                )
                self.assertEqual(label, "preview copy")
                self.assertTrue(ok)  # unknown alone is not concrete drift
                self.assertFalse(fatal)
                self.assertIn("unverifiable", detail)
                self.assertIn(str(self.users), detail)

    def test_failed_root_stat_degrades_without_exception(self):
        with mock.patch.object(Path, "is_dir", side_effect=PermissionError("denied")):
            self.assertEqual(cli._wsl_windows_homes(self.users), [])

    def test_failed_preview_probe_is_unverifiable_not_none(self):
        home = self.users / "readable-home"
        home.mkdir()
        with mock.patch.object(Path, "is_file", side_effect=PermissionError("denied")):
            label, ok, detail, fatal = cli._preview_copy_retention_check([home])
        self.assertEqual(label, "preview copy")
        self.assertTrue(ok)
        self.assertFalse(fatal)
        self.assertIn("unverifiable", detail)
        self.assertIn(str(home / "memory-dream-preview.html"), detail)

    def test_leftover_stays_drift_when_another_home_cannot_be_inspected(self):
        home = self.users / "readable-home"
        home.mkdir()
        preview = home / "memory-dream-preview.html"
        preview.write_text("fixture", encoding="utf-8")
        inaccessible = [self.users / "denied"]
        label, ok, detail, fatal = cli._preview_copy_retention_check(
            [home], unreadable=inaccessible
        )
        self.assertFalse(ok)
        self.assertFalse(fatal)
        self.assertIn(str(preview), detail)
        self.assertIn("delete after review", detail)
        self.assertIn("unverifiable", detail)
        self.assertTrue(preview.exists())
        self.assertEqual(inaccessible, [self.users / "denied"])

    def test_doctor_reports_unreadable_profiles_without_changing_strict_exit(self):
        live = self.root / "live"
        scratch = self.root / "scratch"
        live.mkdir()
        scratch.mkdir()
        original = cli._wsl_windows_homes

        def homes(*, unreadable=None):
            with mock.patch.object(Path, "iterdir", side_effect=PermissionError("denied")):
                return original(self.users, unreadable=unreadable)

        output = io.StringIO()
        with (
            mock.patch.object(cli, "_wsl_windows_homes", side_effect=homes),
            mock.patch.object(config, "scratch_dir", return_value=scratch),
            mock.patch.object(config, "pass_root", return_value=self.root / "passes"),
            mock.patch.object(config, "non_default_values", return_value={}),
            mock.patch.object(transcript, "transcripts_dir_for", return_value=self.root / "transcripts"),
            mock.patch.object(cli, "_detect_installed_claude_version", return_value=None),
            contextlib.redirect_stdout(output),
        ):
            status = cli._run_doctor(
                argparse.Namespace(live_root=str(live), mirror_root=None, strict=True)
            )
        self.assertEqual(status, 0, output.getvalue())
        line = next(line for line in output.getvalue().splitlines() if "preview copy:" in line)
        self.assertIn("unverifiable", line)
        self.assertIn(str(self.users), line)
        self.assertIn("drift: none", output.getvalue())


if __name__ == "__main__":
    unittest.main()
