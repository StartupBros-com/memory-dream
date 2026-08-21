#!/usr/bin/env python3
"""Sanitization regression guard: no private-harness references ship.

This project was extracted from a personal Claude Code harness. The
extraction design (docs/EXTRACTION-DESIGN.md) lists the exact strings and
shorthand that must never appear in a shipped file: local filesystem paths,
the private harness's own script/workflow names, its internal doctrine
shorthand (gate codes like R7 or KTD5 -- the public docs use gate *names*
instead, see the design doc's table), and private issue/PR numbers that mean
nothing outside the original repo.

Every tracked file is scanned except this script (it necessarily quotes the
patterns it looks for) and docs/EXTRACTION-DESIGN.md (the design record,
which documents the source it was extracted from and is explicitly exempt).

`CLAUDE_JOB_DIR` is a single, deliberate exception: memory_dream/config.py
references it as one link in the scratch-directory resolution chain, and the
tests name it only to STRIP it from a subprocess env (so a developer running
the suite inside a Claude Code job cannot have real scratch leak in) -- both
references are the point of the design, not a leak. The exemption below
therefore covers config.py and the whole `tests/` tree. Only the env-var NAME
is exempted there; a leaked path VALUE would still trip the `/home/will`
pattern, so this does not reduce protection.

This is a repo-maintenance tool. It is not part of the shipped plugin, makes
no model calls, and imports only stdlib itself.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
DESIGN_DOC = REPO_ROOT / "docs" / "EXTRACTION-DESIGN.md"
EXCLUDED_FILES = {SELF, DESIGN_DOC}

# (label, literal needle, case_insensitive, {relpaths exempt from just this
# pattern}). Exemptions are per-pattern, not per-file: an exempt file is
# still scanned for every other pattern. An exemption entry ending in "/" is a
# directory prefix (e.g. "tests/" exempts every file under tests/); any other
# entry is an exact repo-relative path.
LITERAL_PATTERNS: list[tuple[str, str, bool, frozenset[str]]] = [
    ("private-home-path", "/home/will", False, frozenset()),
    ("private-dotfiles-path", "~/dotfiles", True, frozenset()),
    ("private-repo-name", "dotfiles", True, frozenset()),
    ("private-harness-script", "sync.sh", True, frozenset()),
    ("private-harness-workflow", "memory-push", True, frozenset()),
    ("private-harness-workflow", "harness-weekly", True, frozenset()),
    ("private-harness-workflow", "memory-mine", True, frozenset()),
    ("private-harness-env-var", "CLAUDE_JOB_DIR", False, frozenset({"memory_dream/config.py", "tests/"})),
    ("private-project-codename", "prbot", True, frozenset()),
    ("private-project-codename", "pi-evals", True, frozenset()),
    ("private-project-codename", "wsl-cdp", True, frozenset()),
]

# Doctrine shorthand: internal gate codes (R7, R17, KTD5, AE8, ...). The
# public docs spell out gate names instead (see EXTRACTION-DESIGN.md's
# "Gate names" table) -- a bare code surviving into a shipped file means the
# rewrite missed a spot.
#
# One deliberate exception: docs/ARCHITECTURE.md's closing glossary table
# exists specifically to map these old codes to the public gate names for
# anyone cross-referencing an older discussion (this is what
# EXTRACTION-DESIGN.md's "GLOSSARY note lives in ARCHITECTURE.md" calls
# for) -- the codes appearing there are the point, not a leak. Every other
# check (literal patterns, issue refs) still applies to that file in full.
DOCTRINE_RE = re.compile(r"\b(?:R1?[0-9]|KTD[0-9]+|AE[0-9]+)\b")
DOCTRINE_EXEMPT_FILES = frozenset({"docs/ARCHITECTURE.md"})

# Private PR/issue references: either an explicit "PR #123" / "issue #61" /
# "pull #9", or a bare "#123"-style reference that isn't a hex color or a
# markdown heading. The negative lookbehind keeps it off HTML numeric
# entities (`&#123;`) and identifiers glued to a `#`; the negative lookahead
# keeps it off hex colors that have letters in them (`#1a2b3c`). Markdown
# headings (`## Title`) never match because the character right after `#` is
# another `#` or whitespace, never a digit.
ISSUE_REF_RE = re.compile(
    r"(?:PR|issue|pull)\s*#[0-9]+" r"|(?<![\w&])#[0-9]{2,}(?![0-9A-Fa-f]{1,4}\b)",
    re.IGNORECASE,
)


def tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [REPO_ROOT / line for line in out.splitlines() if line]


def _exempt(rel: str, exempt: frozenset[str]) -> bool:
    """True if rel is exactly an exempt path, or lives under an exempt directory
    (an exemption entry ending in "/" is treated as a directory prefix)."""
    return any(rel == entry or (entry.endswith("/") and rel.startswith(entry)) for entry in exempt)


def scan_file(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []  # binary or unreadable; nothing to scan as text

    rel = path.relative_to(REPO_ROOT).as_posix()
    violations: list[str] = []

    for lineno, line in enumerate(text.splitlines(), start=1):
        for label, needle, case_insensitive, exempt in LITERAL_PATTERNS:
            if _exempt(rel, exempt):
                continue
            haystack = line.lower() if case_insensitive else line
            target = needle.lower() if case_insensitive else needle
            if target in haystack:
                violations.append(f"{rel}:{lineno}:{label} ({needle!r})")

        if rel not in DOCTRINE_EXEMPT_FILES:
            m_doctrine = DOCTRINE_RE.search(line)
            if m_doctrine:
                violations.append(f"{rel}:{lineno}:doctrine-shorthand ({m_doctrine.group()!r})")

        m = ISSUE_REF_RE.search(line)
        if m:
            violations.append(f"{rel}:{lineno}:private-issue-ref ({m.group()!r})")

    return violations


def main() -> int:
    violations: list[str] = []
    checked = 0

    for path in tracked_files():
        if path.resolve() in EXCLUDED_FILES:
            continue
        if not path.is_file():
            continue  # git ls-files can list a path that's since been removed on disk
        checked += 1
        violations.extend(scan_file(path))

    if violations:
        print("Private-harness references found (sanitization contract violation):")
        for line in violations:
            print(f"  {line}")
        print(f"\n{len(violations)} violation(s) across {checked} tracked file(s) checked.")
        return 1

    print(f"no-private-refs: OK ({checked} tracked file(s) checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
