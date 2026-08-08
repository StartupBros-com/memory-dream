"""Apply an operator-approved dream-pass selection to live memory, safely and resumably.

Reads a patch-set directory and a selection JSON, runs the safety gate stack
in order, and writes only what an operator approved.

Gate order: single-flight lock -> consent verification (a real post-preview
operator-trace turn by default; an explicitly-acknowledged reduced-consent
token mode for non-Claude-Code harnesses) -> per-project mirror-freshness
(only when a mirror is configured) -> active-session warning -> snapshot
backup (always: every file about to be written or deleted is copied into the
patch set before the first live write, so `restore` can reverse this apply)
-> per-proposal destination confinement + sensitive-note refusal +
source-digest re-verification -> per-project stage-then-commit with rollback
and a single index reconciliation -> apply manifest + machine-parseable
completion line. A vanished project or a changed source skips its proposals
rather than aborting the batch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from memory_dream import audit as AUDIT
from memory_dream import compat, config, transcript

APPLY_SCHEMA_VERSION = 1
COMPLETION_PREFIX = "DREAM-APPLY-COMPLETE"
RESTORE_COMPLETE_PREFIX = "RESTORE-COMPLETE"


# --- Consent-trace verification ----------------------------------------------


def verify_operator_trace(
    transcript_path: Path, trace: dict[str, Any], created_at_line: int, approval_token: str
) -> tuple[bool, str]:
    """A real operator turn, recorded AFTER the preview and bearing the approval token, must match.

    Anti-forgery core: drafting and applying in one uninterrupted automated
    sequence produces no post-preview human turn. Beyond liveness, the turn
    must CONTAIN the patch set's approval token (the manifest id, shown in
    the preview and typed by the operator), so the trace expresses consent to
    THIS patch set, not just that some human said something afterward.
    """
    if not isinstance(trace, dict):
        return False, "selection carries no operator_trace"
    digest = trace.get("message_sha256")
    turn_index = trace.get("turn_index")
    if not isinstance(digest, str) or not isinstance(turn_index, int):
        return False, "operator_trace missing message_sha256 or turn_index"
    if turn_index < created_at_line:
        return False, "operator_trace turn precedes the preview (no post-preview approval)"
    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as error:
        return False, f"transcript unreadable: {error}"
    if turn_index < 0 or turn_index >= len(lines):
        return False, "operator_trace turn_index out of transcript range"
    try:
        entry = json.loads(lines[turn_index])
    except json.JSONDecodeError:
        return False, "operator_trace turn is not a valid transcript entry"
    try:
        text = transcript.extract_user_text(entry)
    except transcript.TranscriptSchemaError as error:
        return False, f"transcript schema error: {error} (check for a Claude Code transcript-format change)"
    if text is None:
        return False, "operator_trace turn is not an operator message"
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
        return False, "operator_trace hash does not match the transcript turn"
    if not approval_token or approval_token not in text:
        return False, "operator_trace turn does not carry this patch set's approval token"
    return True, "ok"


# --- Active-session detection ------------------------------------------------


def active_sessions(
    live_root: Path, projects: list[str], self_transcript: Path | None, now: float, window: int
) -> list[str]:
    """Sibling transcripts (``<live-root>/<project>/*.jsonl``) touched within the window.

    Scoped to affected projects and self-excluded by resolved path (when a
    transcript is known at all — ``--consent token`` has none), so the
    invoking session never flags itself.
    """
    self_resolved = (
        self_transcript.resolve() if self_transcript is not None and self_transcript.exists() else self_transcript
    )
    active: list[str] = []
    for project in projects:
        project_dir = live_root / project
        if not project_dir.is_dir():
            continue
        for session_file in sorted(project_dir.glob("*.jsonl")):
            if self_resolved is not None and session_file.resolve() == self_resolved:
                continue
            try:
                if now - session_file.stat().st_mtime <= window:
                    active.append(f"{project}/{session_file.name}")
            except OSError:
                continue
    return active


# --- Destination confinement --------------------------------------------------


def confined_dest(memory_dir: Path, rel: str) -> Path | None:
    """Resolve rel inside the project's memory dir, or None on any escape.

    Single implementation lives in `audit.py` so the assembler and apply
    share exactly one confinement check (rejects .md-less, absolute,
    traversal, NUL, symlink)."""
    return AUDIT.confined_path(memory_dir, rel)


# --- Snapshot backup ----------------------------------------------------------


def _snapshot_targets(by_project: dict[str, list[dict[str, Any]]], present: list[str]) -> dict[str, set[str]]:
    """Every relative path an approved, present-project proposal declares as a
    result or delete, plus ``MEMORY.md`` (every committed project gets an
    index refresh/reconciliation) and ``MEMORY-archive.md`` for any archive
    proposal (always appended to or created).

    Deliberately over-inclusive: a proposal a later validation gate skips
    still gets backed up harmlessly. It does NOT cover a file a later gate
    rewrites only as an emergent side effect rather than a declared result —
    concretely, inbound-wikilink retargeting of a bystander note that is not
    itself a proposal's source or result. Restoring after such a retarget
    will leave that bystander's rewritten links in place; see restore()'s
    docstring.
    """
    targets: dict[str, set[str]] = {}
    for project in present:
        rels = {"MEMORY.md"}
        for proposal in by_project.get(project, []):
            if proposal.get("action") == "archive":
                rels.add("MEMORY-archive.md")
                continue
            for result in proposal.get("results") or []:
                path = result.get("path") if isinstance(result, dict) else None
                if isinstance(path, str):
                    rels.add(path)
            for path in proposal.get("deletes") or []:
                if isinstance(path, str):
                    rels.add(path)
        targets[project] = rels
    return targets


def snapshot_patch_set(
    patch_set: Path, live: dict[str, Path], by_project: dict[str, list[dict[str, Any]]], present: list[str]
) -> str | None:
    """Copy every file about to be touched into ``<patch-set>/backup/`` before
    any live write, so `run_restore` can reverse this apply without a mirror.

    Writes ``<patch-set>/backup/<project>/<relative-path>`` (byte-identical
    copies) and ``<patch-set>/backup/backup-manifest.json`` recording
    ``{project, path, sha256}`` per file; a file that does not yet exist
    (this apply will CREATE it) is recorded with ``sha256: null`` so restore
    knows to delete it rather than restore bytes.

    Returns None on success, or an error message on failure. The caller must
    abort the whole apply on failure — no project has been written to yet.
    """
    backup_root = patch_set / "backup"
    targets = _snapshot_targets(by_project, present)
    manifest_entries: list[dict[str, Any]] = []
    try:
        for project in present:
            memory_dir = live[project]
            dest_dir = backup_root / project
            for rel in sorted(targets[project]):
                src = memory_dir / rel
                if src.is_file():
                    dest = dest_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(src.read_bytes())
                    manifest_entries.append({"project": project, "path": rel, "sha256": AUDIT.digest(src)})
                else:
                    manifest_entries.append({"project": project, "path": rel, "sha256": None})
        backup_root.mkdir(parents=True, exist_ok=True)
        compat.restrict_permissions(backup_root)
        (backup_root / "backup-manifest.json").write_text(
            json.dumps(
                {"schema_version": APPLY_SCHEMA_VERSION, "entries": manifest_entries}, indent=2, sort_keys=True
            )
            + "\n"
        )
    except OSError as error:
        return f"snapshot failed, refusing before any live write: {error}"
    return None


# --- Apply --------------------------------------------------------------------


def stage_and_commit(writes: dict[Path, "bytes | str"], deletes: list[Path]) -> None:
    """Apply a project's writes and deletes atomically with rollback.

    Staging (temp writes) fully precedes the commit, so a staging failure
    leaves the project untouched. The commit journals the original bytes of
    every overwritten or deleted file first, then renames and unlinks; on ANY
    commit-phase failure it restores every journaled file and removes
    newly-created ones, so the project is left exactly as it was. Only a
    failure DURING rollback (doubly exceptional) raises RollbackFailed with
    whatever could not be restored.

    ``writes`` values may be ``str`` (encoded as UTF-8, the normal case for
    generated note/index content) or raw ``bytes`` (used by `run_restore` to
    put snapshotted bytes back byte-for-byte, including any non-UTF-8
    bystander file a snapshot happened to capture).
    """
    # A destination that already exists as a directory cannot be replaced by a
    # file; catch it during staging so the project stays untouched.
    for dest in writes:
        if dest.is_dir():
            raise IsADirectoryError(f"destination is a directory: {dest}")
    temps: list[tuple[Path, Path]] = []
    try:
        for dest, content in writes.items():
            dest.parent.mkdir(parents=True, exist_ok=True)
            temp = dest.with_name(dest.name + ".dream-tmp")
            temp.unlink(missing_ok=True)  # sweep a stale temp from a prior hard-killed run
            data = content.encode("utf-8") if isinstance(content, str) else content
            temp.write_bytes(data)
            temps.append((temp, dest))
    except OSError:
        for temp, _dest in temps:
            temp.unlink(missing_ok=True)
        raise

    # Journal originals so the commit can roll back on any failure.
    overwritten: dict[Path, bytes] = {dest: dest.read_bytes() for _temp, dest in temps if dest.is_file()}
    created: list[Path] = [dest for _temp, dest in temps if not dest.exists()]
    deleted_backup: dict[Path, bytes] = {path: path.read_bytes() for path in deletes if path.is_file()}
    done_writes: list[Path] = []
    done_deletes: list[Path] = []
    try:
        for temp, dest in temps:
            os.replace(temp, dest)
            done_writes.append(dest)
        for path in deletes:
            if path.exists():
                path.unlink()
                done_deletes.append(path)
    except OSError as commit_error:
        # Roll the project back to its pre-commit state.
        failures: list[str] = []
        for temp, _dest in temps:
            temp.unlink(missing_ok=True)
        for path in done_deletes:
            try:
                path.write_bytes(deleted_backup[path])
            except OSError as restore_error:
                failures.append(f"restore-delete {path}: {restore_error}")
        for dest in done_writes:
            try:
                if dest in overwritten:
                    dest.write_bytes(overwritten[dest])
                elif dest in created:
                    dest.unlink(missing_ok=True)
            except OSError as restore_error:
                failures.append(f"restore-write {dest}: {restore_error}")
        if failures:
            raise RollbackFailed(failures) from commit_error
        raise RolledBack(str(commit_error)) from commit_error


class RolledBack(Exception):
    """Commit failed but the project was fully restored to its pre-apply state."""


class RollbackFailed(Exception):
    """Commit failed AND rollback could not fully restore; carries the unrestored items."""

    def __init__(self, failures: list[str]):
        super().__init__("; ".join(failures))
        self.failures = failures


def apply_project(
    project: str,
    memory_dir: Path,
    proposals: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply one project's approved proposals; return a per-proposal status record.

    Inbound-wikilink retargeting is computed here over exactly the approved
    deletion set, so an operator exclusion never leaves a dangling link and
    two proposals that close notes a shared bystander links to produce one
    coherent edit, not conflicting writes.
    """
    statuses: list[dict[str, str]] = []
    # Archive proposals are index-only: a dedicated path that touches exactly
    # MEMORY.md and MEMORY-archive.md (never through the generic note writer,
    # which by design refuses MEMORY.md as a destination). Bodies are untouched
    # and the move is reversible by moving the entry lines back.
    archive_proposals = [p for p in proposals if p.get("action") == "archive"]
    proposals = [p for p in proposals if p.get("action") != "archive"]
    for proposal in archive_proposals:
        pid = proposal.get("id", "?")
        entries = proposal.get("archive_entries") or []
        sources = proposal.get("sources") or []
        if not (isinstance(entries, list) and entries and all(isinstance(e, str) for e in entries)
                and isinstance(sources, list) and len(sources) == 1 and sources[0].get("path") == "MEMORY.md"):
            statuses.append({"id": pid, "status": "skipped", "reason": "malformed-archive-proposal"})
            continue
        index_path = memory_dir / "MEMORY.md"
        if not index_path.is_file() or AUDIT.digest(index_path) != sources[0].get("digest"):
            statuses.append({"id": pid, "status": "skipped", "reason": "source-changed-since-draft"})
            continue
        index_text = index_path.read_text(encoding="utf-8")
        missing = [e for e in entries if e not in index_text]
        if missing:
            statuses.append({"id": pid, "status": "skipped", "reason": "archive-entries-not-found"})
            continue
        # Same per-line filter the archive build used to compute its preview diff
        # (drop any line that appears in ANY entry being archived): since the
        # source-digest check above guarantees these are the identical bytes the
        # build read, this reproduces the previewed MEMORY.md byte-for-byte rather
        # than risking a second, subtly different removal algorithm at apply time.
        entry_lines = [entry.splitlines() for entry in entries]
        remaining = [line for line in index_text.splitlines() if all(line not in lines for lines in entry_lines)]
        new_index = "\n".join(remaining).rstrip() + "\n"
        pointer = "- Older or resolved entries are archived in MEMORY-archive.md (not auto-loaded); grep it when a topic is missing here."
        if pointer not in new_index:
            new_index += pointer + "\n"
        archive_path = memory_dir / "MEMORY-archive.md"
        existing_archive = archive_path.read_text(encoding="utf-8") if archive_path.is_file() else "# Archived memory index entries (not auto-loaded)\n"
        new_archive = existing_archive.rstrip() + "\n" + "\n".join(entries) + "\n"
        try:
            stage_and_commit({index_path: new_index, archive_path: new_archive}, [])
        except (RolledBack, RollbackFailed, OSError) as error:
            statuses.append({"id": pid, "status": "failed", "reason": f"archive-write: {error}"})
            continue
        statuses.append({"id": pid, "status": "applied", "reason": ""})
    applicable: list[dict[str, Any]] = []
    for proposal in proposals:
        pid = proposal.get("id", "?")
        results = proposal.get("results") or []
        deletes = proposal.get("deletes") or []
        sources = proposal.get("sources") or []
        # Structurally validate the manifest rather than trusting it: a wrong type
        # (e.g. "results": null) must skip, never crash the batch mid-apply.
        if not (
            isinstance(results, list) and isinstance(deletes, list) and isinstance(sources, list)
            and all(isinstance(r, dict) and isinstance(r.get("path"), str) and isinstance(r.get("content"), str) for r in results)
            and all(isinstance(d, str) for d in deletes)
            and all(isinstance(s, dict) and isinstance(s.get("path"), str) for s in sources)
        ):
            statuses.append({"id": pid, "status": "skipped", "reason": "malformed-proposal"})
            continue
        if proposal.get("sensitive"):
            statuses.append({"id": pid, "status": "skipped", "reason": "sensitive"})
            continue
        if proposal.get("action") == "leave" or (not results and not deletes):
            statuses.append({"id": pid, "status": "left", "reason": "no-op"})
            continue
        if any(
            not (memory_dir / source["path"]).exists() or AUDIT.digest(memory_dir / source["path"]) != source.get("digest")
            for source in sources
        ):
            statuses.append({"id": pid, "status": "skipped", "reason": "source-changed-since-draft"})
            continue
        if any(confined_dest(memory_dir, path) is None for path in [r["path"] for r in results] + deletes):
            statuses.append({"id": pid, "status": "skipped", "reason": "path-escape"})
            continue
        # A destination validated as fresh at build (a merge's new survivor, a split
        # extract) that has since appeared on disk would be silently overwritten; the
        # digest gate above only covers declared sources, so guard it here.
        source_paths = {source["path"] for source in sources}
        if any(
            result["path"] not in source_paths and (memory_dir / result["path"]).exists()
            for result in results
        ):
            statuses.append({"id": pid, "status": "skipped", "reason": "destination-appeared-since-draft"})
            continue
        applicable.append(proposal)

    # Order-independent conflict resolution across the whole approved batch: skip any
    # proposal that writes a path another proposal writes or deletes (or that it
    # deletes itself), or whose delete target is claimed by another. Applying such a
    # set would write-then-delete a result (data loss) or double-delete a note.
    # Claims cover ALL result paths, not just the survivor: a split writes extracts.
    writer_of: dict[str, list[str]] = {}
    deleter_of: dict[str, list[str]] = {}
    for proposal in applicable:
        for result in proposal.get("results") or []:
            writer_of.setdefault(result["path"], []).append(proposal["id"])
        for rel in proposal.get("deletes") or []:
            deleter_of.setdefault(rel, []).append(proposal["id"])
    conflicted: set[str] = set()
    for path, pids in writer_of.items():
        if len(pids) > 1:
            conflicted.update(pids)
        if path in deleter_of:
            conflicted.update(pids)
            conflicted.update(deleter_of[path])
    for path, pids in deleter_of.items():
        if len(pids) > 1:
            conflicted.update(pids)
    if conflicted:
        kept: list[dict[str, Any]] = []
        for proposal in applicable:
            if proposal["id"] in conflicted:
                statuses.append({"id": proposal["id"], "status": "skipped", "reason": "conflicting-survivor"})
            else:
                kept.append(proposal)
        applicable = kept

    if not applicable:
        archived_ok = any(st["status"] == "applied" for st in statuses)
        return {"project": project, "committed": archived_ok, "deleted": [], "proposals": statuses}

    records = AUDIT.scan_project_notes(memory_dir)
    by_canon, token_to_rel = AUDIT.link_resolver(records)
    delete_to_stem: dict[str, str] = {}
    result_paths: set[str] = set()
    # Pre-commit index entries of the notes being rewritten: the post-commit index
    # refresh replaces a line only when it still matches this canonical rendering
    # (anything else is a hand-edited line and is preserved); redescribe forces the
    # refresh because updating the routing hook is its operator-approved purpose.
    refresh: dict[str, dict[str, Any]] = {}
    for proposal in applicable:
        survivor = proposal.get("survivor")
        if survivor:
            for rel in proposal.get("deletes") or []:
                delete_to_stem[rel] = Path(survivor).name[:-3]
        for result in proposal.get("results") or []:
            rel = result["path"]
            result_paths.add(rel)
            if (memory_dir / rel).is_file():
                refresh[rel] = {
                    "old": AUDIT.entry_fields(memory_dir / rel, rel),
                    "new": None,  # filled post-commit from the rewritten note
                    "force": proposal.get("action") == "redescribe",
                }

    writes: dict[Path, Any] = {}
    for proposal in applicable:
        for result in proposal["results"]:
            dest = confined_dest(memory_dir, result["path"])
            assert dest is not None  # confinement already verified above
            writes[dest] = AUDIT.rewrite_links_to_deleted(result["content"], by_canon, token_to_rel, delete_to_stem)
    # Bystander notes that link to a deleted note: retarget once, over the approved set.
    for rel, _record in records.items():
        if rel in delete_to_stem or rel in result_paths:
            continue
        raw = (memory_dir / rel).read_bytes()
        if not AUDIT.is_valid_utf8(raw):
            continue  # never lossily rewrite a non-utf8 bystander
        original = raw.decode("utf-8")
        rewritten = AUDIT.rewrite_links_to_deleted(original, by_canon, token_to_rel, delete_to_stem)
        if rewritten != original:
            writes[memory_dir / rel] = rewritten

    deleted_rels = sorted({rel for proposal in applicable for rel in proposal.get("deletes") or []})
    deletes = [memory_dir / rel for rel in deleted_rels]
    try:
        stage_and_commit(writes, deletes)
    except RolledBack as error:
        # Commit failed but the project was fully restored: report untouched.
        for proposal in applicable:
            statuses.append({"id": proposal["id"], "status": "failed", "reason": f"rolled-back: {error}"})
        return {"project": project, "committed": False, "proposals": statuses}
    except RollbackFailed as error:
        for proposal in applicable:
            statuses.append({"id": proposal["id"], "status": "failed", "reason": "rollback incomplete"})
        return {"project": project, "committed": False, "rollback_failures": error.failures, "proposals": statuses}
    except OSError as error:
        for proposal in applicable:
            statuses.append({"id": proposal["id"], "status": "failed", "reason": f"write-error: {error}"})
        return {"project": project, "committed": False, "proposals": statuses}

    # Index maintenance runs OUTSIDE the note-commit boundary and must never turn a
    # committed apply into a reported failure: the notes are already durably written
    # and the deleted[] list must reach any mirror-prune step regardless. An index
    # error is reported separately; the next audit fix pass reconciles the drift.
    index_error: str | None = None
    try:
        # Single per-project index reconciliation over the final note set.
        # Appends are restricted to notes THIS apply wrote: corpus-wide healing of
        # pre-existing unindexed notes is its own routing-surface change and stays
        # a separately gated step (the audit tool's fix mode).
        reconcile = AUDIT.compute_index_reconcile(
            project, memory_dir, AUDIT.scan_project_notes(memory_dir), restrict_appends_to=result_paths
        )
        if reconcile:
            AUDIT.apply_index_reconcile(memory_dir / "MEMORY.md", memory_dir, reconcile)
        # Refresh rewritten notes' index entry lines to their new frontmatter hook:
        # the index line routes recall, so it must not keep the pre-consolidation
        # description (the corpus audit found the two surfaces diverging
        # independently). Hand-edited lines are preserved; see refresh_index_lines.
        for rel in list(refresh):
            refresh[rel]["new"] = AUDIT.entry_fields(memory_dir / rel, rel)
        AUDIT.apply_index_refresh(memory_dir / "MEMORY.md", refresh)
    except OSError as error:
        index_error = str(error)
    for proposal in applicable:
        statuses.append({"id": proposal["id"], "status": "applied", "reason": ""})
    # deleted[] feeds any mirror-deletion step so consolidated notes do not resurrect
    # from the mirror on restore (mirror git history keeps it recoverable there too).
    result: dict[str, Any] = {"project": project, "committed": True, "deleted": deleted_rels, "proposals": statuses}
    if index_error:
        result["index_error"] = index_error
    return result


def run_apply(args: argparse.Namespace) -> int:
    patch_set = Path(args.patch_set).expanduser()
    manifest_path = patch_set / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        selection = json.loads(Path(args.selection).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"memory-dream apply: cannot read patch set or selection: {error}", file=sys.stderr)
        return 2

    lock_path = Path(args.lock).expanduser() if args.lock else patch_set.parent / ".apply.lock"
    try:
        with compat.FileLock(lock_path):
            return _run_apply_locked(args, patch_set, manifest, selection)
    except compat.LockHeld as error:
        print(f"memory-dream apply: refusing, {error}", file=sys.stderr)
        return 1


def _run_apply_locked(
    args: argparse.Namespace, patch_set: Path, manifest: dict[str, Any], selection: dict[str, Any]
) -> int:
    live_root = Path(args.live_root).expanduser()
    mirror_root = Path(args.mirror_root).expanduser() if args.mirror_root else None
    transcript_path = Path(args.transcript).expanduser() if args.transcript else None
    now = args.now_ts if args.now_ts is not None else time.time()

    manifest_id = manifest.get("id")
    all_proposals = manifest.get("proposals", [])
    # Reject a manifest with duplicate proposal ids: an approved id must map to
    # exactly one proposal, or a tampered manifest could smuggle a second one.
    ids = [proposal.get("id") for proposal in all_proposals]
    if len(ids) != len(set(ids)):
        print("memory-dream apply: refusing, manifest has duplicate proposal ids", file=sys.stderr)
        return 1

    # Recompute the content-bound id from the actual proposals: if the manifest's
    # bytes were edited after the operator approved (the id kept, content changed),
    # the recomputed id no longer matches and apply refuses. Combined with the
    # consent check below (the operator typed this id), post-approval tampering is
    # caught.
    if manifest_id != AUDIT.content_id(all_proposals):
        print("memory-dream apply: refusing, manifest id does not match its proposal content", file=sys.stderr)
        return 1

    # Bind the selection to THIS patch set so an approval cannot be replayed
    # against a different one. Required in both consent modes: in --consent
    # token mode this IS the entire approval signal (the operator had to have
    # seen the preview to know the id to type here).
    if selection.get("patch_set_id") != manifest_id:
        print("memory-dream apply: refusing, selection patch_set_id does not match the manifest", file=sys.stderr)
        return 1

    if args.consent == "token":
        if not args.acknowledge_reduced_consent_check:
            print(
                "memory-dream apply: refusing, --consent token requires "
                "--acknowledge-reduced-consent-check (see SECURITY.md for what this reduces)",
                file=sys.stderr,
            )
            return 1
    else:
        if transcript_path is None:
            print("memory-dream apply: refusing, --consent trace requires --transcript", file=sys.stderr)
            return 1
        # Consent-trace verification. created_at_line must be present (a
        # missing value would make "post-preview" vacuous); the approval
        # token is the manifest id the operator typed to consent to this
        # specific patch set.
        if not isinstance(manifest.get("created_at_line"), int):
            print("memory-dream apply: refusing, manifest is missing created_at_line", file=sys.stderr)
            return 1
        ok, reason = verify_operator_trace(
            transcript_path, selection.get("operator_trace", {}), manifest["created_at_line"], str(manifest_id)
        )
        if not ok:
            print(f"memory-dream apply: refusing, operator trace invalid: {reason}", file=sys.stderr)
            return 1

    approved = set(selection.get("approved", []))
    proposals = [proposal for proposal in all_proposals if proposal.get("id") in approved]
    by_project: dict[str, list[dict[str, Any]]] = {}
    for proposal in proposals:
        by_project.setdefault(proposal["project"], []).append(proposal)

    live = AUDIT.project_dirs(live_root, live=True)
    vanished = [project for project in by_project if project not in live]
    # A symlinked project or memory root is refused everywhere else in this
    # system; never write through one here. Shared helper so the Windows
    # reparse-point/junction case is covered too, not just POSIX symlinks.
    symlinked = sorted(
        project for project in by_project
        if project in live and compat.is_escaping_link(live[project])
    )
    present = sorted(
        project for project in by_project
        if project in live and project not in symlinked
    )

    # Per-project mirror freshness for present projects — only when a mirror
    # is configured; snapshot mode (below) is the recovery path otherwise.
    if mirror_root is not None:
        stale = AUDIT.mirror_freshness(live_root, mirror_root, present)
        if stale:
            for project, kinds in sorted(stale.items()):
                print(
                    f"memory-dream apply: refusing, mirror not fresh for {project} "
                    f"({', '.join(kinds)}); {config.MIRROR_PUSH_HINT}",
                    file=sys.stderr,
                )
            return 1

    # Active-session warning (warn-only, self-excluded).
    active = active_sessions(live_root, present, transcript_path, now, args.active_window_seconds)
    for session in active:
        print(f"memory-dream apply: WARNING other Claude session appears active: {session}", file=sys.stderr)

    if args.preflight:
        # Report gate outcome and would-apply set; write nothing (the caller
        # uses this to let the operator abort on an active-session warning).
        print(
            json.dumps(
                {
                    "preflight": True,
                    "gates": "passed",
                    "active_sessions": active,
                    "present_projects": present,
                    "vanished_projects": vanished,
                    "approved": sorted(approved),
                },
                sort_keys=True,
            )
        )
        return 0

    # Snapshot backup (always on): copy every file about to be written or
    # deleted, plus each project's MEMORY.md, into the patch set BEFORE the
    # first live write. Skipped entirely when there is nothing to write
    # (every affected project vanished or is symlinked) — there is no first
    # write to precede.
    backed_up = False
    if present:
        snapshot_error = snapshot_patch_set(patch_set, live, by_project, present)
        if snapshot_error is not None:
            print(f"memory-dream apply: refusing, {snapshot_error}", file=sys.stderr)
            return 1
        backed_up = True

    results: list[dict[str, Any]] = []
    for project, reason in [(p, "project-vanished") for p in sorted(vanished)] + [(p, "live-symlink") for p in symlinked]:
        results.append(
            {
                "project": project,
                "committed": False,
                "proposals": [
                    {"id": proposal.get("id", "?"), "status": "skipped", "reason": reason}
                    for proposal in by_project[project]
                ],
            }
        )
    for project in present:
        try:
            results.append(apply_project(project, live[project], by_project[project]))
        except Exception as error:  # noqa: BLE001 - one bad project must never abort the batch or lose the manifest
            results.append(
                {
                    "project": project,
                    "committed": False,
                    "proposals": [
                        {"id": proposal.get("id", "?"), "status": "failed", "reason": f"apply error: {error}"}
                        for proposal in by_project[project]
                    ],
                }
            )

    applied = sum(1 for entry in results for status in entry["proposals"] if status["status"] == "applied")
    skipped = sum(1 for entry in results for status in entry["proposals"] if status["status"] in ("skipped", "left"))
    failed = sum(1 for entry in results for status in entry["proposals"] if status["status"] == "failed")
    # Applied deletions any mirror-prune step must remove so a restore does not
    # resurrect a consolidated note from the mirror (mirror git history keeps
    # it recoverable there regardless).
    deleted = sorted(
        (entry["project"], rel) for entry in results for rel in entry.get("deleted", [])
    )
    # Single-token next= keeps the completion line whitespace-splittable; the
    # human-readable hint (config.MIRROR_PUSH_HINT is free text) prints after.
    next_step = "mirror-sync" if mirror_root is not None else "none"
    apply_manifest = {
        "schema_version": APPLY_SCHEMA_VERSION,
        "patch_set": str(patch_set),
        "patch_set_id": manifest.get("id"),
        "applied": applied,
        "skipped": skipped,
        "failed": failed,
        "active_sessions": active,
        "backup": backed_up,
        "deleted": [{"project": project, "path": rel} for project, rel in deleted],
        "projects": results,
    }
    (patch_set / "apply-manifest.json").write_text(json.dumps(apply_manifest, indent=2, sort_keys=True) + "\n")
    # Machine-parseable completion line: the caller greps this, then runs any
    # mirror record step and prunes the deleted notes from the mirror.
    print(
        f"{COMPLETION_PREFIX} applied={applied} skipped={skipped} failed={failed} "
        f"projects={len(present)} deleted={len(deleted)} next={next_step}"
    )
    if mirror_root is not None:
        print(f"next step: {config.MIRROR_PUSH_HINT}")
    return 0


# --- Restore ------------------------------------------------------------------


def _expected_post_apply_digests(patch_set: Path) -> dict[tuple[str, str], str]:
    """Best-effort map of ``(project, path) -> expected sha256`` after this
    patch set's apply, derived from ``manifest.json``'s proposal results
    cross-referenced against ``apply-manifest.json``'s per-project applied
    statuses: only a proposal recorded as "applied" contributes, and its
    ``results[].content`` is exactly the bytes apply wrote for that path.

    Deliberately partial: inbound-wikilink retargeting of a bystander note
    and the MEMORY.md/MEMORY-archive.md index maintenance are not declared
    proposal content anywhere in the patch set, so no expected digest exists
    for them; the drift check in `run_restore` simply skips paths this
    returns nothing for rather than guessing.
    """
    manifest_path = patch_set / "manifest.json"
    apply_manifest_path = patch_set / "apply-manifest.json"
    if not manifest_path.is_file() or not apply_manifest_path.is_file():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        apply_manifest = json.loads(apply_manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    applied_ids: set[str] = set()
    for entry in apply_manifest.get("projects", []):
        for status in entry.get("proposals", []):
            if isinstance(status, dict) and status.get("status") == "applied":
                applied_ids.add(status.get("id"))
    expected: dict[tuple[str, str], str] = {}
    for proposal in manifest.get("proposals", []):
        if proposal.get("id") not in applied_ids:
            continue
        project = proposal.get("project")
        for result in proposal.get("results") or []:
            if not isinstance(result, dict):
                continue
            path, content = result.get("path"), result.get("content")
            if isinstance(project, str) and isinstance(path, str) and isinstance(content, str):
                expected[(project, path)] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return expected


def run_restore(args: argparse.Namespace) -> int:
    """Reverse an applied patch set from its snapshot backup.

    Refuses outright when the patch set has no backup directory (nothing to
    restore from — either it was never applied, or it predates the snapshot
    feature). Otherwise: for each backed-up file, warn (and require
    ``--force`` to proceed) if the live copy no longer matches what apply is
    on record as having written; then restore snapshotted bytes and delete
    anything apply created, per project, atomically.

    This does NOT undo inbound-wikilink retargeting of bystander notes (see
    `_snapshot_targets`): a bystander whose links were rewritten to route
    around a deletion this patch set made will keep those rewritten links.
    """
    patch_set = Path(args.patch_set).expanduser()
    backup_root = patch_set / "backup"
    backup_manifest_path = backup_root / "backup-manifest.json"
    if not backup_root.is_dir() or not backup_manifest_path.is_file():
        print(f"memory-dream restore: refusing, no backup found under {backup_root}; nothing to restore", file=sys.stderr)
        return 1

    lock_path = Path(args.lock).expanduser() if args.lock else patch_set.parent / ".apply.lock"
    try:
        with compat.FileLock(lock_path):
            return _run_restore_locked(args, patch_set, backup_root, backup_manifest_path)
    except compat.LockHeld as error:
        print(f"memory-dream restore: refusing, {error}", file=sys.stderr)
        return 1


def _run_restore_locked(args: argparse.Namespace, patch_set: Path, backup_root: Path, backup_manifest_path: Path) -> int:
    try:
        backup_manifest = json.loads(backup_manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"memory-dream restore: cannot read backup manifest: {error}", file=sys.stderr)
        return 2
    entries = backup_manifest.get("entries")
    if not isinstance(entries, list):
        print("memory-dream restore: backup manifest is malformed (no entries list)", file=sys.stderr)
        return 2

    live_root = Path(args.live_root).expanduser()
    by_project: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("project"), str) and isinstance(entry.get("path"), str):
            by_project.setdefault(entry["project"], []).append(entry)

    # Drift check: compare each live file this apply is on record as having
    # written against its current bytes, where a recorded expectation exists.
    expected = _expected_post_apply_digests(patch_set)
    drift: list[str] = []
    for project, project_entries in sorted(by_project.items()):
        memory_dir = live_root / project / "memory"
        for entry in project_entries:
            rel = entry["path"]
            exp_digest = expected.get((project, rel))
            if exp_digest is None:
                continue
            live_file = memory_dir / rel
            if not live_file.is_file():
                drift.append(f"{project}/{rel}: expected to exist post-apply, now missing")
            elif AUDIT.digest(live_file) != exp_digest:
                drift.append(f"{project}/{rel}: live content differs from what apply wrote")

    if drift and not args.force:
        print("memory-dream restore: refusing, live memory has drifted since this apply:", file=sys.stderr)
        for line in drift:
            print(f"  - {line}", file=sys.stderr)
        print("memory-dream restore: rerun with --force to restore anyway (this OVERWRITES the drifted content)", file=sys.stderr)
        return 1
    for line in drift:
        print(f"memory-dream restore: WARNING proceeding over drift ({line})", file=sys.stderr)

    restored = 0
    deleted = 0
    projects_touched = 0
    for project, project_entries in sorted(by_project.items()):
        memory_dir = live_root / project / "memory"
        if not memory_dir.is_dir():
            print(f"memory-dream restore: WARNING project {project} no longer exists live; skipping", file=sys.stderr)
            continue
        writes: dict[Path, bytes] = {}
        deletes: list[Path] = []
        for entry in project_entries:
            rel = entry["path"]
            sha256 = entry.get("sha256")
            dest = memory_dir / rel
            if sha256 is None:
                # Did not exist before this apply: restoring means removing it.
                deletes.append(dest)
                continue
            backup_file = backup_root / project / rel
            if not backup_file.is_file():
                print(
                    f"memory-dream restore: WARNING backup copy missing for {project}/{rel}; cannot restore this file",
                    file=sys.stderr,
                )
                continue
            writes[dest] = backup_file.read_bytes()
        try:
            stage_and_commit(writes, deletes)
        except (RolledBack, RollbackFailed, OSError) as error:
            print(f"memory-dream restore: {project}: restore failed, project left as-is: {error}", file=sys.stderr)
            continue
        restored += len(writes)
        deleted += len(deletes)
        projects_touched += 1

    print(
        f"{RESTORE_COMPLETE_PREFIX} restored={restored} deleted={deleted} "
        f"projects={projects_touched} drift_warnings={len(drift)}"
    )
    return 0


# --- CLI ----------------------------------------------------------------------


def add_parsers(subparsers) -> None:
    apply_p = subparsers.add_parser("apply", help="apply an approved patch-set selection to live memory")
    apply_p.add_argument("--patch-set", required=True, help="dream-pass patch-set directory (contains manifest.json)")
    apply_p.add_argument("--selection", required=True, help="operator selection JSON (approved ids + operator_trace)")
    apply_p.add_argument(
        "--transcript",
        default=None,
        help="invoking session's transcript .jsonl; required for --consent trace (the default)",
    )
    config.add_root_args(apply_p)
    apply_p.add_argument("--lock", default=None, help="lock path (default: <patch-set-parent>/.apply.lock)")
    apply_p.add_argument("--active-window-seconds", type=int, default=config.ACTIVE_WINDOW_SECONDS)
    apply_p.add_argument("--now-ts", type=float, default=None, help="inject epoch seconds for deterministic active-session tests")
    apply_p.add_argument("--preflight", action="store_true", help="run gates and report, write nothing")
    apply_p.add_argument(
        "--consent",
        choices=("trace", "token"),
        default="trace",
        help="trace (default): verify a real post-preview operator transcript turn. "
        "token: skip transcript verification; approval is the operator-typed "
        "patch-set id in the selection JSON alone (requires "
        "--acknowledge-reduced-consent-check).",
    )
    apply_p.add_argument(
        "--acknowledge-reduced-consent-check",
        action="store_true",
        help="required together with --consent token; see SECURITY.md for what this reduces",
    )
    apply_p.set_defaults(func=run_apply)

    restore_p = subparsers.add_parser("restore", help="reverse an applied patch-set from its snapshot backup")
    restore_p.add_argument("--patch-set", required=True, help="the patch-set directory that was applied")
    restore_p.add_argument("--live-root", default=str(config.default_live_root()))
    restore_p.add_argument("--force", action="store_true", help="restore even if live memory has drifted since the apply")
    restore_p.add_argument("--lock", default=None, help="lock path (default: <patch-set-parent>/.apply.lock)")
    restore_p.set_defaults(func=run_restore)
