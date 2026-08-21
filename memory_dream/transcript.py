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


# --- compaction canary -------------------------------------------------------

# The two flag keys extract_user_text keys its compaction rejection on
# (transcript.py:155). If Claude Code renames both of them, a compaction turn
# stops being recognized and falls through to being read as an ordinary
# operator turn -- silently, with no exception (see the module docstring's
# TranscriptSchemaError rationale, which does not cover this path: a
# recognized user-turn shape with unrecognized *flag* keys is not a schema
# error, it is a value the flag check simply never fires on).
_COMPACTION_FLAG_KEYS = ("isCompactSummary", "isVisibleInTranscriptOnly")

# Claude Code's documented post-compaction continuation prose (see
# tests/test_trace.py's _COMPACTION_TURN fixture). Model-written and in
# principle unstable, but it is the only shape signal left once a turn's flag
# keys have been renamed -- exactly the drift this canary exists to catch.
_COMPACTION_BOILERPLATE_PREFIX = "This session is being continued from a previous conversation"

_CANARY_SAMPLE_FILES = 5


def _canary_turn_text(entry: dict[str, Any]) -> str | None:
    """Best-effort text extraction for the canary only.

    Mirrors `extract_user_text`'s structural checks (a dict message with
    role=='user', string or text-block-list content) but returns None on any
    unrecognized shape instead of raising: the canary is an advisory sample
    over transcripts that were never asserted to be well-formed, not the
    consent path itself (that loud-failure behavior stays on
    `extract_user_text`/`TranscriptSchemaError`).
    """
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "".join(parts) if parts else None
    return None


def _classify_canary_turn(entry: dict[str, Any]) -> str | None:
    """Classify one transcript entry for the compaction canary.

    Returns "verified" when the entry is a type=='user' (non-meta) turn
    carrying a truthy compaction flag (the shape `extract_user_text` expects);
    "drift" when it is a type=='user' (non-meta) turn that matches the
    documented compaction boilerplate prefix but carries neither flag (the
    shape `extract_user_text` would silently mis-handle if the flags were
    renamed); None when the entry is not compaction-shaped at all -- not a
    candidate turn for this canary, keep scanning.
    """
    if entry.get("type") != "user" or entry.get("isMeta"):
        return None
    if any(entry.get(key) for key in _COMPACTION_FLAG_KEYS):
        return "verified"
    text = _canary_turn_text(entry)
    if text is not None and text.strip().startswith(_COMPACTION_BOILERPLATE_PREFIX):
        return "drift"
    return None


def compaction_canary(transcripts_dir: Path, sample_files: int = _CANARY_SAMPLE_FILES) -> tuple[str, str]:
    """Sample the newest `sample_files` transcripts for a compaction-shaped
    turn and check it still carries the consent-gate flag key(s)
    `extract_user_text` depends on.

    Returns a ``(status, detail)`` pair. ``status`` is one of:

    - ``"verified"``: a compaction-shaped turn was found and carries one of
      the expected flag keys.
    - ``"drift"``: a compaction-shaped turn was found (matched on the
      documented boilerplate prefix) but carries NEITHER expected flag key --
      if this happens for real, `extract_user_text` would silently treat it
      as an ordinary operator turn (the v0.2.1 incident shape).
    - ``"unverified"``: no compaction-shaped turn was found anywhere in the
      sample, including a missing or empty transcripts directory. Nothing to
      check, so nothing is claimed -- this is never reported as "drift".

    Never raises: a missing directory, an unreadable file, a non-JSON line,
    or a malformed entry inside the sample is skipped, not fatal. This is an
    advisory sample used by `doctor` and `apply`'s preflight, not the consent
    path itself (which still raises `TranscriptSchemaError` loudly on an
    unrecognized shape; see `extract_user_text`).
    """
    if not transcripts_dir.is_dir():
        return "unverified", "unverified — no compaction sample (transcripts directory does not exist)"
    transcripts = sorted(transcripts_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)[-sample_files:]
    if not transcripts:
        return "unverified", "unverified — no compaction sample (no *.jsonl files found)"

    drift_in: str | None = None
    verified_in: str | None = None
    for path in transcripts:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            outcome = _classify_canary_turn(entry)
            if outcome == "drift" and drift_in is None:
                drift_in = path.name
            elif outcome == "verified" and verified_in is None:
                verified_in = path.name

    if drift_in is not None:
        return (
            "drift",
            f"drift — compaction-shaped turn in {drift_in} carries neither "
            "isCompactSummary nor isVisibleInTranscriptOnly",
        )
    if verified_in is not None:
        return "verified", f"verified — compaction-shaped turn in {verified_in} carries the expected flag key(s)"
    return (
        "unverified",
        f"unverified — no compaction sample (no compaction-shaped turn in the newest "
        f"{sample_files} sampled transcript(s))",
    )


def extract_user_text(entry: dict[str, Any]) -> str | None:
    """Return the human-typed text of a transcript entry, or None if it is not one.

    Tool results, assistant turns, meta entries, compaction summaries, and CLI
    wrapper-tag content all return None: only genuine operator-typed prose can
    carry consent.

    An entry that looks like a real operator turn (``type == "user"``, not
    ``isMeta``) but whose ``message`` payload matches neither known content
    shape (a plain string, or a list of block dicts) raises
    `TranscriptSchemaError` naming what was found and what was expected,
    instead of silently returning None — see the module docstring.
    """
    if entry.get("type") != "user" or entry.get("isMeta"):
        return None
    # Claude Code's post-compaction continuation turn is written to the
    # transcript as ``type == "user"`` with ``role == "user"`` and NO
    # ``isMeta``, so every check above passes it. Its text is model-written
    # prose summarizing the conversation that was just dropped.
    #
    # Why this is a consent bug and not a cosmetic one: the summarizer quotes
    # prior conversation verbatim, and ``run_trace`` scans BACKWARD from the
    # end of the transcript for the most recent turn carrying the approval
    # token. So a compaction landing between "preview generated" and "operator
    # approves" can reproduce the token inside a turn no human typed, and that
    # synthetic turn is then PREFERRED over a genuine earlier approval because
    # it is later in the file. That is precisely the property this gate exists
    # to guarantee: a pass that drafts and applies with no post-preview human
    # turn must not be able to approve itself.
    #
    # Keyed on the structural flags rather than on summary prose, which is
    # model-written and unstable.
    #
    # Full-corpus measurement, 2026-08-11, 20,470 transcript files: 2098 entries
    # carry these flags; ALL 2098 carry BOTH of them (so the ``or`` cannot
    # over-reject — no entry has one flag without the other), 0 are non-user,
    # 0 carry ``isMeta``, and 0 deviate from the standard continuation prose.
    # Every one of those 2098 would have passed the checks above before this
    # guard. So it rejects no real approval, and it is not a sampling claim.
    if entry.get("isCompactSummary") or entry.get("isVisibleInTranscriptOnly"):
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
            config.emit_json(trace, getattr(args, "out_file", None), sort_keys=True)
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
    config.add_out_file_arg(trace, "operator_trace")
    trace.set_defaults(func=run_trace)

    locate = subparsers.add_parser(
        "transcript-locate", help="print the newest transcript path for the current working directory"
    )
    locate.set_defaults(func=run_transcript_locate)
