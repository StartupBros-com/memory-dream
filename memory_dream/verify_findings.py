"""verify-findings: advisory quote-existence gate for panel/verifier findings.

Stages 3.5 (fidelity), 3.7 (checker-check), and 3.8 (quality panel) of the
dream pass each emit findings that quote a source file to ground a claim
before the session model adjudicates them. A quote that does not actually
appear in its cited file is exactly the fabrication shape one pass hit in
practice (2026-07-20): a finding whose "supporting" text was invented rather
than read from the file it claimed to cite. This subcommand is a mechanical,
code-run gate over a stage's findings file: it never opens a path that fails
confinement, substring-checks each finding's quote against its cited file,
and stamps the result back onto the finding so adjudication can treat an
unverified quote as unverifiable rather than trusting it silently.

Advisory only: this never blocks the pass and never drops a finding. A
finding whose quote cannot be confirmed is stamped and kept, not deleted --
the operator (or the session model doing adjudication) decides what an
unverifiable finding means; this gate only makes the fact visible. Every
code path here returns 0, including a malformed input file (reported to
stderr, left untouched on disk).

Input/output shape (round-tripped in place; every original field is
preserved, only ``quote_checked``/``unverified_quote`` are added or
overwritten on each finding):

    {"files": [{"path": "rel/note.md", "findings": [
        {"severity": "high", "claim": "...", "problem": "...", "fix": "...",
         "quote": "verbatim span from rel/note.md"},
        ...
    ]}, ...]}

A bare JSON list of the same per-file objects (no "files" wrapper) is also
accepted, mirroring recall_eval.load_routes' tolerance for either shape.

Each finding this gate reaches is stamped with ``quote_checked``: ``true``
means the gate ran the substring check and it passed. Every path where
verification did NOT succeed -- a missing/empty ``quote`` field, a cited
path that fails confinement or does not resolve to an existing file, an
unreadable file, or a quote the substring check could not find -- stamps
``quote_checked: false`` plus ``unverified_quote: true``. A finding a
stage's gate invocation never reached at all carries neither key, so "the
gate ran and could not verify" stays distinguishable downstream from "the
gate never ran on this finding".

Known limitation (Stage 3.5 fidelity findings): the fidelity prompt binds
each finding's ``path`` to the PROPOSED destination file while its
``quote`` is copied verbatim from a source note, and the gate runs before
build. For a destination that does not exist on disk yet (extract, split,
merge) ``unverified_quote`` is therefore expected and carries no
fabrication signal -- adjudication must check those quotes against the
cluster's source notes directly instead of trusting the stamp either way.
Stages 3.7 and 3.8 cite files that already exist, so their stamps carry
full signal. (commands/dream.md documents the same caveat in the Stage 3.5
disposition lane.)

Path confinement reuses ``audit.confined_path`` (the same helper
``recall_eval``'s snippet-fabrication check relies on), so a finding citing
an absolute path, a ``..``-escaping path, or anything outside its base root
is stamped unverifiable WITHOUT the file ever being opened. Quote matching
mirrors ``recall_eval.normalize`` (whitespace-collapsed, case-folded) so a
verbatim quote that merely wraps differently still matches.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from memory_dream import audit as AUDIT
from memory_dream.recall_eval import normalize

SCHEMA_VERSION = 1

# Stamp shape shared by every "could not verify" exit out of verify_quote:
# missing/empty quote, a non-string or unconfined path, a path that does not
# resolve to an existing file, an unreadable file, or a quote not found.
_UNVERIFIED = {"quote_checked": False, "unverified_quote": True}


def _normalized_file_content(root: Path, path: Any) -> str | None:
    """Whitespace/case-normalized content of ``path`` confined to ``root``,
    or None when the path is not a confined, readable file.

    Never raises, and never opens a file for a path that fails confinement --
    the confinement check runs before any read is attempted, so an escaping
    or unresolvable ``path`` is rejected without the target file ever being
    touched. A missing file (or a directory) surfaces as the read error the
    OSError guard catches; no separate existence pre-check is needed.
    """
    if not isinstance(path, str) or not path:
        return None
    resolved = AUDIT.confined_path(root, path)
    if resolved is None:
        return None
    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return normalize(content)


def _stamps_for(normalized_content: str | None, quote: str) -> dict[str, bool]:
    if normalized_content is None:
        return dict(_UNVERIFIED)
    if normalize(quote) in normalized_content:
        return {"quote_checked": True}
    return dict(_UNVERIFIED)


def verify_quote(root: Path, path: Any, quote: Any) -> dict[str, bool]:
    """Stamps for one finding's quote-existence check: the quote (if any)
    substring-matched (whitespace/case-normalized) against the file at
    ``path``, confined to ``root``. Never raises; see
    ``_normalized_file_content`` for the confinement-before-read guarantee.
    """
    if not isinstance(quote, str) or not quote.strip():
        return dict(_UNVERIFIED)
    return _stamps_for(_normalized_file_content(root, path), quote)


def verify_findings_payload(data: Any, root: Path) -> tuple[Any, int, int]:
    """Stamp every finding reachable from ``data`` in place and return
    ``(data, checked, unverified)``. ``checked`` counts every finding the
    gate stamped; ``unverified`` counts how many of those failed
    verification.

    Accepts either ``{"files": [...]}`` or a bare list of the same per-file
    objects. Tolerant of a malformed shape at any level -- a non-dict file
    entry, or a "findings" value that is not a list -- which is left
    untouched rather than raising: this gate degrades a bad shape to
    "nothing stamped here", never a crash, matching its advisory contract.

    Each unique cited path is confined, read, and normalized at most once
    per invocation, however many findings cite it.
    """
    entries = data.get("files", data) if isinstance(data, dict) else data
    checked = 0
    unverified = 0
    if not isinstance(entries, list):
        return data, checked, unverified
    content_cache: dict[str, str | None] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        findings = entry.get("findings")
        if not isinstance(findings, list):
            continue
        path = entry.get("path")
        for item in findings:
            if not isinstance(item, dict):
                continue
            quote = item.get("quote")
            if not isinstance(quote, str) or not quote.strip():
                stamps = dict(_UNVERIFIED)
            else:
                if isinstance(path, str) and path in content_cache:
                    normalized_content = content_cache[path]
                else:
                    normalized_content = _normalized_file_content(root, path)
                    if isinstance(path, str):
                        content_cache[path] = normalized_content
                stamps = _stamps_for(normalized_content, quote)
            item.update(stamps)
            checked += 1
            if stamps.get("unverified_quote"):
                unverified += 1
    return data, checked, unverified


def run_verify_findings(args: argparse.Namespace) -> int:
    findings_path = Path(args.findings).expanduser()
    root = Path(args.root).expanduser()
    try:
        raw = findings_path.read_text(encoding="utf-8")
    except OSError as error:
        print(f"memory-dream verify-findings: cannot read {findings_path}: {error}", file=sys.stderr)
        return 0  # advisory: never a nonzero exit, even on an unreadable input
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        print(f"memory-dream verify-findings: {findings_path} is not valid JSON: {error}", file=sys.stderr)
        return 0  # operator error, reported; the file is left exactly as found

    data, checked, unverified = verify_findings_payload(data, root)

    try:
        AUDIT.atomic_write(findings_path, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    except OSError as error:
        print(f"memory-dream verify-findings: cannot write {findings_path}: {error}", file=sys.stderr)
        return 0  # advisory: an unwritable output degrades to no stamps, never a crash
    print(f"memory-dream verify-findings: checked {checked}, unverified {unverified} -> {findings_path}")
    return 0


def add_parsers(subparsers) -> None:
    p = subparsers.add_parser(
        "verify-findings",
        help="advisory gate: verify each finding's quote against its cited file, stamp in place (always exits 0)",
        description=__doc__.splitlines()[0],
    )
    p.add_argument("--findings", required=True, help="findings JSON file to verify and rewrite in place")
    p.add_argument(
        "--root",
        required=True,
        help="base root every finding's cited path is confined against before opening",
    )
    p.set_defaults(func=run_verify_findings)
