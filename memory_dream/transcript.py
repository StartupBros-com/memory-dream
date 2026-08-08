"""Claude Code transcript adapter: the consent-trace backend.

Apply's default consent gate (``--consent trace``) proves a real operator
approved a patch set by finding a human-typed transcript turn, recorded after
the preview, that carries the patch set's approval token. This module knows
how to find that transcript and how to read one JSONL entry out of it.

The transcripts-directory layout below is reverse-engineered, not a
documented Claude Code API: `doctor`'s schema probe exists specifically
because this can drift out from under us on a Claude Code upgrade, and a
drift must fail loudly (`TranscriptSchemaError`) rather than silently
disabling the consent gate.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import sys
from pathlib import Path
from typing import Any

from memory_dream import config

# CLI wrapper turns and interrupt markers that are NOT operator-typed prose,
# even though they arrive as ordinary type=="user" transcript entries: a
# slash-command echo, a bash-passthrough turn, or an incidental Ctrl-C right
# after the preview must never be mistaken for the approval.
_SYNTHETIC_PREFIXES = (
    "<command-message>",
    "<command-name>",
    "<local-command",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<user-prompt-submit-hook",
)
_SYNTHETIC_EXACT = (
    "[Request interrupted by user]",
    "[Request interrupted by user for tool use]",
)


class TranscriptSchemaError(RuntimeError):
    """A transcript entry doesn't match the known Claude Code JSONL shape.

    Raised instead of returning ``None`` so a transcript-format change breaks
    the consent gate loudly (a refusal naming what changed) instead of
    silently turning every future turn into "not an operator message".
    """


def cwd_slug(cwd: Path) -> str:
    """The project slug Claude Code derives from a working directory.

    Reverse-engineered: every path separator and ``.`` becomes ``-``. Windows
    drive colons and backslashes are replaced too — the slug must always stay
    a single relative path component, never an absolute path that would make
    ``projects / slug`` escape the config directory.
    """
    return "".join("-" if ch in "/.\\:" else ch for ch in str(cwd))


def transcripts_dir_for(cwd: Path) -> Path:
    """The per-project transcript directory Claude Code derives from a cwd.

    Reverse-engineered via `cwd_slug`: session transcripts live under
    ``<claude-config-dir>/projects/<slug>/``. This is not a documented API and
    can change between Claude Code releases; that is what `schema_probe` and
    `doctor`'s consent-trace check are for.
    """
    return config.claude_config_dir() / "projects" / cwd_slug(cwd)


def schema_probe(transcripts_dir: Path) -> str:
    """Sample the newest transcript and report whether its shape is recognized.

    Reads at most the first 50 lines of the newest ``*.jsonl`` file in
    ``transcripts_dir`` and runs each through `extract_user_text`. Used by
    `doctor` to surface transcript-format drift before it can break the
    consent gate mid-apply, rather than only discovering it during a refusal.
    """
    if not transcripts_dir.is_dir():
        return "UNRECOGNIZED: transcripts directory does not exist"
    transcripts = sorted(transcripts_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not transcripts:
        return "UNRECOGNIZED: no *.jsonl files found"
    newest = transcripts[-1]
    try:
        with newest.open("r", encoding="utf-8", errors="replace") as handle:
            sample_lines = list(itertools.islice(handle, 50))
    except OSError as error:
        return f"UNRECOGNIZED: {newest.name} unreadable: {error}"
    sampled = 0
    for raw_line in sample_lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return f"UNRECOGNIZED: {newest.name} contains a non-JSON line"
        if not isinstance(entry, dict):
            return f"UNRECOGNIZED: {newest.name} contains a non-object entry"
        try:
            text = extract_user_text(entry)
        except TranscriptSchemaError as error:
            return f"UNRECOGNIZED: {error}"
        if text is not None:
            sampled += 1
    return f"recognized ({sampled} user turn(s) sampled)"


def extract_user_text(entry: dict[str, Any]) -> str | None:
    """Return the human-typed text of a transcript entry, or None if it is not one.

    Tool results, assistant turns, meta entries, and CLI wrapper-tag content
    all return None: only genuine operator-typed prose can carry consent.

    An entry that looks like a real operator turn (``type == "user"``, not
    ``isMeta``) but whose ``message`` payload matches neither known content
    shape (a plain string, or a list of block dicts) raises
    `TranscriptSchemaError` naming what was found and what was expected,
    instead of silently returning None — see the module docstring.
    """
    if entry.get("type") != "user" or entry.get("isMeta"):
        return None
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        raise TranscriptSchemaError(
            "a type=='user' (non-meta) entry has an unrecognized 'message' shape: "
            f"expected a dict with role=='user', found {message!r}"
        )
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                raise TranscriptSchemaError(
                    "user-turn message.content is a list but contains a non-dict "
                    "block: expected every item to be a block dict (e.g. "
                    f"{{'type': 'text', 'text': ...}}), found {block!r}"
                )
        parts = [block.get("text", "") for block in content if block.get("type") == "text"]
        if not parts:
            # A recognized block shape (tool_result, tool_use, image, ...)
            # that simply carries no text: a real turn, just not an operator
            # message. Not a schema deviation.
            return None
        text = "".join(parts)
    else:
        raise TranscriptSchemaError(
            "user-turn message.content is neither a string nor a list of "
            f"blocks: expected str or list, found {type(content).__name__} ({content!r})"
        )
    stripped = text.strip()
    if stripped.startswith(_SYNTHETIC_PREFIXES) or stripped in _SYNTHETIC_EXACT:
        return None
    return text


# --- trace subcommand --------------------------------------------------------


def run_trace(args: argparse.Namespace) -> int:
    """Emit the operator_trace for the latest post-preview human turn.

    Scans backward from the end of the transcript to ``--created-at-line``,
    reporting the most recent operator turn whose text contains ``--token``.
    This is the producer half of the consent-trace gate: apply's verification
    calls the same `extract_user_text` on the same transcript line, so the
    two can never drift against each other.
    """
    transcript_path = Path(args.transcript).expanduser()
    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as error:
        print(f"memory-dream trace: transcript unreadable: {error}", file=sys.stderr)
        return 2
    for index in range(len(lines) - 1, args.created_at_line - 1, -1):
        try:
            entry = json.loads(lines[index])
        except json.JSONDecodeError:
            continue
        try:
            text = extract_user_text(entry)
        except TranscriptSchemaError as error:
            print(f"memory-dream trace: transcript schema error at line {index}: {error}", file=sys.stderr)
            return 2
        # The approval turn must carry the token (the patch set's manifest
        # id, which the operator types to consent to THIS set), so an
        # incidental post-preview turn is never mistaken for approval.
        if text is not None and (not args.token or args.token in text):
            trace = {"message_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "turn_index": index}
            print(json.dumps(trace, sort_keys=True))
            return 0
    print("memory-dream trace: no post-preview operator turn carrying the approval token; cannot approve", file=sys.stderr)
    return 1


def run_transcript_locate(args: argparse.Namespace) -> int:
    """Print the newest transcript path for the current working directory.

    Used by the dream command in place of an inline shell slug derivation
    (the cwd -> transcripts-directory mapping lives in exactly one place:
    `transcripts_dir_for`).
    """
    transcripts_dir = transcripts_dir_for(Path.cwd())
    if not transcripts_dir.is_dir():
        print(
            f"memory-dream transcript-locate: no transcript directory for this cwd: {transcripts_dir}",
            file=sys.stderr,
        )
        return 1
    transcripts = sorted(transcripts_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not transcripts:
        print(
            f"memory-dream transcript-locate: transcript directory holds no *.jsonl files: {transcripts_dir}",
            file=sys.stderr,
        )
        return 1
    print(transcripts[-1])
    return 0


def add_parsers(subparsers) -> None:
    trace = subparsers.add_parser("trace", help="emit operator_trace for the latest post-preview human turn")
    trace.add_argument("--transcript", required=True, help="session transcript .jsonl to scan")
    trace.add_argument("--created-at-line", type=int, default=0, help="scan stops here: the preview's line count")
    trace.add_argument("--token", default="", help="approval token the human turn must contain (the manifest id)")
    trace.set_defaults(func=run_trace)

    locate = subparsers.add_parser(
        "transcript-locate", help="print the newest transcript path for the current working directory"
    )
    locate.set_defaults(func=run_transcript_locate)
