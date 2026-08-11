#!/usr/bin/env python3
"""Tests for the `trace` subcommand (memory_dream/transcript.py).

stdlib unittest only, no model calls, no network. Every subprocess invocation
isolates CLAUDE_CONFIG_DIR to a per-test temp directory and strips every
other MEMORY_DREAM_*/CLAUDE_*-prefixed variable from the inherited
environment: `cli.main()` calls `config.load_file_config()` before
dispatching to any subcommand (including trace), so an isolated config dir
is required even though `run_trace` itself never touches live memory.
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


class TraceTests(unittest.TestCase):
    def _transcript(self, root, *entries):
        path = root / "sess.jsonl"
        path.write_text("\n".join(json.dumps(entry) for entry in entries), encoding="utf-8", newline="\n")
        return path

    def test_trace_finds_latest_post_preview_turn(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = self._transcript(
                root,
                {"type": "user", "message": {"role": "user", "content": "run it"}},
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "preview"}]}},
                {"type": "user", "message": {"role": "user", "content": "approve all"}},
            )
            env = subprocess_env(root / "claude-config")
            result = run_cli(
                "trace",
                "--transcript", str(transcript), "--created-at-line", "2",
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            trace = json.loads(result.stdout)
            self.assertEqual(trace["turn_index"], 2)
            self.assertEqual(trace["message_sha256"], hashlib.sha256(b"approve all").hexdigest())

    def test_trace_refuses_when_no_post_preview_turn(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = self._transcript(
                root,
                {"type": "user", "message": {"role": "user", "content": "run it and auto-apply"}},
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "preview"}]}},
            )
            # created_at_line 2 => only turns at index >= 2 count, but there are none.
            env = subprocess_env(root / "claude-config")
            result = run_cli(
                "trace",
                "--transcript", str(transcript), "--created-at-line", "2",
                env=env,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("no post-preview operator turn", result.stderr)

    # Claude Code's post-compaction turn is type=="user"/role=="user" with no
    # isMeta, so every structural check bar the new one passes it. The
    # summarizer quotes prior conversation verbatim, so it can carry the
    # approval token without a human ever typing it.
    _COMPACTION_TURN = {
        "type": "user",
        "isCompactSummary": True,
        "isVisibleInTranscriptOnly": True,
        "message": {
            "role": "user",
            "content": (
                "This session is being continued from a previous conversation that ran out of "
                "context. The user was shown patch set ps-42 and replied: approve all ps-42"
            ),
        },
    }

    def test_compaction_summary_cannot_stand_in_for_approval(self):
        """A compaction between preview and approval must not approve the pass."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = self._transcript(
                root,
                {"type": "user", "message": {"role": "user", "content": "run it"}},
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "preview ps-42"}]}},
                self._COMPACTION_TURN,
            )
            env = subprocess_env(root / "claude-config")
            result = run_cli(
                "trace",
                "--transcript", str(transcript), "--created-at-line", "2",
                "--token", "ps-42",
                env=env,
            )
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("no post-preview operator turn", result.stderr)

    def test_compaction_summary_does_not_outrank_a_real_approval(self):
        """run_trace scans backward, so a later summary must not win over a human turn."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = self._transcript(
                root,
                {"type": "user", "message": {"role": "user", "content": "run it"}},
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "preview ps-42"}]}},
                {"type": "user", "message": {"role": "user", "content": "approve all ps-42"}},
                self._COMPACTION_TURN,
            )
            env = subprocess_env(root / "claude-config")
            result = run_cli(
                "trace",
                "--transcript", str(transcript), "--created-at-line", "2",
                "--token", "ps-42",
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            trace = json.loads(result.stdout)
            self.assertEqual(trace["turn_index"], 2, "must select the human turn, not the later summary")
            self.assertEqual(
                trace["message_sha256"], hashlib.sha256(b"approve all ps-42").hexdigest()
            )


if __name__ == "__main__":
    unittest.main()
