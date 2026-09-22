#!/usr/bin/env python3
"""Configured preview openers never use the Windows-home copy or fallback."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from memory_dream import cli, config


class PreviewOpenerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.patch_set = self.root / "patch set with spaces"
        self.patch_set.mkdir()
        self.preview = self.patch_set / "preview.html"
        self.preview.write_text("<html>private memory bodies</html>", encoding="utf-8")
        self.windows_home = self.root / "windows-profile"
        self.windows_home.mkdir()
        self.record = self.root / "recorded argv.json"
        self.stub = self.root / "stub opener.py"
        self.stub.write_text(
            "import json, pathlib, sys\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[3:]), encoding='utf-8')\n"
            "raise SystemExit(int(sys.argv[2]))\n",
            encoding="utf-8",
        )

    def opener_command(self, exit_code):
        return shlex.join(
            [
                sys.executable,
                str(self.stub),
                str(self.record),
                str(exit_code),
                "--label",
                "two words",
                "literal;$(echo never)",
            ]
        )

    def run_configured_opener(self, command):
        stderr = io.StringIO()
        with (
            mock.patch.object(config, "PREVIEW_OPENER", command),
            mock.patch.object(
                cli, "_wsl_windows_homes", return_value=[self.windows_home]
            ) as homes,
            mock.patch("shutil.copy") as copy,
            mock.patch("shutil.which") as which,
            contextlib.redirect_stderr(stderr),
        ):
            status = cli._run_open_preview(
                argparse.Namespace(patch_set=str(self.patch_set))
            )
        homes.assert_not_called()
        copy.assert_not_called()
        which.assert_not_called()
        self.assertFalse((self.windows_home / "memory-dream-preview.html").exists())
        return status, stderr.getvalue()

    def test_opener_receives_literal_arguments_and_final_preview_path(self):
        status, stderr = self.run_configured_opener(self.opener_command(0))
        self.assertEqual(status, 0, stderr)
        self.assertEqual(
            json.loads(self.record.read_text(encoding="utf-8")),
            ["--label", "two words", "literal;$(echo never)", str(self.preview)],
        )

    def test_opener_is_waited_for_and_nonzero_exit_propagates_without_fallback(self):
        status, _ = self.run_configured_opener(self.opener_command(23))
        self.assertEqual(status, 23)
        self.assertEqual(
            json.loads(self.record.read_text(encoding="utf-8"))[-1], str(self.preview)
        )

    def test_invalid_command_fails_without_fallback(self):
        for command in (
            "",
            "   ",
            "'unterminated",
            shlex.quote(str(self.root / "missing-opener")),
        ):
            with self.subTest(command=command):
                status, stderr = self.run_configured_opener(command)
                self.assertEqual(status, 1)
                self.assertIn("could not run preview_opener:", stderr)
                self.assertFalse(self.record.exists())

    def test_missing_preview_does_not_invoke_opener(self):
        self.preview.unlink()
        status, stderr = self.run_configured_opener(self.opener_command(0))
        self.assertEqual(status, 1)
        self.assertIn("no preview.html", stderr)
        self.assertFalse(self.record.exists())


if __name__ == "__main__":
    unittest.main()
