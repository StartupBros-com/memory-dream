"""Single source of every path default and tunable threshold.

Resolution order, highest first:
  1. CLI flags (each subcommand exposes the ones it honors)
  2. Environment variables (MEMORY_DREAM_*, plus the two CLAUDE_MEMORY_* names
     kept for compatibility with pre-extraction installs)
  3. Optional JSON config file at <claude-config-dir>/memory-dream.json
  4. The defaults below

Modules reference thresholds as ``config.NAME`` at use time (never
``from config import NAME``), so file-config overrides and test patches
propagate. Threshold provenance and mis-tuning symptoms: docs/TUNING.md —
most values were calibrated on one ~500-note English corpus and are
defaults, not truths.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

# --- Triage: what flags a note for consolidation ---------------------------
TRIAGE_BODY_BYTES = 6000  # body size that flags a note as oversized
TRIAGE_BODY_BYTES_LARGE = 10000  # stronger ranking boost past this
TRIAGE_AGE_DAYS = 90  # mtime age that boosts ranking (never flags alone)
TRIAGE_AGE_DAYS_OLD = 180  # stronger age boost
TRIAGE_MAX_CLUSTERS = 12  # per-pass cluster cap (overflow -> deferred)
TRIAGE_MAX_NOTES_PER_CLUSTER = 8
TRIAGE_DESC_MIN_WORDS = 5  # descriptions shorter than this are flagged vague
SUPPRESS_APPLIED_DAYS = 14  # don't re-flag notes a recent pass just touched

# --- Compatibility record ---------------------------------------------------
# Single source of truth for the "measured against Claude Code vX.Y.Z" claim
# repeated across docs and doctor output. This is the only literal version
# string in the repo; every other site (code and prose) cites
# COMPATIBILITY_RECORD by name instead of restating it. `doctor`'s "index
# cap" check (cli.py) shells out to `claude --version` and compares the
# installed version against claude_code_version below, reporting
# ok/drift/unverifiable — advisory, never fatal.
COMPATIBILITY_RECORD: dict[str, object] = {
    "claude_code_version": "2.1.211",
    "index_load_max_lines": 200,
    "index_load_max_bytes": 25 * 1024,
}

# --- Index budget (the loader-visible MEMORY.md surface) -------------------
# Cap values measured against COMPATIBILITY_RECORD's Claude Code version; the
# cap is not a documented API and can drift with the CLI. `doctor` compares
# the installed CLI version against the record and reports drift.
INDEX_LOAD_MAX_LINES = 200
INDEX_LOAD_MAX_BYTES = 25 * 1024
INDEX_BUDGET_FRACTION = 0.7  # repository-hygiene warning threshold
# Repo-hygiene audit ceilings (distinct from the loader cap above).
AUDIT_MAX_INDEX_BYTES = 32768
AUDIT_MAX_INDEX_LINES = 400
AUDIT_STALE_DAYS = 90

# --- Drafting / build validation -------------------------------------------
MERGE_JACCARD = 0.5  # near-duplicate detection for merge candidates
SIBLING_DESC_JACCARD = 0.6  # build rejects split siblings blurrier than this
MAX_SPLIT_EXTRACTS = 6
ENTRY_DATE_SCAN_BYTES = 4000  # archive candidate date scan: note-body head

# --- Decay (notes carrying confidence/last_validated frontmatter) ----------
DECAY_HALF_LIFE_DAYS = 90.0
DECAY_FLAG_THRESHOLD = 0.3  # effective confidence below this flags the note
NEW_EXTRACT_CONFIDENCE = "0.8"  # stamped into new extracts at build
NEW_EXTRACT_MATURITY = "candidate"

# --- Apply ------------------------------------------------------------------
ACTIVE_WINDOW_SECONDS = 900  # sibling-session activity warning window
PATCH_SET_RETENTION_DAYS = 90  # advisory; pruning is operator-owned

# --- Sensitive-content redaction --------------------------------------------
# Extended (never replaced) by the "sensitive_patterns_extra" config-file key:
# a list of regex strings matched against note filenames.
SENSITIVE_PATTERNS_EXTRA: list[str] = []

# What a mirror-stale apply refusal tells the operator to run. Personal
# harnesses set this to their own sync command via the config file.
MIRROR_PUSH_HINT = "sync your mirror, then retry"

_ENV_PREFIX = "MEMORY_DREAM_"
_FILE_CONFIG_LOADED = False

# Threshold names the JSON config file / env may override. Leading-underscore
# module state (``_ENV_PREFIX``, ``_FILE_CONFIG_LOADED``) is excluded: str.isupper()
# ignores non-cased characters, so those names pass an ``.isupper()`` filter and
# would otherwise become override-able config keys — letting a config file mutate
# ``_ENV_PREFIX`` mid-load and silently break the env-beats-file precedence.
_OVERRIDABLE = {
    name
    for name, value in list(globals().items())
    if name.isupper() and not name.startswith("_") and not isinstance(value, bool) and isinstance(value, (int, float, str, list))
}

# Snapshot of the shipped default for every _OVERRIDABLE name, captured here
# -- before load_file_config()/_apply_env_overrides() mutate these same
# globals in place -- so `doctor` can later diff the live value against what
# actually shipped, no matter what the running process has already applied.
_DEFAULTS: dict[str, object] = {name: globals()[name] for name in _OVERRIDABLE}


def claude_config_dir() -> Path:
    """Base directory for Claude Code state (honors CLAUDE_CONFIG_DIR)."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()


def config_file_path() -> Path:
    return claude_config_dir() / "memory-dream.json"


def default_live_root() -> Path:
    return Path(
        os.environ.get(
            _ENV_PREFIX + "LIVE_ROOT",
            os.environ.get("CLAUDE_MEMORY_LIVE_ROOT", str(claude_config_dir() / "projects")),
        )
    ).expanduser()


def default_mirror_root() -> Path | None:
    """A git-tracked mirror of live memory. None (the default) = snapshot mode.

    There is deliberately no path-relative fallback: the pre-extraction
    default derived a mirror from the script's own location and silently
    repointed when run from a copied checkout.
    """
    raw = os.environ.get(_ENV_PREFIX + "MIRROR_ROOT", os.environ.get("CLAUDE_MEMORY_MIRROR_ROOT"))
    if raw:
        return Path(raw).expanduser()
    file_cfg = _file_config()
    if file_cfg.get("mirror_root"):
        return Path(str(file_cfg["mirror_root"])).expanduser()
    return None


def pass_root() -> Path:
    return Path(
        os.environ.get(_ENV_PREFIX + "PASS_ROOT", str(claude_config_dir() / "logs" / "memory-dream" / "passes"))
    ).expanduser()


def eval_home() -> Path:
    return Path(
        os.environ.get(_ENV_PREFIX + "EVAL_HOME", str(claude_config_dir() / "logs" / "memory-dream" / "eval"))
    ).expanduser()


def scratch_dir() -> Path:
    """Session scratch for plan/drafts/selection JSON. Never memory bodies."""
    env = os.environ.get(_ENV_PREFIX + "SCRATCH")
    if env:
        p = Path(env).expanduser()
    elif os.environ.get("CLAUDE_JOB_DIR"):
        p = Path(os.environ["CLAUDE_JOB_DIR"]) / "tmp"
    else:
        p = Path(tempfile.gettempdir()) / f"memory-dream-{os.getpid()}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _file_config() -> dict:
    path = config_file_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"unreadable config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"config file {path} must hold one JSON object")
    return data


def _apply_env_overrides() -> None:
    """MEMORY_DREAM_<NAME> env vars override any threshold (env beats file)."""
    for name in _OVERRIDABLE:
        raw = os.environ.get(_ENV_PREFIX + name)
        if raw is None:
            continue
        current = globals()[name]
        try:
            if isinstance(current, bool):  # not currently used; guard anyway
                globals()[name] = raw.lower() in ("1", "true", "yes")
            elif isinstance(current, list):
                globals()[name] = json.loads(raw)
            else:
                globals()[name] = type(current)(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SystemExit(f"env var {_ENV_PREFIX + name}: expected {type(current).__name__}: {exc}") from exc


def load_file_config() -> None:
    """Apply config overrides once per process: file first, then env on top."""
    global _FILE_CONFIG_LOADED
    if _FILE_CONFIG_LOADED:
        return
    _FILE_CONFIG_LOADED = True
    data = _file_config()
    unknown = []
    for key, value in data.items():
        if key in ("mirror_root", "mirror_push_hint"):
            if key == "mirror_push_hint":
                globals()["MIRROR_PUSH_HINT"] = str(value)
            continue
        name = key.upper()
        if name in _OVERRIDABLE:
            current = globals()[name]
            try:
                globals()[name] = type(current)(value) if not isinstance(current, list) else list(value)
            except (TypeError, ValueError) as exc:
                raise SystemExit(f"config key {key}: expected {type(current).__name__}: {exc}") from exc
        else:
            unknown.append(key)
    if unknown:
        raise SystemExit(
            f"unknown config key(s) in {config_file_path()}: {', '.join(sorted(unknown))} "
            f"(valid: mirror_root, mirror_push_hint, {', '.join(sorted(k.lower() for k in _OVERRIDABLE))})"
        )
    _apply_env_overrides()


def non_default_values() -> dict[str, tuple[object, object, str]]:
    """Every ``_OVERRIDABLE`` name whose live value no longer matches its
    shipped default (``_DEFAULTS``), mapped to ``(current, default, source)``.

    Source attribution mirrors the resolution order ``load_file_config()``
    already enforces (env beats file): the env var is checked first, then
    the JSON config file key. Neither set means the value was mutated some
    other way (e.g. a test patching the module global directly); source is
    reported as "unknown" in that case.
    """
    file_cfg = _file_config()
    file_keys = {key.lower() for key in file_cfg}
    result: dict[str, tuple[object, object, str]] = {}
    for name, default in _DEFAULTS.items():
        current = globals()[name]
        if current == default:
            continue
        env_name = _ENV_PREFIX + name
        if os.environ.get(env_name) is not None:
            source = f"env:{env_name}"
        elif name.lower() in file_keys:
            source = f"file:{name.lower()}"
        else:
            source = "unknown"
        result[name] = (current, default, source)
    return result


def add_root_args(parser) -> None:
    """The two shared root flags every subcommand honors."""
    parser.add_argument(
        "--live-root",
        default=str(default_live_root()),
        help="root of per-project live memory (default: <claude-config-dir>/projects)",
    )
    mirror_default = default_mirror_root()
    parser.add_argument(
        "--mirror-root",
        default=str(mirror_default) if mirror_default else None,
        help="git-tracked mirror of live memory; omitted = snapshot mode (no mirror gates)",
    )


def add_out_file_arg(parser, what: str) -> None:
    """Register ``--out-file``: the in-process alternative to a shell redirect.

    Deliberately NOT spelled ``--out``. ``build`` and ``archive`` already use
    ``--out`` for an output DIRECTORY, and giving one flag name two meanings
    across sibling subcommands of the same CLI is a footgun.

    Why it exists at all: the documented pass captures these subcommands with
    ``> "$SCRATCH/..."``. A shell redirect whose target is only known after
    variable expansion cannot be statically proven safe, so agent command
    guards refuse it outright — dcg blocks exactly this shape under
    ``core.filesystem:redirect-truncate-dynamic-path``, which halts the
    documented pass at its first step and leaves nothing on disk recording
    where the agent improvised the file instead. Writing from inside the
    process needs no redirect, so the guard has nothing to refuse.
    """
    parser.add_argument(
        "--out-file",
        default=None,
        help=f"write the {what} JSON to this path instead of stdout",
    )


def emit_json(payload: object, out_file: str | None, **dumps_kwargs) -> None:
    """Emit ``payload`` as JSON to stdout, or to ``out_file`` when given.

    Parent directories are created. Callers keep their existing stdout
    behaviour untouched when ``--out-file`` is absent, so this is additive.
    """
    text = json.dumps(payload, **dumps_kwargs)
    if not out_file:
        print(text)
        return
    path = Path(out_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
