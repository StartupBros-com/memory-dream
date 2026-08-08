"""Structural audit, deterministic triage scoring, and mechanical repair for
Claude Code per-project memory stores.

Three subcommands share one live-memory reader:

  audit  - structural findings (broken links, invalid frontmatter, sensitive
           content/filename indicators, oversized index, orphaned staging
           files, stale-dated review candidates), optionally cross-checked
           against a git-tracked mirror.
  triage - read-only consolidation-candidate scoring for the dream pass.
  fix    - mechanical wikilink and index repairs; dry-run unless --apply.

Memory contents are never printed or logged: findings carry structural facts
(paths, byte counts, regex hits), never note bodies.

Word tokenization for description-similarity checks is Unicode-aware
(``re.findall(r"\\w+", text.lower())`` semantics, not an ASCII-only [a-z0-9]
allowlist), so a non-English corpus tokenizes sensibly. The Jaccard
thresholds that consume it were calibrated on an English corpus (see
docs/TUNING.md).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from memory_dream import config

ALLOWED_TYPES = {"user", "feedback", "project", "reference"}
YAML_LITERALS = {"null", "~", "true", "false", "none"}
LINK_RE = re.compile(r"\]\((?P<target><[^>]+>|(?:\\.|[^)])+)\)")
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
DATE_PATTERNS = (
    re.compile(r"(?i)\b(?:status|checkpoint|status\s+as\s+of|checkpoint\s+as\s+of)\s*[:=-]?\s*(20\d{2}-\d{2}-\d{2})\b"),
    re.compile(r"(?i)\b(?:status|checkpoint)\s+date\s*[:=-]\s*(20\d{2}-\d{2}-\d{2})\b"),
)
SENSITIVE_FILENAME_RE = re.compile(
    r"(?i)(?:^|[._-])(secret|credential|password|passwd|api[_-]?key|access[_-]?token|private[_-]?key)(?:[._-]|$)"
)
SENSITIVE_KEY_RE = re.compile(r"(?i)(secret|credential|password|passwd|token|api[_-]?key|private[_-]?key)")
# Historical write bug: an unquoted '#' in a frontmatter value starts a YAML
# comment and silently truncates the value. Flag descriptions ending in the
# characteristic dangling fragments this leaves behind ("shipped as PR", "(issue").
# Case-sensitive on purpose: "PR"/"issue" match how references are written, and
# lowercase-only articles/prepositions avoid flagging values that legitimately
# end in words like "ON" or "-A".
TRUNCATED_DESCRIPTION_RE = re.compile(
    r"(?:[(,;:]|\bPRs?|\bissues?|\s(?:the|an?|and|with|via|after|as|for|to|of|on|by|at))$"
)
CONTENT_SIGNATURES = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b(?:ghp|github_pat|sk_live|sk_test)_[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]\s*['\"]?[A-Za-z0-9_./+\-=]{16,}"),
)


def _is_sensitive_filename(name: str) -> bool:
    """True when `name` matches the built-in sensitive-filename signature or any
    extra pattern from config.SENSITIVE_PATTERNS_EXTRA (a list of regex strings a
    config file may add, matched as-written against the same bare filename). This
    is the extension point for a fork's own naming conventions without forking
    SENSITIVE_FILENAME_RE itself."""
    if SENSITIVE_FILENAME_RE.search(name):
        return True
    return any(re.search(pattern, name) for pattern in config.SENSITIVE_PATTERNS_EXTRA)


# --- Dream-pass triage (stage one) -------------------------------------------
# Additive scoring over structural signals only. Live memory is not a git
# repository, so the operative age signal is live-file mtime; mirror git
# corroboration is advisory and not wired into v1. Every triage threshold below
# is a named value in config.py (config.TRIAGE_*, config.INDEX_LOAD_MAX_*),
# referenced at use time so a config-file override always takes effect.
# Supersession markers are case-sensitive line-leading tokens, so
# "SUPERSEDED"/"CORRECTED"/"RESOLVED" status lines count while prose mentions
# ("resolved the bug") do not.
SUPERSESSION_RE = re.compile(r"(?m)^[\s>*_+-]*(?:SUPERSEDED|CORRECTED|RESOLVED)\b")
# A note is flagged only on STRUCTURAL rot: at least one supersession marker, or
# a body past the log-shaped size threshold (config.TRIAGE_BODY_BYTES). Age and
# zero-inbound are ranking boosts only, never independent flag triggers, so a
# merely-old orphan note (the bulk of a mature store) does not manufacture a
# consolidation proposal. See docs/TUNING.md for the corpus these size and age
# thresholds were calibrated against.
TRIAGE_SUPERSESSION_FLAG = 1  # >=1 supersession marker flags a note


def digest(path: Path) -> str:
    if path.is_symlink():
        value = b"symlink\0" + os.fsencode(os.readlink(path))
    else:
        value = path.read_bytes()
    return hashlib.sha256(value).hexdigest()


def project_dirs(root: Path, live: bool) -> dict[str, Path]:
    if not root.is_dir():
        return {}
    if live:
        return {
            parent.name: memory
            for parent in sorted(root.iterdir(), key=lambda item: item.name)
            if parent.is_dir() and (memory := parent / "memory").is_dir()
        }
    return {item.name: item for item in sorted(root.iterdir(), key=lambda item: item.name) if item.is_dir()}


def files(directory: Path, markdown_only: bool = False) -> dict[str, Path]:
    pattern = "*.md" if markdown_only else "*"
    return {
        path.relative_to(directory).as_posix(): path
        for path in sorted(directory.rglob(pattern), key=lambda item: item.as_posix())
        if path.is_file() or path.is_symlink()
    }


def scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return value.strip()


def parse_frontmatter(text: str) -> tuple[dict[str, Any] | None, str | None, str, dict[str, str]]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, "missing YAML frontmatter", text, {}
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration:
        return None, "unterminated YAML frontmatter", text, {}
    data: dict[str, Any] = {}
    metadata: dict[str, str] = {}
    raw: dict[str, str] = {}
    in_metadata = False
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^(?P<indent>\s*)(?P<key>[A-Za-z0-9_.-]+)\s*:\s*(?P<value>.*)$", line)
        if not match:
            continue
        indent, key, value = match.group("indent"), match.group("key"), match.group("value")
        if not indent:
            in_metadata = key == "metadata"
            if in_metadata:
                data["metadata"] = metadata
            else:
                data[key] = scalar(value)
                raw[key] = value.strip()
        elif in_metadata:
            metadata[key] = scalar(value)
    return data, None, "\n".join(lines[end + 1 :]), raw


def index_targets(text: str) -> tuple[set[str], set[str]]:
    targets: set[str] = set()
    escaping: set[str] = set()
    for match in LINK_RE.finditer(text):
        target = match.group("target")
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1]
        target = re.sub(r"\\([()\\])", r"\1", target)
        target = unquote(target.split("#", 1)[0])
        if target.startswith("./"):
            target = target[2:]
        path = Path(target)
        if not target.lower().endswith(".md"):
            continue
        if path.is_absolute() or ".." in path.parts:
            escaping.add(target)
        else:
            targets.add(path.as_posix())
    return targets, escaping


def canon(value: str) -> str:
    value = value.strip().lower()
    if value.endswith(".md"):
        value = value[:-3]
    value = value.replace("-", "_")
    value = re.sub(r"[^a-z0-9_]+", "_", value)
    return re.sub(r"_+", "_", value).strip("_")


def finding(kind: str, project: str, path: str | None = None, **details: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"kind": kind, "project": project}
    if path is not None:
        result["path"] = path
    result.update(details)
    return result


def audit(args: argparse.Namespace) -> dict[str, Any]:
    live_root = Path(args.live_root).expanduser()
    mirror_root = Path(args.mirror_root).expanduser() if args.mirror_root else None
    if not live_root.is_dir():
        raise OSError(f"live root is not a directory: {live_root}")
    live = project_dirs(live_root, live=True)
    mirror = project_dirs(mirror_root, live=False) if mirror_root is not None else {}
    findings: list[dict[str, Any]] = []

    projects = sorted(set(live) | set(mirror)) if mirror_root is not None else sorted(live)
    for project in projects:
        if mirror_root is not None:
            if project not in live:
                findings.append(finding("mirror_only_project", project))
                continue
            if project not in mirror:
                findings.append(finding("mirror_missing_project", project))
        memory_dir = live[project]
        if memory_dir.is_symlink() or memory_dir.parent.is_symlink():
            # A symlinked project or memory root would let a mirror sync copy an
            # arbitrary external tree; block and skip traversal entirely.
            findings.append(finding("live_symlink", project, "" if memory_dir.parent.is_symlink() else "memory"))
            continue
        live_all_files = files(memory_dir)
        mirror_all_files = files(mirror[project]) if project in mirror else {}
        for relative, path in sorted(live_all_files.items()):
            if path.is_symlink():
                findings.append(finding("live_symlink", project, relative))
                continue
            if relative.endswith(".dream-tmp"):
                # atomic_write's temp sibling renames away instantly on success; a
                # surviving one means a write crashed mid-rewrite. `doctor` scans
                # for these too; the audit surfaces exactly which file to inspect.
                findings.append(finding("orphaned_dream_tmp", project, relative))
            # Everything here would be copied by a mirror sync, so the sensitive
            # scan covers every file: MEMORY.md, frontmatter, and attachments.
            if _is_sensitive_filename(Path(relative).name):
                findings.append(finding("sensitive_filename_indicator", project, relative))
            if args.scan_content:
                try:
                    raw_text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    findings.append(finding("unreadable_file", project, relative))
                    continue
                if any(pattern.search(raw_text) for pattern in CONTENT_SIGNATURES):
                    findings.append(finding("sensitive_content_indicator", project, relative))
        live_files = files(memory_dir, markdown_only=True)
        index = live_files.get("MEMORY.md")
        if index is None or index.is_symlink():
            if index is None:
                findings.append(finding("missing_memory_index", project, "MEMORY.md"))
            targets: set[str] = set()
            escaping_targets: set[str] = set()
        else:
            index_text = index.read_text(encoding="utf-8", errors="replace")
            targets, escaping_targets = index_targets(index_text)
            for target in sorted(escaping_targets):
                findings.append(finding("escaping_index_target", project, target))
            size = index.stat().st_size
            lines = len(index_text.splitlines())
            if size > args.max_index_bytes or lines > args.max_index_lines:
                findings.append(
                    finding(
                        "oversized_memory_index",
                        project,
                        "MEMORY.md",
                        bytes=size,
                        lines=lines,
                        max_bytes=args.max_index_bytes,
                        max_lines=args.max_index_lines,
                    )
                )
        note_names: dict[str, list[str]] = {}
        note_stems: set[str] = set()
        note_bodies: dict[str, str] = {}
        for relative, path in sorted(live_files.items()):
            if relative in ("MEMORY.md", "MEMORY-archive.md"):
                continue
            if path.is_symlink():
                continue
            note_stems.add(Path(relative).name[:-3])
            if relative not in targets:
                findings.append(finding("unindexed_markdown", project, relative))
            text = path.read_text(encoding="utf-8", errors="replace")
            metadata, parse_error, body, raw_values = parse_frontmatter(text)
            note_bodies[relative] = body
            problems: list[str] = []
            if parse_error:
                problems.append(parse_error)
            else:
                assert metadata is not None
                for key in ("name", "description"):
                    value = metadata.get(key)
                    if not isinstance(value, str) or not value.strip():
                        problems.append(f"missing or empty {key}")
                    elif raw_values.get(key, "").strip().lower() in YAML_LITERALS:
                        # An unquoted null/true/false is a YAML non-string, not a name.
                        problems.append(f"{key} is a YAML literal, not a string")
                description = metadata.get("description")
                if isinstance(description, str) and description.strip():
                    tail = description.strip()
                    # Unbalanced open-paren: the same unquoted-# YAML truncation bug,
                    # cut mid-parenthetical so no tracked trailing token remains.
                    if (
                        TRUNCATED_DESCRIPTION_RE.search(tail)
                        or re.fullmatch(r"20\d{2}-\d{2}-\d{2}", tail)
                        or tail.count("(") > tail.count(")")
                    ):
                        findings.append(finding("truncated_description", project, relative))
                nested = metadata.get("metadata")
                nested_type = nested.get("type") if isinstance(nested, dict) else None
                note_type = nested_type or metadata.get("type")
                if note_type not in ALLOWED_TYPES:
                    problems.append("type or metadata.type must be user|feedback|project|reference")
                # Decay enforcement (added 2026-07-31 during the authors' consolidation
                # campaign): notes carrying the mined-note fields lose confidence on a
                # half-life since last_validated (config.DECAY_HALF_LIFE_DAYS days).
                # Effective confidence below config.DECAY_FLAG_THRESHOLD => dream-pass
                # candidate: revalidate or archive.
                if isinstance(nested, dict) and "confidence" in nested and "last_validated" in nested:
                    decay = decay_effective(nested, dt.date.today())
                    if decay is None:
                        problems.append("confidence/last_validated present but unparseable")
                    else:
                        effective, age_days = decay
                        if effective < config.DECAY_FLAG_THRESHOLD:
                            findings.append(
                                finding(
                                    "decayed_confidence",
                                    project,
                                    relative,
                                    confidence=float(nested["confidence"]),
                                    age_days=age_days,
                                    effective=round(effective, 3),
                                )
                            )
                name = metadata.get("name")
                if isinstance(name, str) and name.strip():
                    note_names.setdefault(name.strip(), []).append(relative)
                sensitive_keys = sorted(
                    key
                    for key in metadata
                    if SENSITIVE_KEY_RE.search(key)
                )
                if isinstance(nested, dict):
                    sensitive_keys.extend(f"metadata.{key}" for key in nested if SENSITIVE_KEY_RE.search(key))
                if sensitive_keys:
                    findings.append(
                        finding("sensitive_frontmatter_indicator", project, relative, indicators=sorted(set(sensitive_keys)))
                    )
            if problems:
                findings.append(finding("invalid_frontmatter", project, relative, problems=sorted(problems)))
            if args.as_of:
                dates: set[dt.date] = set()
                # Patterns require an explicit status/checkpoint label, so scanning
                # frontmatter and body remains high-confidence without broad date noise.
                for pattern in DATE_PATTERNS:
                    for value in pattern.findall(text):
                        try:
                            dates.add(dt.date.fromisoformat(value))
                        except ValueError:
                            pass
                stale_dates = sorted(value for value in dates if (args.as_of - value).days > args.stale_days)
                if stale_dates:
                    findings.append(
                        finding(
                            "stale_date_review",
                            project,
                            relative,
                            dates=[value.isoformat() for value in stale_dates],
                            stale_days=args.stale_days,
                        )
                    )
        for name, paths in sorted(note_names.items()):
            if len(paths) > 1:
                findings.append(finding("duplicate_frontmatter_name", project, paths=sorted(paths), count=len(paths)))
        for target in sorted(targets - set(live_files)):
            findings.append(finding("missing_index_target", project, target))

        # Body [[wikilinks]] that resolve to exactly one existing note under a
        # different slug are typos worth fixing; links with no close match are
        # allowed forward references and stay silent.
        raw_names = {name for name in note_names}
        by_canon: dict[str, set[str]] = {}
        for stem in note_stems:
            by_canon.setdefault(canon(stem), set()).add(stem)
            for prefix in ("feedback_", "project_", "reference_"):
                if stem.startswith(prefix):
                    by_canon.setdefault(canon(stem[len(prefix):]), set()).add(stem)
        for name in raw_names:
            by_canon.setdefault(canon(name), set()).add(name)
        for relative, body in sorted(note_bodies.items()):
            for match in WIKILINK_RE.finditer(body):
                link = match.group(1)
                if link in note_stems or link in raw_names:
                    continue
                candidates = by_canon.get(canon(re.sub(r"\s+", "", link) if "\n" in link else link), set())
                if len(candidates) == 1 and next(iter(candidates)) != link:
                    findings.append(
                        finding("wikilink_typo", project, relative, link=link, resolves_to=next(iter(candidates)))
                    )

        if mirror_root is not None and project in mirror:
            for relative in sorted(set(live_all_files) | set(mirror_all_files)):
                if relative not in live_all_files:
                    findings.append(finding("mirror_only_file", project, relative))
                elif relative not in mirror_all_files:
                    findings.append(finding("mirror_missing_file", project, relative))
                elif digest(live_all_files[relative]) != digest(mirror_all_files[relative]):
                    findings.append(finding("mirror_stale_file", project, relative))

    findings.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    counts = dict(sorted(Counter(item["kind"] for item in findings).items()))
    blocking = sum(count for kind, count in counts.items() if kind != "stale_date_review")
    result: dict[str, Any] = {
        "schema_version": 1,
        "roots": {"live": str(live_root), "mirror": str(mirror_root) if mirror_root is not None else None},
        "summary": {
            "live_projects": len(live),
            "mirror_projects": len(mirror),
            "findings": len(findings),
            "blocking_findings": blocking,
            "review_candidates": counts.get("stale_date_review", 0),
            "by_kind": counts,
        },
        "findings": findings,
    }
    if args.as_of:
        result["as_of"] = args.as_of.isoformat()
    return result


def render_human(result: dict[str, Any]) -> str:
    summary = result["summary"]
    lines = [
        "memory-audit: "
        f"{summary['live_projects']} live, {summary['mirror_projects']} mirrored, "
        f"{summary['blocking_findings']} actionable, {summary['review_candidates']} review candidate(s)"
    ]
    for kind, count in summary["by_kind"].items():
        lines.append(f"  {kind}: {count}")
    return "\n".join(lines)


# --- Shared dream-pass helpers -----------------------------------------------


def decay_effective(nested: Any, today: dt.date) -> tuple[float, int] | None:
    """(effective_confidence, age_days) for a note's confidence/last_validated
    decay pair (0.5 half-life, config.DECAY_HALF_LIFE_DAYS-day period), or None
    when the pair is missing OR present but unparseable. The single shared
    computation for both audit() and triage_project() so the half-life formula
    can never drift apart between call sites; each call site applies its own
    config.DECAY_FLAG_THRESHOLD and, for audit(), its own presence check to
    distinguish "absent" (no problem) from "present but unparseable" (an
    invalid_frontmatter problem)."""
    if not (isinstance(nested, dict) and "confidence" in nested and "last_validated" in nested):
        return None
    try:
        conf = float(nested["confidence"])
        validated = dt.date.fromisoformat(str(nested["last_validated"]).strip())
    except (TypeError, ValueError):
        return None
    age_days = (today - validated).days
    effective = conf * (0.5 ** (max(age_days, 0) / config.DECAY_HALF_LIFE_DAYS))
    return effective, age_days


def scan_project_notes(memory_dir: Path) -> dict[str, dict[str, Any]]:
    """Per-note structural records for one project (excludes MEMORY.md and symlinks)."""
    records: dict[str, dict[str, Any]] = {}
    for relative, path in sorted(files(memory_dir, markdown_only=True).items()):
        if relative in ("MEMORY.md", "MEMORY-archive.md") or path.is_symlink():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        metadata, _error, body, _raw = parse_frontmatter(text)
        name = None
        description = None
        if isinstance(metadata, dict):
            candidate = metadata.get("name")
            if isinstance(candidate, str) and candidate.strip():
                name = candidate.strip()
            candidate = metadata.get("description")
            if isinstance(candidate, str) and candidate.strip():
                description = candidate.strip()
        stat = path.stat()
        nested = metadata.get("metadata") if isinstance(metadata, dict) else None
        records[relative] = {
            "body": body,
            "body_bytes": len(body.encode("utf-8")),
            "mtime": stat.st_mtime,
            "stem": Path(relative).name[:-3],
            "name": name,
            "description": description,
            "nested_meta": nested if isinstance(nested, dict) else {},
        }
    return records


def project_by_canon(stems: set[str], names: set[str]) -> dict[str, set[str]]:
    """Canonical-form graph mirroring the auditor's wikilink_typo resolution."""
    by_canon: dict[str, set[str]] = {}
    for stem in stems:
        by_canon.setdefault(canon(stem), set()).add(stem)
        for prefix in ("feedback_", "project_", "reference_"):
            if stem.startswith(prefix):
                by_canon.setdefault(canon(stem[len(prefix):]), set()).add(stem)
    for name in names:
        by_canon.setdefault(canon(name), set()).add(name)
    return by_canon


def inbound_index(records: dict[str, dict[str, Any]]) -> dict[str, set[str]]:
    """Target rel -> set of source rels that [[wikilink]] to it (v1: wikilinks only).

    Doubles as the inbound-wikilink retargeting index: the same single-candidate
    resolution the auditor uses for wikilink_typo, so a link resolves only when
    exactly one note owns its canonical form.
    """
    stems = {record["stem"] for record in records.values()}
    names = {record["name"] for record in records.values() if record["name"]}
    by_canon = project_by_canon(stems, names)
    token_to_rel: dict[str, set[str]] = {}
    for rel, record in records.items():
        token_to_rel.setdefault(record["stem"], set()).add(rel)
        if record["name"]:
            token_to_rel.setdefault(record["name"], set()).add(rel)
    index: dict[str, set[str]] = {rel: set() for rel in records}
    for source, record in records.items():
        for match in WIKILINK_RE.finditer(record["body"]):
            link = match.group(1)
            key = canon(re.sub(r"\s+", "", link) if "\n" in link else link)
            candidates = by_canon.get(key, set())
            if len(candidates) != 1:
                continue
            targets = token_to_rel.get(next(iter(candidates)), set())
            if len(targets) != 1:
                continue
            target = next(iter(targets))
            if target != source:
                index[target].add(source)
    return index


def link_resolver(records: dict[str, dict[str, Any]]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """(by_canon, token_to_rel) for resolving a [[wikilink]] to at most one note rel."""
    stems = {record["stem"] for record in records.values()}
    names = {record["name"] for record in records.values() if record["name"]}
    by_canon = project_by_canon(stems, names)
    token_to_rel: dict[str, set[str]] = {}
    for rel, record in records.items():
        token_to_rel.setdefault(record["stem"], set()).add(rel)
        if record["name"]:
            token_to_rel.setdefault(record["name"], set()).add(rel)
    return by_canon, token_to_rel


def resolve_link(link: str, by_canon: dict[str, set[str]], token_to_rel: dict[str, set[str]]) -> str | None:
    """The single note rel a [[wikilink]] resolves to, or None when ambiguous or unknown."""
    key = canon(re.sub(r"\s+", "", link) if "\n" in link else link)
    candidates = by_canon.get(key, set())
    if len(candidates) != 1:
        return None
    targets = token_to_rel.get(next(iter(candidates)), set())
    return next(iter(targets)) if len(targets) == 1 else None


def rewrite_links_to_deleted(
    text: str,
    by_canon: dict[str, set[str]],
    token_to_rel: dict[str, set[str]],
    delete_to_stem: dict[str, str],
) -> str:
    """Rewrite every [[wikilink]] that resolves to a deleted note to its survivor
    stem (inbound-wikilink retargeting)."""
    head, body = split_frontmatter_raw(text)
    new_body = body
    for match in WIKILINK_RE.finditer(body):
        link = match.group(1)
        target = resolve_link(link, by_canon, token_to_rel)
        if target is not None and target in delete_to_stem:
            new_body = new_body.replace(f"[[{link}]]", f"[[{delete_to_stem[target]}]]")
    return head + new_body


def score_note(
    body_bytes: int, age_days: int, inbound: int, supersessions: int, desc_problems: tuple[str, ...] = ()
) -> tuple[int, list[str]]:
    """Additive priority score plus per-signal reasons; ranks the flagged set (the
    per-pass fan-out cap in the assembler picks the top)."""
    score = 0
    reasons: list[str] = []
    if supersessions >= 1:
        score += 3 * supersessions
        reasons.append(f"supersession:{supersessions}")
    if body_bytes > config.TRIAGE_BODY_BYTES_LARGE:
        score += 5
        reasons.append(f"size:{body_bytes}")
    elif body_bytes > config.TRIAGE_BODY_BYTES:
        score += 3
        reasons.append(f"size:{body_bytes}")
    if age_days > config.TRIAGE_AGE_DAYS_OLD:
        score += 2
        reasons.append(f"age:{age_days}")
    elif age_days > config.TRIAGE_AGE_DAYS:
        score += 1
        reasons.append(f"age:{age_days}")
    if inbound == 0:
        score += 1
        reasons.append("orphan:0-inbound")
    score += 2 * len(desc_problems)
    reasons.extend(desc_problems)
    return score, reasons


def is_flagged(body_bytes: int, supersessions: int, desc_problems: tuple[str, ...] = ()) -> bool:
    """Structural-rot gate: supersession stacks, oversized bodies, and unroutable
    descriptions flag (recall is description-routed, so a bad one hides its note)."""
    return (
        supersessions >= TRIAGE_SUPERSESSION_FLAG
        or body_bytes > config.TRIAGE_BODY_BYTES
        or bool(desc_problems)
    )


def description_problems(description: str | None, duplicated: set[str]) -> tuple[str, ...]:
    """Deterministic description-quality signals for one note.

    desc_short: too few words to route agentic recall (or missing entirely).
    desc_dup: another note in the same project carries the identical description,
    so neither can be told apart at recall time.
    """
    problems: list[str] = []
    if description is None or len(description.split()) < config.TRIAGE_DESC_MIN_WORDS:
        problems.append("desc_short")
    elif description.casefold() in duplicated:
        problems.append("desc_dup")
    return tuple(problems)


def duplicated_descriptions(records: dict[str, dict[str, Any]]) -> set[str]:
    """Case-folded descriptions carried verbatim by two or more notes in a project."""
    seen: dict[str, int] = {}
    for record in records.values():
        description = record.get("description")
        if isinstance(description, str):
            key = description.casefold()
            seen[key] = seen.get(key, 0) + 1
    return {key for key, count in seen.items() if count >= 2}


def effective_index_size(index_text: str) -> tuple[int, int]:
    """(lines, bytes) of MEMORY.md as Claude Code measures its load cap: YAML
    frontmatter and block-level HTML comments are stripped before counting
    (measured against Claude Code v2.1.211's accounting). config.INDEX_LOAD_MAX_LINES
    / config.INDEX_LOAD_MAX_BYTES is what actually loads; the rest is silently
    invisible to every session. `triage` warns at config.INDEX_BUDGET_FRACTION of
    the cap, matching Claude Code's own over-limit rewrite trigger."""
    _fm, body = split_frontmatter_raw(index_text)
    body = re.sub(r"(?s)<!--.*?-->", "", body)
    return len(body.splitlines()), len(body.encode("utf-8"))


def loaded_index_text(index_text: str) -> str:
    """The index content a session ACTUALLY loads: frontmatter and block HTML
    comments stripped, then hard-truncated to the first config.INDEX_LOAD_MAX_LINES
    lines and config.INDEX_LOAD_MAX_BYTES bytes, whichever cuts first. Anything past
    this is invisible to real recall, so eval judges must never see it either."""
    _fm, body = split_frontmatter_raw(index_text)
    body = re.sub(r"(?s)<!--.*?-->", "", body)
    truncated = "".join(body.splitlines(keepends=True)[: config.INDEX_LOAD_MAX_LINES])
    return truncated.encode("utf-8")[: config.INDEX_LOAD_MAX_BYTES].decode("utf-8", errors="ignore")


def triage_project(project: str, memory_dir: Path, now: dt.date) -> tuple[int, list[dict[str, Any]]]:
    records = scan_project_notes(memory_dir)
    index = inbound_index(records)
    duplicated = duplicated_descriptions(records)
    flagged: list[dict[str, Any]] = []
    for rel, record in sorted(records.items()):
        supersessions = len(SUPERSESSION_RE.findall(record["body"]))
        age_days = max(0, (now - dt.date.fromtimestamp(record["mtime"])).days)
        inbound = len(index[rel])
        desc_problems = description_problems(record.get("description"), duplicated)
        score, reasons = score_note(record["body_bytes"], age_days, inbound, supersessions, desc_problems)
        # Decay flag: mined notes carrying confidence/last_validated frontmatter
        # lose effective confidence on a half-life (config.DECAY_HALF_LIFE_DAYS).
        # Once effective confidence drops below config.DECAY_FLAG_THRESHOLD the
        # note is a dream-pass candidate (revalidate or archive), surfaced through
        # the same flagged channel the pass reads.
        decayed = False
        nested = record.get("nested_meta") or {}
        decay = decay_effective(nested, now)
        if decay is not None:
            effective, _age_days = decay
            if effective < config.DECAY_FLAG_THRESHOLD:
                decayed = True
                reasons = list(reasons) + [f"decayed_confidence (effective {effective:.2f})"]
        if decayed or is_flagged(record["body_bytes"], supersessions, desc_problems):
            flagged.append(
                {
                    "project": project,
                    "path": rel,
                    "score": score,
                    "reasons": reasons,
                    "body_bytes": record["body_bytes"],
                    "age_days": age_days,
                    "age_source": "mtime",
                    "inbound_links": inbound,
                    "supersessions": supersessions,
                }
            )
    return len(records), flagged


def recently_applied_paths(days: int, now: dt.date) -> set[tuple[str, str]]:
    """(project, path) pairs an APPLIED dream pass touched within the window.

    Reads applied patch-set manifests so both triage and the planner can skip
    consolidation-refire flags (size and supersession-language heuristics fire
    on notes already in their durable form)."""
    recently_applied: set[tuple[str, str]] = set()
    pass_root = config.pass_root()
    cutoff = dt.date.fromordinal(now.toordinal() - days)
    if not pass_root.is_dir():
        return recently_applied
    for patch_dir in pass_root.iterdir():
        apply_manifest = patch_dir / "apply-manifest.json"
        manifest = patch_dir / "manifest.json"
        if not (apply_manifest.is_file() and manifest.is_file()):
            continue
        if dt.date.fromtimestamp(apply_manifest.stat().st_mtime) < cutoff:
            continue
        try:
            proposals = json.loads(manifest.read_text())["proposals"]
        except (json.JSONDecodeError, KeyError, OSError):
            continue
        for proposal in proposals:
            project = proposal.get("project", "")
            for result_file in proposal.get("results") or []:
                if isinstance(result_file, dict) and result_file.get("path"):
                    recently_applied.add((project, result_file["path"]))
            # Early-wave manifests store survivor as a bare path string.
            survivor = proposal.get("survivor")
            if isinstance(survivor, str) and survivor:
                recently_applied.add((project, survivor))
            elif isinstance(survivor, dict) and survivor.get("path"):
                recently_applied.add((project, survivor["path"]))
    return recently_applied


def run_triage(args: argparse.Namespace) -> int:
    live_root = Path(args.live_root).expanduser()
    mirror_root = Path(args.mirror_root).expanduser() if args.mirror_root else None
    if not live_root.is_dir():
        print(f"memory-triage: live root is not a directory: {live_root}", file=sys.stderr)
        return 2
    now = args.now or dt.date.today()
    live = project_dirs(live_root, live=True)
    all_flagged: list[dict[str, Any]] = []
    notes_scored = 0
    by_project: dict[str, int] = {}
    index_over_budget: dict[str, dict[str, int]] = {}
    for project in sorted(live):
        memory_dir = live[project]
        if memory_dir.is_symlink() or memory_dir.parent.is_symlink():
            # Symlinked roots are an audit finding; triage never traverses them.
            continue
        count, flagged = triage_project(project, memory_dir, now)
        notes_scored += count
        if flagged:
            by_project[project] = len(flagged)
            all_flagged.extend(flagged)
        index_path = memory_dir / "MEMORY.md"
        if index_path.is_file() and not index_path.is_symlink():
            lines, size = effective_index_size(index_path.read_text(encoding="utf-8", errors="replace"))
            if (
                lines >= config.INDEX_LOAD_MAX_LINES * config.INDEX_BUDGET_FRACTION
                or size >= config.INDEX_LOAD_MAX_BYTES * config.INDEX_BUDGET_FRACTION
            ):
                index_over_budget[project] = {"lines": lines, "bytes": size}
    # Consolidation-refire suppression: notes an applied dream pass touched within
    # --suppress-applied-days are already in their durable form; size and
    # supersession-language heuristics refire on correct content (the authors'
    # consolidation campaign measured already-consolidated notes reflagged the
    # same day they were applied). Suppressed entries are reported, not silently
    # dropped.
    suppressed: list[dict[str, Any]] = []
    days = getattr(args, "suppress_applied_days", config.SUPPRESS_APPLIED_DAYS)
    if days > 0:
        recently_applied = recently_applied_paths(days, now)
        if recently_applied:
            kept = []
            for record in all_flagged:
                if (record["project"], record["path"]) in recently_applied:
                    suppressed.append(record)
                else:
                    kept.append(record)
            all_flagged = kept
            for record in suppressed:
                by_project[record["project"]] = by_project.get(record["project"], 1) - 1
                if by_project.get(record["project"], 0) <= 0:
                    by_project.pop(record["project"], None)
    all_flagged.sort(key=lambda record: (-record["score"], record["project"], record["path"]))
    result = {
        "schema_version": 1,
        "command": "triage",
        "roots": {"live": str(live_root), "mirror": str(mirror_root) if mirror_root is not None else None},
        "now": now.isoformat(),
        "summary": {
            "live_projects": len(live),
            "notes_scored": notes_scored,
            "flagged": len(all_flagged),
            "suppressed_recently_applied": len(suppressed),
            "by_project": dict(sorted(by_project.items())),
            "index_over_budget": dict(sorted(index_over_budget.items())),
        },
        "flagged": all_flagged,
        "suppressed": suppressed,
    }
    if args.format == "json":
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    else:
        print(render_triage_human(result))
    return 0


def render_triage_human(result: dict[str, Any]) -> str:
    # Project-level only, no note filenames or bodies, so this output is safe to
    # fold verbatim into a broader status report. The trailing flagged:N line is
    # a stable machine-readable summary line; external tooling may grep for it,
    # so its exact shape is part of this command's documented output contract.
    summary = result["summary"]
    lines = [
        "memory-triage: "
        f"{summary['live_projects']} project(s), {summary['notes_scored']} note(s) scored, "
        f"{summary['flagged']} flagged"
    ]
    for project, count in summary["by_project"].items():
        top = max((record["score"] for record in result["flagged"] if record["project"] == project), default=0)
        lines.append(f"  {project}: {count} flagged (max score {top})")
    # Claude Code hard-truncates MEMORY.md at the session load cap
    # (config.INDEX_LOAD_MAX_LINES lines / config.INDEX_LOAD_MAX_BYTES bytes);
    # content past the cap is invisible to every session. WARN prefix is
    # intentional: downstream status/notification tooling can watch for it.
    for project, size in summary.get("index_over_budget", {}).items():
        lines.append(
            f"WARN {project}: MEMORY.md at {size['lines']}/{config.INDEX_LOAD_MAX_LINES} lines, "
            f"{size['bytes']}/{config.INDEX_LOAD_MAX_BYTES} bytes "
            f"(>= {config.INDEX_BUDGET_FRACTION:.0%} of the session load cap)"
        )
    lines.append(f"flagged:{summary['flagged']}")
    return "\n".join(lines)


def mirror_freshness(live_root: Path, mirror_root: Path, projects: list[str]) -> dict[str, list[str]]:
    """Per-project live->mirror freshness gate shared by fix --apply and the
    apply command.

    Returns {project: [finding_kind, ...]} for any project whose live memory is
    not fully reflected in the mirror. mirror_only paths do not block: the
    mirror deliberately preserves history the live tree no longer has.
    """
    live = project_dirs(live_root, live=True)
    mirror = project_dirs(mirror_root, live=False)
    result: dict[str, list[str]] = {}
    for project in projects:
        issues: set[str] = set()
        if project not in mirror:
            issues.add("mirror_missing_project")
        elif project in live:
            live_files = files(live[project])
            mirror_files = files(mirror[project])
            for rel in sorted(live_files):
                if rel not in mirror_files:
                    issues.add("mirror_missing_file")
                elif digest(live_files[rel]) != digest(mirror_files[rel]):
                    issues.add("mirror_stale_file")
        if issues:
            result[project] = sorted(issues)
    return result


def confined_path(memory_dir: Path, rel: str) -> Path | None:
    """Resolve rel to a .md file strictly inside memory_dir, or None on any
    escape (destination confinement).

    Rejects empty, absolute, traversal, non-.md, MEMORY.md (single-writer), NUL
    bytes, and any symlink-escaping destination. The single confinement
    implementation shared by the assembler and the apply command, so the
    security check can never drift.
    """
    if not rel or os.path.isabs(rel):
        return None
    candidate = Path(rel)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    if candidate.name == "MEMORY.md" or not candidate.name.endswith(".md"):
        return None
    dest = memory_dir / candidate
    try:
        base = memory_dir.resolve()
        resolved = dest.resolve()
    except (OSError, ValueError):
        return None
    if resolved == base or base not in resolved.parents:
        return None
    return dest


def atomic_write(path: Path, data: bytes) -> None:
    """Write via a temp sibling then rename, so an interrupted write never truncates."""
    temp = path.with_name(path.name + ".dream-tmp")
    temp.write_bytes(data)
    os.replace(temp, path)


def _fm_quote_if_needed(value: str) -> str:
    """Quote a frontmatter scalar when a bare value would be mis-parsed (e.g. contains
    '#', a colon, or leading/trailing whitespace); matches the memory '#'-quoting rule."""
    if value != value.strip() or any(ch in value for ch in "#:") or value[:1] in "!&*[]{}>|%@`\"'":
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def _set_fm_field(lines: list[str], key: str, indent: str, value: str) -> bool:
    """Replace the value of `<indent>key:` in a frontmatter line list, in place.
    Returns False when no such line exists (nothing replaced)."""
    pattern = re.compile(rf"^{re.escape(indent)}{re.escape(key)}\s*:")
    for index, line in enumerate(lines):
        if pattern.match(line):
            newline = "\n" if line.endswith("\n") else ""
            lines[index] = f"{indent}{key}: {value}{newline}"
            return True
    return False


def _set_or_insert_nested(lines: list[str], key: str, value: str) -> None:
    """Set `  key: value` inside the frontmatter's nested `metadata:` block, inserting
    after the block's last field when absent. No-op when there is no nested block
    (legacy flat-schema notes, common enough in a real corpus to require handling):
    never invent structure."""
    if _set_fm_field(lines, key, "  ", value):
        return
    for index, line in enumerate(lines):
        if re.match(r"^metadata\s*:", line):
            end = index + 1
            while end < len(lines) and re.match(r"^\s{2,}\S", lines[end]):
                end += 1
            lines.insert(end, f"  {key}: {value}\n")
            return


_FLAT_SCHEMA_FIELDS = ("node_type", "type", "originSessionId")


def _normalize_flat_schema(lines: list[str]) -> list[str]:
    """Lift a legacy flat-schema frontmatter's harness fields into a nested
    metadata block, preserving their values verbatim. Lift-only: fields absent
    from the donor are never invented, unknown top-level fields stay top-level,
    and frontmatter that already has a metadata: block is returned unchanged."""
    if any(re.match(r"^metadata\s*:", line) for line in lines):
        return lines
    lifted: dict[str, str] = {}
    kept: list[str] = []
    for line in lines:
        match = re.match(r"^(node_type|type|originSessionId)\s*:\s*(.*?)\s*$", line)
        if match:
            lifted[match.group(1)] = match.group(2)
        else:
            kept.append(line)
    if not lifted:
        return lines
    block = ["metadata:\n"] + [
        f"  {key}: {lifted[key]}\n" for key in _FLAT_SCHEMA_FIELDS if key in lifted
    ]
    # The raw frontmatter includes its --- delimiter lines; the block must land
    # inside them, so insert before the closing delimiter (the last --- line).
    closing = max(
        (index for index, line in enumerate(kept) if line.rstrip("\n") == "---"),
        default=None,
    )
    if closing is None or closing == 0:
        return lines
    return kept[:closing] + block + kept[closing:]


def preserve_metadata(
    drafter_content: str, donor_content: str, extra_nested: dict[str, str] | None = None
) -> str:
    """Overlay the drafter's name/description/type onto the DONOR note's frontmatter.

    A consolidation must not silently strip schema fields the harness maintains
    (node_type, originSessionId, any other metadata). The drafter reconstructs a
    minimal frontmatter; this keeps the donor's full frontmatter block and only
    updates name, description, and metadata.type from the drafter's output, then
    appends the drafter's body. extra_nested fields (e.g. honest multi-source
    originSessionIds for a merge) are set inside the nested metadata block.
    A legacy flat-schema donor (top-level type/node_type/originSessionId, no
    metadata: block — common enough in a real corpus to require handling, not
    just a theoretical shape) is normalized first: its harness fields are
    lifted, values unchanged, into a nested metadata block, so consolidation
    heals the legacy shape instead of propagating it and extra_nested fields are
    not silently dropped. Returns the drafter content unchanged when either side
    lacks parseable frontmatter (nothing safe to merge).
    """
    donor_fm, _donor_body = split_frontmatter_raw(donor_content)
    drafter_fm, drafter_body = split_frontmatter_raw(drafter_content)
    if not donor_fm or not drafter_fm:
        return drafter_content
    metadata, error, _body, _raw = parse_frontmatter(drafter_content)
    if error or not isinstance(metadata, dict):
        return drafter_content
    lines = _normalize_flat_schema(donor_fm.splitlines(keepends=True))
    if isinstance(metadata.get("name"), str) and metadata["name"].strip():
        _set_fm_field(lines, "name", "", metadata["name"].strip())
    if isinstance(metadata.get("description"), str) and metadata["description"].strip():
        _set_fm_field(lines, "description", "", _fm_quote_if_needed(metadata["description"].strip()))
    nested = metadata.get("metadata")
    if isinstance(nested, dict) and isinstance(nested.get("type"), str) and nested["type"].strip():
        _set_fm_field(lines, "type", "  ", nested["type"].strip())
    for key, value in (extra_nested or {}).items():
        _set_or_insert_nested(lines, key, value)
    return "".join(lines) + drafter_body


def content_tokens(value: str) -> set[str]:
    """Content-word token set for description-similarity checks (recall is routed
    by these one-liners, so two near-identical descriptions cannot be told apart).

    Unicode-aware: `\\w+` matches letters in any script, not just ASCII a-z0-9,
    so a non-English corpus tokenizes sensibly. The Jaccard thresholds that
    consume this (config.MERGE_JACCARD, config.SIBLING_DESC_JACCARD) were
    calibrated on an English corpus (see docs/TUNING.md)."""
    return set(re.findall(r"\w+", value.lower()))


def description_similarity(first: str, second: str) -> float:
    """Jaccard overlap of two descriptions' content words; 0.0 when either is empty."""
    a, b = content_tokens(first), content_tokens(second)
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def origin_session_id(content: str) -> str | None:
    """The note's originSessionId, tolerating both frontmatter shapes (nested
    metadata block or legacy flat top-level); None when absent or unparseable."""
    metadata, error, _body, _raw = parse_frontmatter(content)
    if error or not isinstance(metadata, dict):
        return None
    nested = metadata.get("metadata")
    value = nested.get("originSessionId") if isinstance(nested, dict) else None
    value = value or metadata.get("originSessionId")
    return value.strip() if isinstance(value, str) and value.strip() else None


def redescribe_content(donor_content: str, description: str) -> str | None:
    """The donor note with ONLY its frontmatter description replaced; body, name,
    and every other frontmatter byte are preserved by construction. None when the
    donor has no parseable description line to replace.

    The description arrives RAW from drafter JSON (unlike every other overlay
    value, which passes through the line-based frontmatter parser), so collapse
    all whitespace runs: an embedded newline would otherwise inject arbitrary
    frontmatter lines."""
    donor_fm, donor_body = split_frontmatter_raw(donor_content)
    if not donor_fm:
        return None
    lines = donor_fm.splitlines(keepends=True)
    flattened = " ".join(description.split())
    if not flattened or not _set_fm_field(lines, "description", "", _fm_quote_if_needed(flattened)):
        return None
    return "".join(lines) + donor_body


def is_valid_utf8(raw: bytes) -> bool:
    try:
        raw.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def content_id(proposals: list[dict[str, Any]]) -> str:
    """Content-bound patch-set id: the digest apply recomputes and the operator's token
    must match, so editing any proposal byte after approval invalidates the approval."""
    return hashlib.sha256(json.dumps(proposals, sort_keys=True).encode("utf-8")).hexdigest()[:16]


# --- Dream-pass fix mode (mechanical repairs) --------------------------------


def split_frontmatter_raw(text: str) -> tuple[str, str]:
    """Return (frontmatter-with-fences, body) preserving bytes exactly; ("", text) when none."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return "", text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "".join(lines[: index + 1]), "".join(lines[index + 1 :])
    return "", text


def entry_fields(note_path: Path, rel: str) -> tuple[str, str]:
    """Link text and hook for a MEMORY.md entry, sourced from the note's frontmatter."""
    fallback = Path(rel).name[:-3] if rel.endswith(".md") else rel
    try:
        text = note_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return fallback, ""
    metadata, _error, _body, _raw = parse_frontmatter(text)
    name = None
    description = None
    if isinstance(metadata, dict):
        candidate_name = metadata.get("name")
        candidate_desc = metadata.get("description")
        if isinstance(candidate_name, str) and candidate_name.strip():
            name = candidate_name.strip()
        if isinstance(candidate_desc, str) and candidate_desc.strip():
            description = candidate_desc.strip()
    return name or fallback, description or ""


def compute_index_reconcile(
    project: str,
    memory_dir: Path,
    records: dict[str, dict[str, Any]],
    restrict_appends_to: set[str] | None = None,
) -> dict[str, Any] | None:
    """Index reconciliation plan: drop all-dead lines, append unindexed notes.

    restrict_appends_to bounds the append set (the dream pass appends ONLY the
    notes it just created): corpus-wide index healing, appending pre-existing
    unindexed notes, is a routing-surface change of its own (measured, in the
    authors' pre-release evaluation, at up to a +35% index-token increase) and
    stays a separately gated step (`fix` mode passes None and heals everything)."""
    index_path = memory_dir / "MEMORY.md"
    if not index_path.is_file() or index_path.is_symlink():
        return None  # a missing index is an audit finding; fix does not create indexes in v1
    text = index_path.read_text(encoding="utf-8", errors="replace")
    targets, _escaping = index_targets(text)
    live_note_rels = set(records)
    gone = {target for target in targets if target != "MEMORY.md" and target not in live_note_rels}
    # A gone target is only "dropped" when its entire index line is dead, matching
    # what apply removes. A mixed line (a dead and a surviving link) is left intact
    # and its dead target is NOT claimed as dropped, so the record never lies; the
    # residual stale ref stays visible as the auditor's missing_index_target.
    droppable: set[str] = set()
    fragment_droppable: set[str] = set()
    for line in text.splitlines():
        line_targets, _escaping = index_targets(line)
        dead_here = line_targets & gone
        if not dead_here:
            continue
        if line_targets.issubset(gone):
            droppable |= dead_here
        else:
            # A dead ref on a mixed (hand-packed) line: the LINE is preserved
            # (hand-edited content for live notes is never rewritten) but the
            # dead fragment itself is removable; a link to a nonexistent file
            # serves nobody (this shape occurs in real corpora: a stale
            # cross-reference left behind after a note's close-out).
            fragment_droppable |= dead_here
    dropped = sorted(droppable)
    fragment_dropped = sorted(fragment_droppable)
    unindexed = sorted(
        rel for rel in live_note_rels
        if rel not in targets and (restrict_appends_to is None or rel in restrict_appends_to)
    )
    if not dropped and not unindexed and not fragment_dropped:
        return None
    return {
        "kind": "index_reconcile", "project": project, "path": "MEMORY.md",
        "dropped": dropped, "appended": unindexed, "fragment_dropped": fragment_dropped,
    }


def compute_fix(live_root: Path, mirror_root: Path | None) -> list[dict[str, Any]]:
    live = project_dirs(live_root, live=True)
    repairs: list[dict[str, Any]] = []
    for project in sorted(live):
        memory_dir = live[project]
        if memory_dir.is_symlink() or memory_dir.parent.is_symlink():
            continue
        records = scan_project_notes(memory_dir)
        stems = {record["stem"] for record in records.values()}
        names = {record["name"] for record in records.values() if record["name"]}
        by_canon = project_by_canon(stems, names)
        for rel, record in sorted(records.items()):
            seen: set[tuple[str, str]] = set()
            for match in WIKILINK_RE.finditer(record["body"]):
                link = match.group(1)
                if link in stems or link in names:
                    continue
                key = canon(re.sub(r"\s+", "", link) if "\n" in link else link)
                candidates = by_canon.get(key, set())
                if len(candidates) != 1:
                    continue
                target = next(iter(candidates))
                if target == link or (rel, link) in seen:
                    continue
                seen.add((rel, link))
                repairs.append(
                    {"kind": "wikilink_rewrite", "project": project, "path": rel, "link": link, "resolves_to": target}
                )
        reconcile = compute_index_reconcile(project, memory_dir, records)
        if reconcile:
            repairs.append(reconcile)
    return repairs


def rewrite_wikilinks(path: Path, items: list[dict[str, Any]]) -> bool:
    """Rewrite [[wikilinks]] atomically, preserving mtime. Returns False (untouched) when
    the file is not valid UTF-8, so a read-modify-write never lossily corrupts stray bytes."""
    stat = path.stat()
    raw = path.read_bytes()
    if not is_valid_utf8(raw):
        return False
    head, body = split_frontmatter_raw(raw.decode("utf-8"))
    for item in items:
        body = body.replace(f"[[{item['link']}]]", f"[[{item['resolves_to']}]]")
    atomic_write(path, (head + body).encode("utf-8"))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    return True


def strip_dead_index_fragments(line: str, dead: set[str]) -> str:
    """Remove '; '-separated fragments of a packed index line whose only .md
    targets are all dead. Live fragments are preserved byte-for-byte (hand-edited
    content for live notes is never rewritten); a fragment mixing a dead and a
    live target is conservatively left intact. Returns the line unchanged when
    nothing is safely removable."""
    newline = "\n" if line.endswith("\n") else ""
    body = line.rstrip("\n")
    fragments = body.split("; ")
    kept_fragments: list[str] = []
    for fragment in fragments:
        frag_targets, _escaping = index_targets(fragment)
        if frag_targets and frag_targets.issubset(dead):
            continue
        kept_fragments.append(fragment)
    if len(kept_fragments) == len(fragments):
        return line
    rebuilt = "; ".join(kept_fragments).rstrip("; ").rstrip()
    return rebuilt + newline


def render_index_reconcile(
    index_text: str, memory_dir: Path, reconcile: dict[str, Any], content_by_rel: dict[str, str] | None = None
) -> str:
    """The reconciled MEMORY.md text: drop all-dead lines, append unindexed notes.

    Pure (no I/O beyond reading a note's frontmatter for its entry). content_by_rel
    lets the assembler preview an appended entry for a not-yet-written survivor using
    its drafted frontmatter, so the preview matches what apply will produce."""
    content_by_rel = content_by_rel or {}
    gone = set(reconcile["dropped"])
    fragment_gone = set(reconcile.get("fragment_dropped", []))
    kept: list[str] = []
    for line in index_text.splitlines(keepends=True):
        line_targets, _escaping = index_targets(line)
        if line_targets and line_targets.issubset(gone | fragment_gone):
            continue  # every .md target on this line is gone; drop it
        dead_here = line_targets & fragment_gone
        if dead_here:
            line = strip_dead_index_fragments(line, dead_here)
        kept.append(line)
    result = "".join(kept)
    appended: list[str] = []
    for rel in reconcile["appended"]:
        if rel in content_by_rel:
            name, description = frontmatter_entry(content_by_rel[rel], rel)
        else:
            name, description = entry_fields(memory_dir / rel, rel)
        # ": " separator, not an em dash; a '#' inside description is literal
        # markdown text here, not a YAML comment.
        appended.append(index_entry_line(name, description, rel) + "\n")
    if appended:
        if result and not result.endswith("\n"):
            result += "\n"
        result += "".join(appended)
    return result


def index_entry_line(name: str, description: str, rel: str) -> str:
    """The canonical MEMORY.md entry for a note (shared by append, refresh, and the
    hand-edit detection that guards refresh)."""
    return f"- [{name}]({rel}): {description}" if description else f"- [{name}]({rel})"


def refresh_index_lines(index_text: str, refresh: dict[str, dict[str, Any]]) -> str:
    """Rewrite the MEMORY.md entry line of each refreshed note to match its current
    frontmatter. refresh: rel -> {"old": (name, description) | None,
    "new": (name, description), "force": bool}.

    Recall is routed by these one-line hooks, so a note the pass just rewrote must
    not keep advertising its pre-consolidation description (the corpus audit found
    index lines and frontmatter descriptions diverging independently). Guardrails:
    only a line whose sole .md target is the refreshed note is considered, and it is
    replaced only when it exactly matches the canonical rendering of the note's
    PRE-rewrite frontmatter; any deviation means a hand-edited line, which is
    preserved. "force" (redescribe: updating the routing hook is the action's
    operator-approved purpose, and the index diff is in the preview) replaces the
    sole-target line regardless."""
    if not refresh:
        return index_text
    result: list[str] = []
    for line in index_text.splitlines(keepends=True):
        line_targets, _escaping = index_targets(line)
        if len(line_targets) == 1 and (rel := next(iter(line_targets))) in refresh:
            entry = refresh[rel]
            old = entry.get("old")
            matches_old = old is not None and line.rstrip() == index_entry_line(old[0], old[1], rel)
            if entry.get("force") or matches_old:
                name, description = entry["new"]
                newline = "\n" if line.endswith("\n") else ""
                result.append(f"{index_entry_line(name, description, rel)}{newline}")
                continue
        result.append(line)
    return "".join(result)


def frontmatter_entry(content: str, rel: str) -> tuple[str, str]:
    """Link text and hook from in-memory note content (for previewing a fresh survivor)."""
    fallback = Path(rel).name[:-3] if rel.endswith(".md") else rel
    metadata, _error, _body, _raw = parse_frontmatter(content)
    name = description = None
    if isinstance(metadata, dict):
        candidate_name, candidate_desc = metadata.get("name"), metadata.get("description")
        if isinstance(candidate_name, str) and candidate_name.strip():
            name = candidate_name.strip()
        if isinstance(candidate_desc, str) and candidate_desc.strip():
            description = candidate_desc.strip()
    return name or fallback, description or ""


def apply_index_reconcile(index_path: Path, memory_dir: Path, reconcile: dict[str, Any]) -> None:
    stat = index_path.stat()
    raw_index = index_path.read_bytes()
    index_text = raw_index.decode("utf-8") if is_valid_utf8(raw_index) else raw_index.decode("utf-8", errors="replace")
    result = render_index_reconcile(index_text, memory_dir, reconcile)
    atomic_write(index_path, result.encode("utf-8"))
    os.utime(index_path, ns=(stat.st_atime_ns, stat.st_mtime_ns))


def apply_index_refresh(index_path: Path, refresh: dict[str, dict[str, Any]]) -> None:
    """Refresh index entry lines to match current note frontmatter, mtime-preserving
    (maintenance must never reset the age signal used for triage ranking)."""
    if not refresh or not index_path.is_file() or index_path.is_symlink():
        return
    stat = index_path.stat()
    raw_index = index_path.read_bytes()
    index_text = raw_index.decode("utf-8") if is_valid_utf8(raw_index) else raw_index.decode("utf-8", errors="replace")
    result = refresh_index_lines(index_text, refresh)
    if result != index_text:
        atomic_write(index_path, result.encode("utf-8"))
        os.utime(index_path, ns=(stat.st_atime_ns, stat.st_mtime_ns))


def snapshot_pre_fix(live_root: Path, repairs: list[dict[str, Any]]) -> Path | None:
    """Copy every file `fix --apply` is about to touch into a timestamped snapshot
    directory before any write; used when no mirror is configured (mirror mode's
    freshness gate is the equivalent safety net there). Returns the snapshot
    directory, or None when there was nothing to snapshot.

    Uses the module's own atomic-write helper for the copy, so a crash mid-
    snapshot can never leave a half-written backup. The live files' own mtimes
    are restored by the rewrite helpers exactly as before this snapshot step.
    """
    live = project_dirs(live_root, live=True)
    touched: set[tuple[str, str]] = set()
    for repair in repairs:
        rel = repair["path"] if repair["kind"] == "wikilink_rewrite" else "MEMORY.md"
        touched.add((repair["project"], rel))
    if not touched:
        return None
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot_root = config.pass_root() / "fix-backups" / ts
    for project, rel in sorted(touched):
        memory_dir = live.get(project)
        if memory_dir is None:
            continue
        source = memory_dir / rel
        if not source.is_file() or source.is_symlink():
            continue
        destination = snapshot_root / project / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(destination, source.read_bytes())
    return snapshot_root


def apply_fix(live_root: Path, mirror_root: Path | None, repairs: list[dict[str, Any]]) -> int:
    affected = sorted({repair["project"] for repair in repairs})
    if mirror_root is not None:
        stale = mirror_freshness(live_root, mirror_root, affected)
        if stale:
            for project, kinds in sorted(stale.items()):
                print(
                    f"memory-fix: refusing to apply to {project}: mirror not fresh "
                    f"({', '.join(kinds)}); {config.MIRROR_PUSH_HINT}",
                    file=sys.stderr,
                )
            return 1
    else:
        snapshot_pre_fix(live_root, repairs)
    live = project_dirs(live_root, live=True)
    rewrites: dict[tuple[str, str], list[dict[str, Any]]] = {}
    reconciles: list[dict[str, Any]] = []
    for repair in repairs:
        if repair["kind"] == "wikilink_rewrite":
            rewrites.setdefault((repair["project"], repair["path"]), []).append(repair)
        elif repair["kind"] == "index_reconcile":
            reconciles.append(repair)
    for (project, rel), items in sorted(rewrites.items()):
        rewrite_wikilinks(live[project] / rel, items)
    for reconcile in sorted(reconciles, key=lambda item: item["project"]):
        memory_dir = live[reconcile["project"]]
        apply_index_reconcile(memory_dir / "MEMORY.md", memory_dir, reconcile)
    return 0


def run_fix(args: argparse.Namespace) -> int:
    live_root = Path(args.live_root).expanduser()
    mirror_root = Path(args.mirror_root).expanduser() if args.mirror_root else None
    if not live_root.is_dir():
        print(f"memory-fix: live root is not a directory: {live_root}", file=sys.stderr)
        return 2
    repairs = compute_fix(live_root, mirror_root)
    exit_code = 0
    if args.apply and repairs:
        exit_code = apply_fix(live_root, mirror_root, repairs)
    result = {
        "schema_version": 1,
        "command": "fix",
        "applied": bool(args.apply and repairs and exit_code == 0),
        "roots": {"live": str(live_root), "mirror": str(mirror_root) if mirror_root is not None else None},
        "repairs": repairs,
    }
    if args.format == "json":
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    else:
        print(render_fix_human(result))
    return exit_code


def render_fix_human(result: dict[str, Any]) -> str:
    state = "applied" if result["applied"] else "proposed (dry-run)"
    lines = [f"memory-fix: {len(result['repairs'])} repair(s) {state}"]
    for repair in result["repairs"]:
        if repair["kind"] == "wikilink_rewrite":
            lines.append(
                f"  wikilink {repair['project']}/{repair['path']}: "
                f"[[{repair['link']}]] -> [[{repair['resolves_to']}]]"
            )
        elif repair["kind"] == "index_reconcile":
            lines.append(
                f"  index {repair['project']}/MEMORY.md: "
                f"+{len(repair['appended'])} appended, -{len(repair['dropped'])} dropped"
            )
    return "\n".join(lines)


def date_value(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from error


# --- CLI registration ---------------------------------------------------------


def _add_format_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=("human", "json"), default="human")


def add_parsers(subparsers) -> None:
    """Register `audit`, `triage`, and `fix` on the shared top-level subparsers
    action (see memory_dream.cli.build_parser)."""
    _add_audit_parser(subparsers)
    _add_triage_parser(subparsers)
    _add_fix_parser(subparsers)


def _add_audit_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "audit", help="scan live memory (and an optional mirror) for structural problems"
    )
    config.add_root_args(parser)
    _add_format_arg(parser)
    parser.add_argument("--as-of", type=date_value, help="inject YYYY-MM-DD for deterministic stale-date review")
    parser.add_argument("--stale-days", type=int, default=config.AUDIT_STALE_DAYS)
    parser.add_argument("--max-index-bytes", type=int, default=config.AUDIT_MAX_INDEX_BYTES)
    parser.add_argument("--max-index-lines", type=int, default=config.AUDIT_MAX_INDEX_LINES)
    parser.add_argument(
        "--scan-content",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="scan bodies for high-signal credential signatures (default: enabled)",
    )
    parser.add_argument(
        "--sensitive-exit",
        action="store_true",
        help="exit 1 only when a sensitive indicator is present (instead of on any blocking finding)",
    )
    parser.set_defaults(func=run_audit)


def _add_triage_parser(subparsers) -> None:
    parser = subparsers.add_parser("triage", help="score live notes for the dream pass (read-only)")
    config.add_root_args(parser)
    _add_format_arg(parser)
    parser.add_argument("--now", type=date_value, default=None, help="inject YYYY-MM-DD for deterministic age scoring")
    parser.add_argument(
        "--suppress-applied-days",
        type=int,
        default=config.SUPPRESS_APPLIED_DAYS,
        help="drop flags on notes an applied dream pass touched within N days (0 disables)",
    )
    parser.set_defaults(func=run_triage)


def _add_fix_parser(subparsers) -> None:
    parser = subparsers.add_parser("fix", help="mechanical repairs (dry-run unless --apply)")
    config.add_root_args(parser)
    _add_format_arg(parser)
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "write repairs to live memory; refuses per-project when a configured "
            "mirror is stale, or snapshots every touched file first when no "
            "mirror is configured"
        ),
    )
    parser.set_defaults(func=run_fix)


def run_audit(args: argparse.Namespace) -> int:
    try:
        result = audit(args)
    except OSError as error:
        print(f"memory-audit: unable to read audit roots: {error}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    else:
        print(render_human(result))
    if args.sensitive_exit:
        return int(
            any(
                item["kind"].startswith("sensitive_") or item["kind"] == "live_symlink"
                for item in result["findings"]
            )
        )
    return 1 if result["summary"]["blocking_findings"] else 0
