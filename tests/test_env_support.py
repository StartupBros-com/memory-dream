"""Synthetic regressions for the shared narrow environment-isolation policy."""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from _env_support import strip_leaky_keys, subprocess_env


class EnvironmentSupportTests(unittest.TestCase):
    def test_strips_only_the_matching_prefixes_and_exact_job_key(self):
        source = {
            "MEMORY_DREAM_": "drop",
            "MEMORY_DREAM_PASS_ROOT": "drop",
            "CLAUDE_MEMORY_": "drop",
            "CLAUDE_MEMORY_ROOT": "drop",
            "CLAUDE_JOB_DIR": "drop",
            "CLAUDE_JOB_DIR_EXTRA": "keep",
            "CLAUDE_OTHER": "keep",
            "CLAUDE_CONFIG_DIR": "keep until replaced",
            "MEMORY_DREAM": "keep",
            "CLAUDE_MEMORY": "keep",
            "memory_dream_root": "keep",
            "PATH": "synthetic-bin",
            "EMPTY": "",
        }
        before = source.copy()
        expected = {
            key: value for key, value in source.items()
            if value != "drop"
        }
        result = strip_leaky_keys(source)
        self.assertEqual(result, expected)
        self.assertEqual(source, before)
        self.assertIsNot(result, source)
        result["PATH"] = "changed"
        self.assertEqual(source, before)

    def test_empty_input_returns_a_distinct_empty_mapping(self):
        source = {}
        result = strip_leaky_keys(source)
        self.assertEqual(result, {})
        self.assertIsNot(result, source)

    def test_subprocess_env_replaces_config_without_mutating_ambient_env(self):
        ambient = {
            "MEMORY_DREAM_PASS_ROOT": "synthetic-pass-root",
            "CLAUDE_MEMORY_ROOT": "synthetic-memory-root",
            "CLAUDE_JOB_DIR": "synthetic-job-root",
            "CLAUDE_CONFIG_DIR": "synthetic-ambient-config",
            "CLAUDE_OTHER": "preserved",
            "PATH": "synthetic-bin",
        }
        config_dir = Path("synthetic fixture") / "config"
        with patch.dict(os.environ, ambient, clear=True):
            before = dict(os.environ)
            result = subprocess_env(config_dir)
            self.assertEqual(result, {
                "CLAUDE_CONFIG_DIR": str(config_dir),
                "CLAUDE_OTHER": "preserved",
                "PATH": "synthetic-bin",
            })
            self.assertEqual(dict(os.environ), before)
            result["PATH"] = "changed"
            self.assertEqual(dict(os.environ), before)

    def test_subprocess_env_reads_each_current_environment(self):
        with patch.dict(os.environ, {"MARKER": "first"}, clear=True):
            first = subprocess_env(Path("first-config"))
            os.environ["MARKER"] = "second"
            second = subprocess_env(Path("second-config"))
        self.assertEqual(first, {
            "MARKER": "first", "CLAUDE_CONFIG_DIR": str(Path("first-config")),
        })
        self.assertEqual(second, {
            "MARKER": "second", "CLAUDE_CONFIG_DIR": str(Path("second-config")),
        })
        self.assertIsNot(first, second)


if __name__ == "__main__":
    unittest.main()
