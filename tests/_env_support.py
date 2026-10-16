"""Shared environment isolation for tests using the narrow override policy.

Only callers with this exact policy belong here. Other test modules deliberately
strip all CLAUDE_* keys or retain CLAUDE_JOB_DIR; their policies stay separate.
"""

import os
from pathlib import Path


def strip_leaky_keys(env: dict) -> dict:
    """Copy env without memory overrides or the exact job-directory fallback."""
    return {
        key: value
        for key, value in env.items()
        if not key.startswith(("MEMORY_DREAM_", "CLAUDE_MEMORY_")) and key != "CLAUDE_JOB_DIR"
    }


def subprocess_env(claude_config_dir: Path) -> dict:
    """Build a fresh subprocess environment using the caller's fixture config."""
    env = strip_leaky_keys(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir)
    return env
