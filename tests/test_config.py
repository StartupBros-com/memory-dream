#!/usr/bin/env python3
"""Behavior-preservation tests for config override coercion."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from memory_dream import config


class ConfigOverrideTests(unittest.TestCase):
    def test_environment_scalar_override_keeps_default_type(self):
        with (
            mock.patch.object(config, "_OVERRIDABLE", {"TRIAGE_BODY_BYTES"}),
            mock.patch.object(config, "TRIAGE_BODY_BYTES", 6000),
            mock.patch.dict(os.environ, {"MEMORY_DREAM_TRIAGE_BODY_BYTES": "1234"}, clear=False),
        ):
            config._apply_env_overrides()
            self.assertEqual(config.TRIAGE_BODY_BYTES, 1234)
            self.assertIs(type(config.TRIAGE_BODY_BYTES), int)

    def test_environment_list_override_remains_json_decoded(self):
        with (
            mock.patch.object(config, "_OVERRIDABLE", {"SENSITIVE_PATTERNS_EXTRA"}),
            mock.patch.object(config, "SENSITIVE_PATTERNS_EXTRA", []),
            mock.patch.dict(
                os.environ,
                {"MEMORY_DREAM_SENSITIVE_PATTERNS_EXTRA": '["secret", "token"]'},
                clear=False,
            ),
        ):
            config._apply_env_overrides()
            self.assertEqual(config.SENSITIVE_PATTERNS_EXTRA, ["secret", "token"])

    def test_file_list_override_remains_a_copy_of_decoded_value(self):
        supplied = ["secret", "token"]
        with (
            mock.patch.object(config, "_FILE_CONFIG_LOADED", False),
            mock.patch.object(config, "_OVERRIDABLE", {"SENSITIVE_PATTERNS_EXTRA"}),
            mock.patch.object(config, "SENSITIVE_PATTERNS_EXTRA", []),
            mock.patch.object(config, "_file_config", return_value={"sensitive_patterns_extra": supplied}),
            mock.patch.object(config, "_apply_env_overrides"),
        ):
            config.load_file_config()
            self.assertEqual(config.SENSITIVE_PATTERNS_EXTRA, supplied)
            self.assertIsNot(config.SENSITIVE_PATTERNS_EXTRA, supplied)

    def test_invalid_environment_override_keeps_source_specific_diagnostic(self):
        with (
            mock.patch.object(config, "_OVERRIDABLE", {"TRIAGE_BODY_BYTES"}),
            mock.patch.object(config, "TRIAGE_BODY_BYTES", 6000),
            mock.patch.dict(os.environ, {"MEMORY_DREAM_TRIAGE_BODY_BYTES": "not-an-int"}, clear=False),
        ):
            with self.assertRaisesRegex(
                SystemExit,
                r"^env var MEMORY_DREAM_TRIAGE_BODY_BYTES: expected int:",
            ):
                config._apply_env_overrides()

    def test_invalid_file_override_keeps_source_specific_diagnostic(self):
        with (
            mock.patch.object(config, "_FILE_CONFIG_LOADED", False),
            mock.patch.object(config, "_OVERRIDABLE", {"TRIAGE_BODY_BYTES"}),
            mock.patch.object(config, "TRIAGE_BODY_BYTES", 6000),
            mock.patch.object(config, "_file_config", return_value={"triage_body_bytes": "not-an-int"}),
            mock.patch.object(config, "_apply_env_overrides"),
        ):
            with self.assertRaisesRegex(SystemExit, r"^config key triage_body_bytes: expected int:"):
                config.load_file_config()


if __name__ == "__main__":
    unittest.main()
