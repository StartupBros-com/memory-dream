"""Deterministic assembly for the consolidation pass: cluster, validate, retarget, assemble.

Two stages, both LLM-free. `plan` reads triage output, groups flagged notes into
per-project near-duplicate clusters, drops any cluster with a sensitive member
whole (routed to manual review), and caps clusters/notes per pass with overflow
deferred. The orchestrating command dispatches one zero-tool subagent per cluster
(the zero-tool drafter gate) and feeds the drafts to `build`, which schema-
validates each proposal (schema validation), confines every destination
(destination confinement), retargets same-project inbound wikilinks to survivors
(inbound-wikilink retargeting), pulls any proposal whose resulting file fails an
audit dry-run (the post-build audit dry-run gate), assigns stable IDs, and writes
the patch set (manifest + unified diffs + preview) the apply command consumes.

A third, independent tier (`archive`) is entirely deterministic and index-only:
it demotes cold MEMORY.md entries to an archive file without touching any note
body, gated by the same operator preview and approval flow as everything else.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import html
import json
import re
import sys
from pathlib import Path
from typing import Any

from memory_dream import audit as AUDIT
from memory_dream import compat, config

ACTIONS = {"period-close", "merge", "compress", "split", "redescribe", "leave"}


# --- Stable IDs --------------------------------------------------------------


def stable_id(project: str, targets: list[str], action: str) -> str:
    key = f"{project}|{'|'.join(sorted(targets))}|{action}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


# --- Clustering ----------------------------------------------------------------


def body_tokens(body: str) -> set[str]:
    return set(re_findall_words(body))


def re_findall_words(body: str) -> list[str]:
    return re.findall(r"[a-z0-9]{4,}", body.lower())


def build_clusters(
    flagged: list[dict[str, Any]],
    sensitive: set[tuple[str, str]],
    max_clusters: int,
    max_notes: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Group flagged notes into per-project near-duplicate clusters.

    Returns (clusters, deferred, manual_review). Union-find joins two notes whose
    body-token Jaccard clears config.MERGE_JACCARD. A cluster with any sensitive
    member is dropped whole to manual_review. Oversized clusters keep their top
    notes by score and defer the rest; clusters beyond the per-pass cap are
    deferred.
    """
    manual_review: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    by_project: dict[str, list[dict[str, Any]]] = {}
    for note in flagged:
        by_project.setdefault(note["project"], []).append(note)

    clusters: list[dict[str, Any]] = []
    for project in sorted(by_project):
        notes = sorted(by_project[project], key=lambda note: note["path"])
        parent = list(range(len(notes)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        tokens = [body_tokens(note["body"]) for note in notes]
        for i in range(len(notes)):
            for j in range(i + 1, len(notes)):
                union = tokens[i] | tokens[j]
                if union and len(tokens[i] & tokens[j]) / len(union) >= config.MERGE_JACCARD:
                    parent[find(i)] = find(j)

        groups: dict[int, list[dict[str, Any]]] = {}
        for index, note in enumerate(notes):
            groups.setdefault(find(index), []).append(note)

        for members in groups.values():
            paths = [note["path"] for note in members]
            if any((project, path) in sensitive for path in paths):
                for path in sorted(paths):
                    manual_review.append({"project": project, "path": path, "reason": "sensitive"})
                continue
            ordered = sorted(members, key=lambda note: (-note["score"], note["path"]))
            kept = ordered[:max_notes]
            for note in ordered[max_notes:]:
                deferred.append({"project": project, "path": note["path"], "reason": "cluster-size-cap"})
            cluster_paths = sorted(note["path"] for note in kept)
            clusters.append(
                {
                    "cluster_id": stable_id(project, cluster_paths, "cluster"),
                    "project": project,
                    "top_score": max(note["score"] for note in kept),
                    "notes": [
                        {
                            "path": note["path"],
                            "name": note.get("name"),
                            "body": note["body"],
                            "score": note["score"],
                            "supersessions": note.get("supersessions", 0),
                        }
                        for note in sorted(kept, key=lambda note: note["path"])
                    ],
                }
            )

    clusters.sort(key=lambda cluster: (-cluster["top_score"], cluster["project"], cluster["cluster_id"]))
    kept_clusters = clusters[:max_clusters]
    for cluster in clusters[max_clusters:]:
        deferred.append({"project": cluster["project"], "cluster_id": cluster["cluster_id"], "reason": "per-pass-cap"})
    return kept_clusters, deferred, manual_review


# --- Plan stage --------------------------------------------------------------


def sensitive_paths(live_root: Path, mirror_root: Path | None) -> set[tuple[str, str]]:
    """(project, path) pairs the auditor flags as sensitive, so clusters can skip them.

    Runs the shared audit even in snapshot mode (no mirror configured); only the
    sensitive_* findings are consulted here, never the mirror_* ones.
    """
    args = argparse.Namespace(
        live_root=str(live_root),
        mirror_root=str(mirror_root) if mirror_root else None,
        scan_content=True,
        as_of=None,
        stale_days=config.AUDIT_STALE_DAYS,
        max_index_bytes=config.AUDIT_MAX_INDEX_BYTES,
        max_index_lines=config.AUDIT_MAX_INDEX_LINES,
    )
    result = AUDIT.audit(args)
    flagged: set[tuple[str, str]] = set()
    for finding in result["findings"]:
        if finding["kind"].startswith("sensitive_") and "path" in finding:
            flagged.add((finding["project"], finding["path"]))
    return flagged


def collect_flagged(live_root: Path, now: dt.date) -> list[dict[str, Any]]:
    """Flagged notes with bodies attached, reusing the auditor's triage scoring."""
    live = AUDIT.project_dirs(live_root, live=True)
    flagged: list[dict[str, Any]] = []
    for project in sorted(live):
        memory_dir = live[project]
        if memory_dir.is_symlink() or memory_dir.parent.is_symlink():
            continue
        records = AUDIT.scan_project_notes(memory_dir)
        _count, project_flagged = AUDIT.triage_project(project, memory_dir, now)
        # Consolidation-refire suppression, mirroring triage: a note a recent
        # applied pass already touched is already in its durable form and
        # should not be re-flagged within the suppression window.
        recently_applied = AUDIT.recently_applied_paths(config.SUPPRESS_APPLIED_DAYS, now)
        project_flagged = [
            note for note in project_flagged if (project, note["path"]) not in recently_applied
        ]
        for note in project_flagged:
            record = records.get(note["path"], {})
            flagged.append(
                {
                    "project": project,
                    "path": note["path"],
                    "name": record.get("name"),
                    "body": record.get("body", ""),
                    "score": note["score"],
                    "supersessions": note["supersessions"],
                }
            )
    return flagged


def run_plan(args: argparse.Namespace) -> int:
    live_root = Path(args.live_root).expanduser()
    if not live_root.is_dir():
        print(f"memory-dream: live root is not a directory: {live_root}", file=sys.stderr)
        return 2
    mirror_root = Path(args.mirror_root).expanduser() if args.mirror_root else None
    now = args.now or dt.date.today()
    flagged = collect_flagged(live_root, now)
    sensitive = sensitive_paths(live_root, mirror_root)
    clusters, deferred, manual_review = build_clusters(
        flagged, sensitive, args.max_clusters, args.max_notes
    )
    plan = {
        "schema_version": 1,
        "stage": "plan",
        "caps": {"max_clusters": args.max_clusters, "max_notes_per_cluster": args.max_notes},
        "clusters": clusters,
        "deferred": deferred,
        "manual_review": manual_review,
    }
    # Pretty-printed on purpose: zero-tool drafters Read this file directly, and
    # a single-line dump forces them into chunked grep reconstruction of their
    # own cluster (measured during the authors' consolidation campaign: early
    # drafters given a single-line dump burned 30+ tool calls each on that
    # reconstruction).
    print(json.dumps(plan, sort_keys=True, indent=1))
    # Token shaping, not a behavior change: with shards, each drafter reads only
    # its own cluster (~1/N of the plan) instead of every cluster's note bodies.
    # Defaults under the session scratch dir so it is never left unset by
    # accident; pass --shards-dir explicitly to put it somewhere durable.
    shards_dir = Path(args.shards_dir).expanduser() if args.shards_dir else (config.scratch_dir() / "shards")
    shards_dir.mkdir(parents=True, exist_ok=True)
    for cluster in clusters:
        shard = {"schema_version": 1, "stage": "plan-shard", "cluster": cluster}
        (shards_dir / f"{cluster['cluster_id']}.json").write_text(
            json.dumps(shard, sort_keys=True, indent=1) + "\n"
        )
    print(f"memory-dream: {len(clusters)} plan shard(s) -> {shards_dir}", file=sys.stderr)
    return 0


# --- Build stage -------------------------------------------------------------


def validate_proposal(proposal: dict[str, Any]) -> str | None:
    """Schema-validate one drafter proposal; return an error string or None (the schema validation gate)."""
    if not isinstance(proposal, dict):
        return "proposal is not an object"
    action = proposal.get("action")
    if action not in ACTIONS:
        return f"action must be one of {sorted(ACTIONS)}"
    if not isinstance(proposal.get("justification"), str) or not proposal["justification"].strip():
        return "justification missing"
    deletes = proposal.get("deletes", [])
    if not isinstance(deletes, list) or not all(isinstance(item, str) for item in deletes):
        return "deletes must be a list of paths"
    extracts = proposal.get("extracts") or []
    if action != "split" and extracts:
        return "only split carries extracts"
    survivor = proposal.get("survivor")
    if action == "leave":
        if survivor is not None or deletes:
            return "leave takes no survivor or deletes"
        return None
    if action in ("compress", "split", "redescribe") and deletes:
        return f"{action} deletes nothing (it rewrites in place)"
    if action in ("period-close", "merge") and not deletes:
        return f"{action} must delete at least one source note"
    if not isinstance(survivor, dict) or not isinstance(survivor.get("path"), str):
        return "survivor must carry a path"
    if action == "redescribe":
        # Description-only rewrite: the drafter supplies the new one-liner, never
        # a body, so a redescribe structurally cannot change note content.
        description = survivor.get("description")
        if not isinstance(description, str) or not description.strip():
            return "redescribe survivor must carry a description"
        return None
    if not isinstance(survivor.get("content"), str) or not survivor["content"].strip():
        return "survivor must carry content"
    if action == "split":
        if not isinstance(extracts, list) or not extracts:
            return "split must carry at least one extract"
        # A split fans one mega-note out into atomic notes; cap the fan-out so a
        # single proposal stays reviewable and a runaway draft cannot spray files.
        if len(extracts) > config.MAX_SPLIT_EXTRACTS:
            return f"split carries more than {config.MAX_SPLIT_EXTRACTS} extracts"
        if not all(
            isinstance(extract, dict)
            and isinstance(extract.get("path"), str)
            and isinstance(extract.get("content"), str)
            and extract["content"].strip()
            for extract in extracts
        ):
            return "each extract must carry a path and content"
        paths = [extract["path"] for extract in extracts]
        if len(set(paths)) != len(paths) or survivor["path"] in paths:
            return "extract paths must be unique and distinct from the survivor"
    return None


def result_audit_problems(rel: str, content: str) -> list[str]:
    """Per-file audit dry-run: frontmatter validity + sensitive scan (the post-build audit dry-run gate)."""
    problems: list[str] = []
    if AUDIT.SENSITIVE_FILENAME_RE.search(Path(rel).name):
        problems.append("sensitive_filename")
    if any(pattern.search(content) for pattern in AUDIT.CONTENT_SIGNATURES):
        problems.append("sensitive_content")
    metadata, error, _body, _raw = AUDIT.parse_frontmatter(content)
    if error:
        problems.append(f"frontmatter: {error}")
        return problems
    for key in ("name", "description"):
        value = metadata.get(key)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"missing {key}")
    description = metadata.get("description")
    if isinstance(description, str) and description.strip() and len(description.split()) < config.TRIAGE_DESC_MIN_WORDS:
        # Recall is routed by this one-liner; a consolidation must never produce
        # a description triage would immediately re-flag as unroutable.
        problems.append("description too short to route recall")
    nested = metadata.get("metadata")
    note_type = nested.get("type") if isinstance(nested, dict) else metadata.get("type")
    if note_type not in AUDIT.ALLOWED_TYPES:
        problems.append("type must be user|feedback|project|reference")
    sensitive_keys = [key for key in metadata if AUDIT.SENSITIVE_KEY_RE.search(key)]
    if isinstance(nested, dict):
        sensitive_keys += [key for key in nested if AUDIT.SENSITIVE_KEY_RE.search(key)]
    if sensitive_keys:
        problems.append("sensitive_frontmatter")
    return problems


def anticipated_retargets(memory_dir: Path, project_proposals: list[dict[str, Any]]) -> dict[str, str]:
    """Preview-only: the bystander edits apply WOULD make if every proposal is approved.

    Apply recomputes this over the actually-approved deletion set (inbound-
    wikilink retargeting); this is the all-approved projection so the operator
    sees inbound-link changes in the preview. Uses the same shared rewrite
    helper as apply, so the two never disagree.
    """
    records = AUDIT.scan_project_notes(memory_dir)
    by_canon, token_to_rel = AUDIT.link_resolver(records)
    delete_to_stem: dict[str, str] = {}
    survivor_paths: set[str] = set()
    for proposal in project_proposals:
        survivor = proposal.get("survivor")
        if survivor:
            survivor_paths.add(survivor)
            for rel in proposal.get("deletes", []):
                delete_to_stem[rel] = Path(survivor).name[:-3]
    if not delete_to_stem:
        return {}
    edits: dict[str, str] = {}
    for rel, record in records.items():
        if rel in delete_to_stem or rel in survivor_paths:
            continue
        original = (memory_dir / rel).read_bytes().decode("utf-8", errors="replace")
        rewritten = AUDIT.rewrite_links_to_deleted(original, by_canon, token_to_rel, delete_to_stem)
        if rewritten != original:
            edits[rel] = rewritten
    return edits


def anticipated_index_texts(memory_dir: Path, project_proposals: list[dict[str, Any]]) -> tuple[str, str] | None:
    """(current, projected) MEMORY.md text for the all-approved projection, or None
    when the index would not change. Mirrors apply's reconcile-then-refresh exactly
    (same shared helpers), so the operator reviews the real index change as part
    of the operator preview and item-by-item approval."""
    index_path = memory_dir / "MEMORY.md"
    if not index_path.is_file() or index_path.is_symlink():
        return None
    current = index_path.read_text(encoding="utf-8", errors="replace")
    records = AUDIT.scan_project_notes(memory_dir)
    deleted = {rel for proposal in project_proposals for rel in proposal.get("deletes", [])}
    # Every resulting file (survivor AND split extracts) shapes the index preview.
    content_by_rel: dict[str, str] = {}
    refresh: dict[str, dict[str, Any]] = {}
    for proposal in project_proposals:
        for result in proposal.get("results", []):
            rel = result["path"]
            content_by_rel[rel] = result["content"]
            record = records.get(rel)
            if record is not None:
                old_name = record.get("name") or Path(rel).name[:-3]
                refresh[rel] = {
                    "old": (old_name, record.get("description") or ""),
                    "new": AUDIT.frontmatter_entry(result["content"], rel),
                    "force": proposal.get("action") == "redescribe",
                }
    final_rels = (set(records) - deleted) | set(content_by_rel)
    # Same append restriction as apply: only pass-created notes enter the index;
    # corpus-wide healing is a separate operator decision (fix mode).
    reconcile = AUDIT.compute_index_reconcile(
        "_", memory_dir, {rel: {} for rel in final_rels}, restrict_appends_to=set(content_by_rel)
    )
    reconciled = AUDIT.render_index_reconcile(current, memory_dir, reconcile, content_by_rel) if reconcile else current
    reconciled = AUDIT.refresh_index_lines(reconciled, refresh)
    if reconciled == current:
        return None
    return current, reconciled


def anticipated_index_diff(memory_dir: Path, project_proposals: list[dict[str, Any]]) -> str | None:
    """Unified-diff wrapper over anticipated_index_texts for the .diff artifact."""
    texts = anticipated_index_texts(memory_dir, project_proposals)
    if texts is None:
        return None
    current, reconciled = texts
    return "".join(
        difflib.unified_diff(
            current.splitlines(keepends=True), reconciled.splitlines(keepends=True),
            fromfile="a/MEMORY.md", tofile="b/MEMORY.md",
        )
    )


def confined(memory_dir: Path, rel: str) -> bool:
    """Single shared confinement check (destination confinement); see audit.confined_path."""
    return AUDIT.confined_path(memory_dir, rel) is not None


def _primary_source(deletes: list[str], cluster: dict[str, Any]) -> str | None:
    """The source note whose frontmatter a fresh survivor inherits: the highest-triage
    (most substantial) deleted note in the cluster, a proxy for 'holds current truth'."""
    if not deletes:
        return None
    by_score = {note["path"]: note.get("score", 0) for note in cluster["notes"]}
    return sorted(deletes, key=lambda rel: (-by_score.get(rel, 0), rel))[0]


def _note_body_digest(body: str) -> str:
    """sha256 hex digest of a note's BODY (frontmatter-stripped, the same
    normalization AUDIT.scan_project_notes/AUDIT.parse_frontmatter apply when
    a file is read). Comparable between plan.json's verbatim-recorded body and
    a freshly re-read live file's body — unlike AUDIT.digest, which hashes a
    file's full raw bytes including frontmatter and would never match a
    body-only value even with zero drift."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _has_decay_fields(content: str) -> bool:
    """True when the note's nested metadata carries the decay pair
    (confidence + last_validated) that the audit's decay enforcement keys on."""
    metadata, error, _body, _raw = AUDIT.parse_frontmatter(content)
    if error or not isinstance(metadata, dict):
        return False
    nested = metadata.get("metadata")
    return isinstance(nested, dict) and "confidence" in nested and "last_validated" in nested


def assemble_proposals(
    clusters: list[dict[str, Any]],
    drafts: dict[str, list[dict[str, Any]]],
    live_root: Path,
    stamp: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Turn drafter output into apply-ready proposals; return (proposals, dropped).

    `stamp` (YYYY-MM-DD) is the revalidation date: new extracts get full decay
    frontmatter (confidence/maturity/last_validated) so dream output participates
    in the audit's decay enforcement like any other note does (measured 2026-07-31
    during the authors' consolidation campaign: dream extracts previously carried
    no decay frontmatter and were permanently decay-exempt); survivors that
    already carry decay fields get last_validated bumped — the pass's fidelity
    gates just re-verified their content, which is what validation means.
    """
    live = AUDIT.project_dirs(live_root, live=True)
    proposals: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    records_cache: dict[str, dict[str, dict[str, Any]]] = {}

    for cluster in clusters:
        project = cluster["project"]
        cluster_id = cluster["cluster_id"]
        memory_dir = live.get(project)
        if memory_dir is None:
            dropped.append({"cluster_id": cluster_id, "reason": "project-vanished"})
            continue
        if project not in records_cache:
            records_cache[project] = AUDIT.scan_project_notes(memory_dir)
        records = records_cache[project]
        cluster_paths = {note["path"] for note in cluster["notes"]}

        for proposal in drafts.get(cluster_id, []):
            error = validate_proposal(proposal)
            if error:
                dropped.append({"cluster_id": cluster_id, "reason": f"schema: {error}"})
                continue
            if proposal["action"] == "leave":
                # A leave carries no survivor or deletes, so cluster paths alone
                # collide when one cluster yields several leaves; bind the id to
                # the justification too (identical justifications ARE the same
                # proposal, so that residual collapse is correct).
                leave_key = hashlib.sha256(proposal["justification"].encode("utf-8")).hexdigest()[:16]
                proposals.append(
                    {
                        "id": stable_id(project, sorted(cluster_paths) + [leave_key], "leave"),
                        "project": project,
                        "action": "leave",
                        "sources": [],
                        "results": [],
                        "deletes": [],
                        "justification": proposal["justification"],
                        "sensitive": False,
                    }
                )
                continue

            survivor = proposal["survivor"]
            deletes = list(proposal.get("deletes", []))
            extracts = list(proposal.get("extracts") or []) if proposal["action"] == "split" else []
            extract_paths = [extract["path"] for extract in extracts]
            targets = sorted(set(deletes) | {survivor["path"]} | set(extract_paths))
            # Deletes may only remove notes the operator actually reviewed in this
            # cluster; the survivor may be a fresh or existing note (destination
            # confinement applies to every path below).
            if any(path not in cluster_paths for path in deletes):
                dropped.append({"cluster_id": cluster_id, "reason": "delete outside cluster"})
                continue
            if any(not confined(memory_dir, path) for path in [survivor["path"], *deletes, *extract_paths]):
                dropped.append({"cluster_id": cluster_id, "reason": "path-escape"})
                continue
            # The survivor may be a brand-new note or a note the operator reviewed in
            # this cluster, but never an arbitrary existing note outside it: that would
            # let an injection-steered draft overwrite an unrelated note.
            if survivor["path"] not in cluster_paths and (memory_dir / survivor["path"]).exists():
                dropped.append({"cluster_id": cluster_id, "reason": "survivor outside cluster"})
                continue
            # compress, split, and redescribe rewrite an existing reviewed note IN
            # PLACE: a fresh survivor path would leave the flagged note untouched
            # while creating an unreviewed sibling beside it.
            if proposal["action"] in ("compress", "split", "redescribe") and (
                survivor["path"] not in cluster_paths or survivor["path"] not in records
            ):
                dropped.append({"cluster_id": cluster_id, "reason": f"{proposal['action']} survivor not an existing cluster note"})
                continue
            # Extracts are strictly NEW notes: overwriting any existing note through a
            # split would let a draft clobber an unreviewed bystander.
            if any(path in records or (memory_dir / path).exists() for path in extract_paths):
                dropped.append({"cluster_id": cluster_id, "reason": "extract path already exists"})
                continue

            # Preserve the donor note's frontmatter (node_type, originSessionId, and any
            # schema field the harness maintains) rather than let the drafter's minimal
            # frontmatter strip them. Donor = the survivor when it already exists, else
            # the primary source note (highest triage relevance) holding current truth.
            donor_rel = survivor["path"] if survivor["path"] in records else _primary_source(deletes, cluster)
            donor_content = (
                (memory_dir / donor_rel).read_bytes().decode("utf-8", errors="replace")
                if donor_rel is not None
                else None
            )

            if proposal["action"] == "redescribe":
                # Built from the donor by construction: only the description line
                # changes, so body and name are preserved byte-for-byte.
                assert donor_content is not None  # guaranteed by the existing-note check above
                rebuilt = AUDIT.redescribe_content(donor_content, survivor["description"])
                if rebuilt is None:
                    dropped.append({"cluster_id": cluster_id, "reason": "redescribe: donor has no description line"})
                    continue
                survivor_content = rebuilt
                # Decay revalidation bump: centralized here, alongside the
                # merge/period-close arm below, for any donor already carrying the
                # decay pair (adding it to a non-decay note would opt it into a
                # regime its author never chose). Redescribe rebuilds from the same
                # donor and passes the same fidelity/repo-grounding/quality-panel
                # pipeline as any other survivor, so it earns the same revalidation
                # — this used to be skipped entirely because the bump lived only in
                # the merge/period-close branch below. A single preserve_metadata
                # call straight from `rebuilt` (not a second pass over an
                # already-merged survivor_content) so no other field is disturbed.
                if _has_decay_fields(donor_content):
                    survivor_content = AUDIT.preserve_metadata(
                        rebuilt, donor_content, {"last_validated": stamp}
                    )
            else:
                # Honest multi-source provenance: a merge or close-out that folds
                # several sessions' notes into one records every distinct origin
                # instead of keeping only the donor's (never fabricates one).
                extra_nested: dict[str, str] = {}
                if proposal["action"] in ("merge", "period-close"):
                    origin_ids = set()
                    for rel in {*deletes, *( {survivor["path"]} if survivor["path"] in records else set() )}:
                        sid = AUDIT.origin_session_id(
                            (memory_dir / rel).read_bytes().decode("utf-8", errors="replace")
                        )
                        if sid:
                            origin_ids.add(sid)
                    if len(origin_ids) >= 2:
                        extra_nested["originSessionIds"] = ", ".join(sorted(origin_ids))
                # Decay revalidation bump: folded into the SAME preserve_metadata
                # call as any origin_ids above (not a second pass), since a second
                # call would rebuild from the donor's ORIGINAL frontmatter and
                # silently drop whatever this call just added.
                if donor_content is not None and _has_decay_fields(donor_content):
                    extra_nested["last_validated"] = stamp
                survivor_content = survivor["content"]
                if donor_content is not None:
                    survivor_content = AUDIT.preserve_metadata(
                        survivor["content"], donor_content, extra_nested or None
                    )

            results = [{"path": survivor["path"], "content": survivor_content}]
            for extract in extracts:
                # Each extract inherits the split note's frontmatter schema fields
                # (node_type, originSessionId): honest provenance, they derive from it.
                # Extracts also get FULL decay frontmatter: they are new notes born
                # from a fidelity-verified pass, entering at candidate maturity.
                assert donor_content is not None
                results.append(
                    {
                        "path": extract["path"],
                        "content": AUDIT.preserve_metadata(
                            extract["content"],
                            donor_content,
                            {
                                "confidence": config.NEW_EXTRACT_CONFIDENCE,
                                "maturity": config.NEW_EXTRACT_MATURITY,
                                "last_validated": stamp,
                            },
                        ),
                    }
                )
            # Every extract must be reachable from the survivor by a [[stem]] wikilink
            # (Zettelkasten contract): a split that orphans its atomic notes leaves
            # them invisible to link-following recall.
            unlinked = [
                path for path in extract_paths
                if f"[[{Path(path).name[:-3]}]]" not in survivor_content
            ]
            if unlinked:
                dropped.append({"cluster_id": cluster_id, "reason": f"split survivor does not link: {', '.join(sorted(unlinked))}"})
                continue
            # Inbound-wikilink retargeting is applied by the apply command over the
            # actually-approved deletion set, not baked in here: two proposals that
            # close notes a third one links to would otherwise emit conflicting edits
            # to the same bystander file. Every resulting file is audited here.
            problems = [
                f"{result['path']}: {', '.join(file_problems)}"
                for result in results
                if (file_problems := result_audit_problems(result["path"], result["content"]))
            ]
            if problems:
                dropped.append({"cluster_id": cluster_id, "reason": f"audit dry-run: {'; '.join(problems)}"})
                continue
            # Frontmatter name must equal the filename stem: the index renders
            # "- [name](path)", so a mismatched name becomes wrong link text and
            # routing judges return it as a pseudo-path (observed during the
            # authors' consolidation campaign: 5 of 14 drafted files had drifted
            # names before an A/B panel pass caught it).
            name_mismatches = []
            for result in results:
                fm_name = (AUDIT.frontmatter_entry(result["content"], result["path"])[0] or "").strip()
                stem = Path(result["path"]).name[:-3]
                if fm_name and fm_name != stem:
                    name_mismatches.append(f"{result['path']}: name '{fm_name}' != stem '{stem}'")
            if name_mismatches:
                dropped.append({"cluster_id": cluster_id, "reason": f"name-stem mismatch: {'; '.join(name_mismatches)}"})
                continue
            # Sibling discriminability: every pair of resulting descriptions must be
            # tellable apart at recall time, or the split adds routing confusion
            # instead of precision (measured 2026-07-18 during the authors'
            # consolidation campaign, a shadow experiment).
            entries = [(r["path"], AUDIT.frontmatter_entry(r["content"], r["path"])[1]) for r in results]
            blurred = [
                f"{a_path} ~ {b_path} (j={AUDIT.description_similarity(a_desc, b_desc):.2f})"
                for i, (a_path, a_desc) in enumerate(entries)
                for b_path, b_desc in entries[i + 1 :]
                if AUDIT.description_similarity(a_desc, b_desc) >= config.SIBLING_DESC_JACCARD
            ]
            # Distractor confusion comes from ALL project siblings, not just the
            # proposal's own results: a new description must also be routable against
            # every existing note it will sit beside in the index (notes this
            # proposal replaces or deletes are excluded; they will not be there).
            replaced = {r["path"] for r in results} | set(deletes)
            for result_path, result_desc in entries:
                for rel, record in records.items():
                    existing_desc = record.get("description")
                    if rel in replaced or not existing_desc:
                        continue
                    similarity = AUDIT.description_similarity(result_desc, existing_desc)
                    if similarity >= config.SIBLING_DESC_JACCARD:
                        blurred.append(f"{result_path} ~ existing {rel} (j={similarity:.2f})")
            if blurred:
                dropped.append(
                    {"cluster_id": cluster_id, "reason": f"sibling descriptions not discriminative: {'; '.join(blurred)}"}
                )
                continue

            source_rels = sorted(set(deletes) | ({survivor["path"]} if survivor["path"] in records else set()))
            sources = [
                {"path": rel, "digest": AUDIT.digest(memory_dir / rel)}
                for rel in source_rels
            ]
            proposals.append(
                {
                    "id": stable_id(project, targets, proposal["action"]),
                    "project": project,
                    "action": proposal["action"],
                    "survivor": survivor["path"],
                    "sources": sources,
                    "results": results,
                    "deletes": sorted(deletes),
                    "justification": proposal["justification"],
                    "sensitive": False,
                }
            )
    proposals.sort(key=lambda proposal: (proposal["project"], proposal["id"]))
    return proposals, dropped


def proposal_file_diffs(
    memory_dir: Path, proposal: dict[str, Any], retarget: dict[str, Any]
) -> list[tuple[str, str, list[str], list[str]]]:
    """(fromdesc, todesc, before_lines, after_lines) per file a proposal changes.

    `retarget` = (by_canon, token_to_rel, delete_to_stem) so the after-content matches
    what apply writes post-retargeting (inbound-wikilink retargeting). Line lists
    carry no trailing newlines."""
    by_canon, token_to_rel, delete_to_stem = retarget
    diffs: list[tuple[str, str, list[str], list[str]]] = []
    for result in proposal["results"]:
        path = memory_dir / result["path"]
        before = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []
        after = AUDIT.rewrite_links_to_deleted(result["content"], by_canon, token_to_rel, delete_to_stem).splitlines()
        diffs.append((result["path"], result["path"], before, after))
    for rel in proposal["deletes"]:
        path = memory_dir / rel
        before = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []
        diffs.append((rel, "(deleted)", before, []))
    return diffs


def _unified_from_file_diffs(file_diffs: list[tuple[str, str, list[str], list[str]]]) -> str:
    chunks: list[str] = []
    for fromdesc, todesc, before, after in file_diffs:
        chunks.extend(
            difflib.unified_diff(
                [line + "\n" for line in before], [line + "\n" for line in after],
                fromfile=f"a/{fromdesc}", tofile=f"b/{todesc}",
            )
        )
    return "".join(chunks)


def unified_diff(memory_dir: Path, proposal: dict[str, Any], retarget: dict[str, Any]) -> str:
    """Unified diff text for the per-proposal .diff artifact (side-by-side lives in the HTML)."""
    return _unified_from_file_diffs(proposal_file_diffs(memory_dir, proposal, retarget))


def _sxs_rows(before: list[str], after: list[str]) -> list[tuple[str, int | None, str, int | None, str]]:
    """Side-by-side rows (kind, left_lineno, left_line, right_lineno, right_line) aligned by
    a line-level SequenceMatcher. kind is equal|change|gap; None line numbers are filler.
    Long unchanged runs collapse to a marker so a small edit does not render the whole note."""
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    rows: list[tuple[str, int | None, str, int | None, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            span = list(range(i2 - i1))
            keep = span if len(span) <= 10 else span[:3] + [-1] + span[-3:]
            for offset in keep:
                if offset == -1:
                    rows.append(("gap", None, f"… {i2 - i1 - 6} unchanged lines …", None, ""))
                else:
                    rows.append(("equal", i1 + offset + 1, before[i1 + offset], j1 + offset + 1, after[j1 + offset]))
        else:  # replace / delete / insert all render as a change (removed left, added right)
            for offset in range(max(i2 - i1, j2 - j1)):
                left_n = i1 + offset + 1 if i1 + offset < i2 else None
                right_n = j1 + offset + 1 if j1 + offset < j2 else None
                rows.append(
                    ("change", left_n, before[i1 + offset] if left_n else "", right_n, after[j1 + offset] if right_n else "")
                )
    return rows


def _sxs_table(before: list[str], after: list[str]) -> str:
    body = []
    for kind, left_n, left, right_n, right in _sxs_rows(before, after):
        if kind == "gap":
            lcls = rcls = "gap"
        elif kind == "equal":
            lcls = rcls = "ctx"
        else:  # change: color a present cell, leave the empty side neutral
            lcls = "del" if left_n is not None else "nil"
            rcls = "add" if right_n is not None else "nil"
        ln = "" if left_n is None else str(left_n)
        rn = "" if right_n is None else str(right_n)
        body.append(
            f'<tr><td class="n">{ln}</td><td class="{lcls}">{html.escape(left) or "&nbsp;"}</td>'
            f'<td class="n">{rn}</td><td class="{rcls}">{html.escape(right) or "&nbsp;"}</td></tr>'
        )
    return f'<table class="diff"><tbody>{"".join(body)}</tbody></table>'


def write_preview_html(
    out: Path,
    manifest: dict[str, Any],
    report: dict[str, Any],
    file_diffs_by_id: dict[str, list[tuple[str, str, list[str], list[str]]]],
    index_diffs_by_project: dict[str, tuple[list[str], list[str]]] | None = None,
) -> Path:
    """Self-contained HTML preview: a readable, word-wrapping side-by-side diff per file
    (left = current note, right = proposed), openable in a browser pre-apply."""
    proposals = manifest.get("proposals", [])
    token = html.escape(str(manifest.get("id", "")))
    sections = []
    for proposal in proposals:
        action = html.escape(proposal.get("action", ""))
        blocks = []
        for fromdesc, todesc, before, after in file_diffs_by_id.get(proposal["id"], []):
            label = html.escape(fromdesc) if fromdesc == todesc else f"{html.escape(fromdesc)} → {html.escape(todesc)}"
            blocks.append(f'<h3>{label}</h3><div class="scroll">{_sxs_table(before, after)}</div>')
        body = "".join(blocks) or "<p class='muted'>(no file changes)</p>"
        sections.append(
            f'<section class="card"><h2><span class="badge {action}">{action}</span> '
            f'{html.escape(proposal.get("project", ""))}</h2>'
            f'<p class="just">{html.escape(proposal.get("justification", ""))}</p>{body}</section>'
        )
    # The MEMORY.md changes are part of what the operator approves: show the exact
    # reconcile+refresh projection apply will perform (all-approved).
    for project, (before, after) in sorted((index_diffs_by_project or {}).items()):
        sections.append(
            f'<section class="card"><h2><span class="badge index">index</span> '
            f'{html.escape(project)} MEMORY.md</h2>'
            f'<p class="just">Anticipated index change (all-approved projection): deleted notes drop off, '
            f'new notes append, rewritten notes get their routing hook refreshed.</p>'
            f'<div class="scroll">{_sxs_table(before, after)}</div></section>'
        )
    counts = (
        f'{len(proposals)} proposal &middot; {len(report.get("dropped", []))} dropped &middot; '
        f'{len(report.get("deferred", []))} deferred &middot; {len(report.get("manual_review", []))} manual-review'
    )
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Memory dream pass preview</title>
<style>
 :root{{--bg:#faf9f7;--surface:#fff;--ink:#1c1917;--muted:#78716c;--line:#e7e5e4;
  --accent:#7c3aed;--add-bg:#e8f6ec;--add-ink:#0f6b34;--del-bg:#fbeced;--del-ink:#a11a29;
  --serif:ui-serif,Georgia,"Times New Roman",serif;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}}
 *{{box-sizing:border-box}}
 body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}}
 .page{{max-width:1180px;margin:0 auto;padding:2.5rem 1.5rem 5rem}}
 .eyebrow{{font-size:.72rem;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);font-weight:700;margin:0 0 .4rem}}
 h1{{font-family:var(--serif);font-size:2rem;line-height:1.15;margin:0 0 .3rem;letter-spacing:-.01em}}
 .sub{{color:var(--muted);margin:0 0 1.5rem}}
 .token{{background:#fdf6e3;border:1px solid #e7d9a8;border-radius:10px;padding:.8rem 1rem;margin:1.2rem 0}}
 .token code{{font-family:var(--mono);font-weight:700;background:#f4ead0;padding:.15rem .4rem;border-radius:5px}}
 .legend{{color:var(--muted);font-size:.85rem;margin:.4rem 0 0}}
 .legend .chip{{padding:.05rem .4rem;border-radius:4px;font-weight:600}}
 .card{{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:1.3rem 1.4rem;margin:1.5rem 0;box-shadow:0 1px 2px rgba(0,0,0,.03)}}
 h2{{font-family:var(--serif);font-size:1.25rem;margin:0 0 .3rem}}
 h3{{font-size:.85rem;font-family:var(--mono);color:var(--muted);margin:1.1rem 0 .4rem;font-weight:600}}
 .just{{color:#44403c;margin:.1rem 0 .4rem}} .muted{{color:var(--muted)}}
 .scroll{{border:1px solid var(--line);border-radius:10px;overflow:hidden}}
 table.diff{{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:12.5px;line-height:1.5;table-layout:fixed}}
 table.diff td{{padding:.05rem .6rem;vertical-align:top;white-space:pre-wrap;overflow-wrap:break-word;word-break:break-word}}
 table.diff td.n{{width:2.8em;text-align:right;color:#bbb;background:#faf9f7;user-select:none;font-size:11px;border-right:1px solid var(--line)}}
 table.diff td.ctx,table.diff td.nil,table.diff td.gap{{background:var(--surface)}}
 table.diff td.del{{background:var(--del-bg);color:var(--del-ink)}}
 table.diff td.add{{background:var(--add-bg);color:var(--add-ink)}}
 table.diff td.gap{{color:var(--muted);font-style:italic;text-align:center}}
 .badge{{display:inline-block;padding:.1rem .55rem;border-radius:6px;color:#fff;font-size:.72rem;font-weight:700;text-transform:uppercase;vertical-align:middle;margin-right:.3rem}}
 .compress{{background:#2d7d46}} .period-close{{background:#a03030}} .merge{{background:#3060a0}} .leave{{background:#78716c}}
 .split{{background:#b45309}} .redescribe{{background:#0e7490}} .index{{background:#57534e}}
</style></head><body><div class="page">
<p class="eyebrow">Memory dream pass</p>
<h1>Consolidation preview</h1>
<p class="sub">{counts}</p>
<div class="token">To approve, reply in chat with a message containing this token: <code>{token}</code>. Nothing is written until you approve.</div>
<p class="legend">Left = current note, right = proposed.
 <span class="chip" style="background:var(--del-bg);color:var(--del-ink)">removed</span>
 <span class="chip" style="background:var(--add-bg);color:var(--add-ink)">added</span></p>
{''.join(sections)}
<p class="muted" style="margin-top:2rem;font-size:.85rem">Read-only preview. Approval applies to live memory; deletions are recoverable — from the patch set's snapshot backup by default, or from mirror git history when a mirror is configured.</p>
</div></body></html>"""
    path = out / "preview.html"
    path.write_text(page, encoding="utf-8")
    return path


def project_retarget_context(memory_dir: Path, project_proposals: list[dict[str, Any]]) -> tuple[Any, Any, dict[str, str]]:
    """(by_canon, token_to_rel, delete_to_stem) for a project's approved proposals."""
    records = AUDIT.scan_project_notes(memory_dir)
    by_canon, token_to_rel = AUDIT.link_resolver(records)
    delete_to_stem: dict[str, str] = {}
    for proposal in project_proposals:
        survivor = proposal.get("survivor")
        if survivor:
            for rel in proposal.get("deletes", []):
                delete_to_stem[rel] = Path(survivor).name[:-3]
    return by_canon, token_to_rel, delete_to_stem


def run_build(args: argparse.Namespace) -> int:
    live_root = Path(args.live_root).expanduser()
    mirror_root = Path(args.mirror_root).expanduser() if args.mirror_root else None
    try:
        plan = json.loads(Path(args.plan).expanduser().read_text(encoding="utf-8"))
        drafts_raw = json.loads(Path(args.drafts).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"memory-dream: cannot read plan or drafts: {error}", file=sys.stderr)
        return 2
    drafts = drafts_raw.get("clusters", drafts_raw) if isinstance(drafts_raw, dict) else {}
    if isinstance(drafts, list):
        drafts = {entry["cluster_id"]: entry.get("proposals", []) for entry in drafts}
    # Verification-coverage gate: refuse to assemble drafts the mandatory
    # verification stages never touched. Coverage is per drafted cluster; only
    # clean/fixed proceed.
    try:
        findings = json.loads(Path(args.findings).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(
            f"memory-dream: cannot read findings file (--findings): {error}; "
            "run the fidelity/repo-grounding/quality-panel stages and persist their findings first",
            file=sys.stderr,
        )
        return 2
    findings_clusters = findings.get("clusters") if isinstance(findings, dict) else None
    if not isinstance(findings_clusters, dict):
        print(
            'memory-dream: findings file must be {"clusters": {"<cluster_id>": '
            '{"status": ...}}}; refusing',
            file=sys.stderr,
        )
        return 2
    uncovered = sorted(
        cid for cid in drafts
        if not isinstance(findings_clusters.get(cid), dict)
        or findings_clusters[cid].get("status") not in ("clean", "fixed")
    )
    if uncovered:
        print(
            "memory-dream: verification-coverage gate: drafted cluster(s) without a "
            f"clean/fixed verification entry: {', '.join(uncovered)}. The fidelity "
            "verification, repo-grounding, and quality-panel stages are mandatory; "
            "verify these clusters (or drop them from drafts) and rerun",
            file=sys.stderr,
        )
        return 2
    # Content-binding: a clean/fixed status alone only proves SOME payload for this
    # cluster id was verified, not that it was THIS payload. Every drafted cluster's
    # findings entry must carry drafts_digest = AUDIT.content_id(proposals) as
    # recorded by the fidelity/repo-grounding/quality-panel stages; build recomputes
    # it and refuses on mismatch, so an edited/replaced draft or a stale findings
    # entry from an earlier redraft of the same cluster id can never ride through
    # unverified.
    missing_digest = sorted(
        cid for cid in drafts
        if not isinstance(findings_clusters[cid].get("drafts_digest"), str)
        or not findings_clusters[cid]["drafts_digest"]
    )
    if missing_digest:
        print(
            "memory-dream: verification-coverage gate: drafted cluster(s) missing "
            f"drafts_digest: {', '.join(missing_digest)}. The fidelity/repo-grounding/"
            "quality-panel stages must record AUDIT.content_id(proposals) for the "
            "cluster they verified so build can bind coverage to the exact bytes "
            "being assembled; this is a hard requirement, not optional metadata",
            file=sys.stderr,
        )
        return 2
    stale_digest = sorted(
        cid for cid in drafts
        if findings_clusters[cid]["drafts_digest"] != AUDIT.content_id(drafts[cid])
    )
    if stale_digest:
        print(
            "memory-dream: verification-coverage gate: drafted cluster(s) verified "
            f"against stale content: {', '.join(stale_digest)}. The proposals for "
            "these clusters changed since the fidelity/repo-grounding/quality-panel "
            "stages ran; re-verify with the current drafts and rerun",
            file=sys.stderr,
        )
        return 2
    # Plan-source drift gate: the content-binding check above only proves the
    # findings stages verified the CURRENT drafts payload; it says nothing
    # about whether the source notes those drafts were built FROM still hold
    # the bytes the plan captured. assemble_proposals reads live donor files
    # (not the plan's snapshot), and the post-build digest recorded on each
    # proposal's `sources` is only computed AFTER this read — so a source note
    # edited, or deleted by a merge/period-close, between plan/verification and
    # build would silently ride through: the digest always "matches" whatever
    # is on disk by the time it's taken. Recompute each drafted cluster's note
    # bodies from the live files, the same way the plan captured them
    # (frontmatter-stripped body text via AUDIT.parse_frontmatter), and refuse
    # on any mismatch against the body plan.json recorded verbatim.
    plan_clusters_by_id = {
        cluster["cluster_id"]: cluster
        for cluster in plan.get("clusters", [])
        if isinstance(cluster, dict) and cluster.get("cluster_id")
    }
    live_for_drift = AUDIT.project_dirs(live_root, live=True)
    drifted_clusters: set[str] = set()
    for cid in drafts:
        cluster = plan_clusters_by_id.get(cid)
        if cluster is None:
            drifted_clusters.add(cid)
            continue
        memory_dir = live_for_drift.get(cluster.get("project"))
        if memory_dir is None:
            drifted_clusters.add(cid)
            continue
        for planned_note in cluster.get("notes", []):
            rel = planned_note.get("path")
            planned_digest = _note_body_digest(str(planned_note.get("body", "")))
            target = memory_dir / rel if rel and confined(memory_dir, rel) else None
            if target is None or not target.is_file() or target.is_symlink():
                drifted_clusters.add(cid)
                break
            live_text = target.read_text(encoding="utf-8", errors="replace")
            _metadata, _error, live_body, _raw = AUDIT.parse_frontmatter(live_text)
            if _note_body_digest(live_body) != planned_digest:
                drifted_clusters.add(cid)
                break
    if drifted_clusters:
        print(
            "memory-dream: source notes changed since plan; re-run "
            f"plan+verification (cluster(s): {', '.join(sorted(drifted_clusters))})",
            file=sys.stderr,
        )
        return 2
    proposals, dropped = assemble_proposals(plan.get("clusters", []), drafts, live_root, str(args.stamp))

    out = Path(args.out).expanduser() if args.out else _fresh_pass_dir()
    out.mkdir(parents=True, exist_ok=True)
    # Owner-only: the patch-set dir is the durable body-bearing surface. Restrict
    # both it and its parent so bodies never sit world-readable. The
    # orchestrating command's own transient plan.json/drafts.json also carry
    # bodies; keep those under the session scratch dir (config.scratch_dir())
    # and never persist them.
    compat.restrict_permissions(out)
    compat.restrict_permissions(out.parent)
    live = AUDIT.project_dirs(live_root, live=True)
    retarget_preview: dict[str, list[str]] = {}
    index_preview: list[str] = []
    file_diffs_by_id: dict[str, list[tuple[str, str, list[str], list[str]]]] = {}
    index_diffs_by_project: dict[str, tuple[list[str], list[str]]] = {}
    for project in sorted({proposal["project"] for proposal in proposals}):
        memory_dir = live.get(project)
        if memory_dir is None:
            continue
        project_proposals = [p for p in proposals if p["project"] == project]
        retarget = project_retarget_context(memory_dir, project_proposals)
        for proposal in project_proposals:
            if proposal["action"] == "leave":
                continue
            file_diffs = proposal_file_diffs(memory_dir, proposal, retarget)
            file_diffs_by_id[proposal["id"]] = file_diffs
            (out / f"{proposal['id']}.diff").write_text(_unified_from_file_diffs(file_diffs))
        # Anticipated inbound-link retargets (all-approved projection) for the preview.
        edits = anticipated_retargets(memory_dir, project_proposals)
        if edits:
            retarget_preview[project] = sorted(edits)
            chunks: list[str] = []
            for rel in sorted(edits):
                before = (memory_dir / rel).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
                after = edits[rel].splitlines(keepends=True)
                chunks.extend(difflib.unified_diff(before, after, fromfile=f"a/{rel}", tofile=f"b/{rel}"))
            (out / f"retargets-{project}.diff").write_text("".join(chunks))
        # Anticipated MEMORY.md reconciliation, so index byte changes are in the preview.
        index_texts = anticipated_index_texts(memory_dir, project_proposals)
        if index_texts:
            current, reconciled = index_texts
            index_preview.append(project)
            (out / f"index-{project}.diff").write_text(
                "".join(
                    difflib.unified_diff(
                        current.splitlines(keepends=True), reconciled.splitlines(keepends=True),
                        fromfile="a/MEMORY.md", tofile="b/MEMORY.md",
                    )
                )
            )
            index_diffs_by_project[project] = (current.splitlines(), reconciled.splitlines())
    # A pass must not silently spend index budget past Claude Code's load cap:
    # entries appended beyond it are routing-invisible (measured 2026-07-19
    # during the authors' consolidation campaign, found via an A/B miss). This
    # is a REFUSAL, not a WARN, when the patch set GROWS an already-over-cap
    # index (measured 2026-07-31: a split-heavy pass grew two over-cap indexes
    # by 28 lines; the correct remedy is the archive tier, run first).
    index_over_cap = []
    index_growth_violations = []
    for project, (current_lines, reconciled_lines) in index_diffs_by_project.items():
        current_bytes = len(("\n".join(current_lines) + "\n").encode("utf-8"))
        reconciled_text = "\n".join(reconciled_lines) + "\n"
        raw_bytes = len(reconciled_text.encode("utf-8"))
        if raw_bytes > config.INDEX_LOAD_MAX_BYTES or len(reconciled_lines) > config.INDEX_LOAD_MAX_LINES:
            index_over_cap.append({"project": project, "bytes": raw_bytes, "lines": len(reconciled_lines)})
            print(
                f"memory-dream: WARN anticipated index for {project} exceeds the load cap "
                f"({raw_bytes}B/{len(reconciled_lines)} lines vs {config.INDEX_LOAD_MAX_BYTES}B/"
                f"{config.INDEX_LOAD_MAX_LINES}); appended entries past the cap will be routing-invisible",
                file=sys.stderr,
            )
            if raw_bytes > current_bytes or len(reconciled_lines) > len(current_lines):
                index_growth_violations.append(project)
    if index_growth_violations and not args.allow_index_growth:
        print(
            "memory-dream: refusing to build: patch set GROWS the over-cap "
            f"index of: {', '.join(sorted(index_growth_violations))}. Run the archive "
            "tier first (the `archive` subcommand) to demote cold entries, or pass "
            "--allow-index-growth to consciously accept routing-invisible entries",
            file=sys.stderr,
        )
        return 2
    # Build must never park a token apply will refuse: run apply's manifest
    # integrity condition here and fail loudly (measured 2026-07-18 during the
    # authors' consolidation campaign: a leave-id collision was only caught
    # post-approval, wasting an operator round-trip).
    proposal_ids = [proposal["id"] for proposal in proposals]
    if len(proposal_ids) != len(set(proposal_ids)):
        print("memory-dream: duplicate proposal ids in built manifest, refusing to write", file=sys.stderr)
        return 2
    manifest = {
        "schema_version": 1,
        # Content-bound: any change to proposal content, not just the id set, yields a
        # new patch_set_id, so a stale selection cannot ride onto a redrafted patch set.
        # Apply recomputes this from proposals and refuses on mismatch.
        "id": AUDIT.content_id(proposals),
        "created_at_line": args.created_at_line,
        "live_root": str(live_root),
        "mirror_root": str(mirror_root) if mirror_root else None,
        "proposals": proposals,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    # Plain-markdown copies of every result, one file each, so gate agents
    # (checkers, quality-panel lenses) read exactly the files they are assigned
    # instead of the whole JSON-escaped manifest. Token shaping only: content is
    # byte-identical to manifest results[]; manifest.json stays authoritative.
    results_dir = out / "results"
    results_dir.mkdir(exist_ok=True)
    for proposal in proposals:
        for result in proposal.get("results", []):
            (results_dir / f"{proposal['project']}__{result['path']}").write_text(result["content"])
    # Filename-casing drift lint (measured 2026-07-31 during the authors'
    # consolidation campaign: drafters silently renamed kebab-case survivors to
    # snake_case, mixing conventions inside one project store). Flag NEW
    # filenames that deviate from the store's dominant separator; advisory
    # (report + WARN), never a drop — mixed stores have no clean answer.
    casing_drift = []
    for project in sorted({proposal["project"] for proposal in proposals}):
        memory_dir = live.get(project)
        if memory_dir is None:
            continue
        existing = [p.stem for p in memory_dir.glob("*.md") if p.name != "MEMORY.md"]
        hyphen = sum(1 for stem in existing if "-" in stem)
        underscore = sum(1 for stem in existing if "_" in stem)
        if hyphen == underscore:
            continue
        minority = "_" if hyphen > underscore else "-"
        dominant = "-" if minority == "_" else "_"
        for proposal in proposals:
            if proposal["project"] != project:
                continue
            for result in proposal.get("results", []):
                stem = Path(result["path"]).name[:-3]
                if (memory_dir / result["path"]).exists():
                    continue  # existing filenames keep their casing; only new files drift
                if minority in stem and dominant not in stem:
                    casing_drift.append({"project": project, "path": result["path"], "dominant": dominant})
                    print(
                        f"memory-dream: WARN casing drift: new file {project}/{result['path']} "
                        f"uses '{minority}' but the store's dominant separator is '{dominant}'",
                        file=sys.stderr,
                    )
    # A redescribe's operator-approved purpose is updating the routing hook, but
    # refresh_index_lines only ever rewrites a line whose SOLE target is the note:
    # a hand-packed multi-note line is deliberately preserved. That makes a
    # redescribe routing-inert at the index layer (measured during the authors'
    # consolidation campaign: a redescribe scored flat in its A/B because its
    # target sat in a packed line). WARN so the pass surfaces the gap before a
    # token is parked.
    redescribe_index_warnings = []
    for proposal in proposals:
        if proposal.get("action") != "redescribe":
            continue
        index_file = live_root / proposal["project"] / "memory" / "MEMORY.md"
        if not index_file.is_file():
            continue
        rel = proposal["results"][0]["path"]
        sole = packed = False
        for line in index_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line_targets, _escaping = AUDIT.index_targets(line)
            if rel in line_targets:
                if len(line_targets) == 1:
                    sole = True
                else:
                    packed = True
        if sole:
            continue
        kind = "packed-line-only" if packed else "not-indexed"
        redescribe_index_warnings.append({"project": proposal["project"], "path": rel, "kind": kind})
        print(
            f"memory-dream: WARN redescribe {proposal['project']}/{rel}: index entry is "
            f"{kind}, so the new description will NOT surface in index routing "
            f"(only sessions that open the note see it)",
            file=sys.stderr,
        )
    report = {
        "schema_version": 1,
        "proposals": len(proposals),
        "dropped": dropped,
        "deferred": plan.get("deferred", []),
        "manual_review": plan.get("manual_review", []),
        "anticipated_retargets": retarget_preview,
        "anticipated_index_changes": sorted(index_preview),
        "index_over_cap": index_over_cap,
        "casing_drift": casing_drift,
        "redescribe_index_warnings": redescribe_index_warnings,
    }
    (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    preview = write_preview_html(out, manifest, report, file_diffs_by_id, index_diffs_by_project)
    print(f"memory-dream: {len(proposals)} proposal(s), {len(dropped)} dropped -> {out}")
    print(f"preview: {preview}")
    return 0


# --- Archive tier (deterministic, index-only) --------------------------------

ARCHIVE_POINTER = "- Older or resolved entries are archived in MEMORY-archive.md (not auto-loaded); grep it when a topic is missing here."


def parse_index_entries(index_text: str) -> list[list[str]]:
    """Split MEMORY.md into entry blocks: a '- [' line plus its continuation lines."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in index_text.splitlines():
        if line.lstrip().startswith("- ["):
            if current:
                blocks.append(current)
            current = [line]
        elif current is not None and line.strip() and not line.startswith("#"):
            current.append(line)
        else:
            if current:
                blocks.append(current)
                current = None
    if current:
        blocks.append(current)
    return blocks


def entry_latest_date(block: list[str], memory_dir: Path) -> str | None:
    """Latest YYYY-MM-DD date in the entry text plus the head of its note body."""
    text = "\n".join(block)
    match = re.search(r"\(([^)]+\.md)\)", block[0])
    if match:
        note = memory_dir / match.group(1)
        if note.is_file():
            text += "\n" + note.read_text(encoding="utf-8", errors="replace")[: config.ENTRY_DATE_SCAN_BYTES]
    dates = re.findall(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    return max(dates) if dates else None


def run_archive(args: argparse.Namespace) -> int:
    """Build an index-only archive patch set: demote cold entries to MEMORY-archive.md.

    Entirely deterministic (no drafting): an entry is a candidate when every date it
    or its note's head mentions is on or before --cutoff. Undated entries are KEPT
    hot (cold cannot be proven). The date is only a candidate filter; content is the
    real criterion, so repeatable --keep patterns retain content-hot candidates
    (standing conventions, traps, negative results, live ops refs) and every keep is
    recorded in the report for audit. The proposal moves the exact entry blocks; note
    bodies are never touched, so the operation is fully reversible by moving the
    lines back. Gated by the same operator preview and approval flow (consent trace
    by default) as every other patch set.
    """
    live_root = Path(args.live_root).expanduser()
    mirror_root = Path(args.mirror_root).expanduser() if args.mirror_root else None
    memory_dir = live_root / args.project / "memory"
    index_path = memory_dir / "MEMORY.md"
    if not index_path.is_file():
        print(f"memory-dream: no MEMORY.md for {args.project}", file=sys.stderr)
        return 2
    index_text = index_path.read_text(encoding="utf-8")
    blocks = parse_index_entries(index_text)
    keeps = [pattern.lower() for pattern in getattr(args, "keep", None) or []]
    candidates: list[list[str]] = []
    kept: list[dict[str, str]] = []
    for block in blocks:
        latest = entry_latest_date(block, memory_dir)
        if latest is None or latest > args.cutoff:
            continue
        text = "\n".join(block).lower()
        match = next((pattern for pattern in keeps if pattern in text), None)
        if match is not None:
            kept.append({"entry": block[0], "keep": match})
            continue
        candidates.append(block)
    if not candidates:
        print("memory-dream: no archive candidates at this cutoff; nothing to build")
        return 0
    entry_texts = ["\n".join(block) for block in candidates]
    freed = sum(len(t.encode("utf-8")) + 1 for t in entry_texts)
    proposal = {
        "id": stable_id(args.project, sorted(b[0] for b in candidates), "archive"),
        "project": args.project,
        "action": "archive",
        "justification": f"demote {len(candidates)} cold index entries (latest date <= {args.cutoff}) to MEMORY-archive.md, freeing ~{freed}B of hot-index budget; bodies untouched, fully reversible",
        "sources": [{"path": "MEMORY.md", "digest": AUDIT.digest(index_path)}],
        "results": [],
        "deletes": [],
        "archive_entries": entry_texts,
        "sensitive": False,
        "survivor": None,
    }
    out = Path(args.out).expanduser() if args.out else _fresh_pass_dir()
    out.mkdir(parents=True, exist_ok=True)
    compat.restrict_permissions(out)
    if out.parent.exists():
        compat.restrict_permissions(out.parent)
    manifest = {
        "schema_version": 1,
        "id": AUDIT.content_id([proposal]),
        "created_at_line": args.created_at_line,
        "live_root": str(live_root),
        "mirror_root": str(mirror_root) if mirror_root else None,
        "proposals": [proposal],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    remaining = [ln for ln in index_text.splitlines() if all(ln not in t.splitlines() for t in entry_texts)]
    new_index = "\n".join(remaining).rstrip() + "\n"
    if ARCHIVE_POINTER not in new_index:
        new_index += ARCHIVE_POINTER + "\n"
    diff_text = "".join(
        difflib.unified_diff(
            index_text.splitlines(keepends=True), new_index.splitlines(keepends=True),
            fromfile="a/MEMORY.md", tofile="b/MEMORY.md",
        )
    )
    (out / f"index-{args.project}.diff").write_text(diff_text)
    (out / "archive-additions.txt").write_text("\n".join(entry_texts) + "\n")
    report = {
        "schema_version": 1,
        "proposals": 1,
        "dropped": [],
        "deferred": [],
        "manual_review": [],
        "anticipated_retargets": [],
        "anticipated_index_changes": [args.project],
        "index_over_cap": [],
        "archive": {"project": args.project, "entries": len(candidates), "bytes_freed": freed,
                    "post_archive_index_bytes": len(new_index.encode("utf-8")), "kept": kept},
    }
    (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    preview = write_preview_html(out, manifest, report, {}, {args.project: (index_text.splitlines(), new_index.splitlines())})
    print(f"memory-dream: archive patch set, {len(candidates)} entries, ~{freed}B freed -> {out}")
    print(f"preview: {preview}")
    return 0


def _fresh_pass_dir() -> Path:
    """A fresh, not-yet-existing patch-set directory under the configured pass root."""
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S%f")
    return config.pass_root() / stamp


# --- CLI registration ---------------------------------------------------------


def add_parsers(subparsers) -> None:
    """Register `plan`, `build`, and `archive` on the shared memory-dream subparsers."""
    _add_plan_parser(subparsers)
    _add_build_parser(subparsers)
    _add_archive_parser(subparsers)


def _add_plan_parser(subparsers) -> None:
    p = subparsers.add_parser("plan", help="cluster flagged notes for drafting (deterministic, no model calls)")
    config.add_root_args(p)
    p.add_argument("--now", type=AUDIT.date_value, default=None)
    p.add_argument(
        "--shards-dir",
        default=None,
        help="also write one per-cluster shard JSON here, so drafters read only "
             "their own cluster (default: a 'shards' directory under the "
             "session scratch dir, config.scratch_dir())",
    )
    p.add_argument("--max-clusters", type=int, default=config.TRIAGE_MAX_CLUSTERS)
    p.add_argument("--max-notes", type=int, default=config.TRIAGE_MAX_NOTES_PER_CLUSTER)
    p.set_defaults(func=run_plan)


def _add_build_parser(subparsers) -> None:
    p = subparsers.add_parser("build", help="validate drafts and assemble the patch set")
    config.add_root_args(p)
    p.add_argument("--plan", required=True)
    p.add_argument("--drafts", required=True)
    p.add_argument(
        "--out",
        default=None,
        help="patch-set output directory (default: a fresh directory under "
             "the configured pass root, config.pass_root())",
    )
    p.add_argument("--created-at-line", type=int, required=True)
    # Enforcement, not doctrine (measured 2026-07-31 during the authors'
    # consolidation campaign): an abbreviated pass that skipped the mandatory
    # fidelity/repo-grounding/quality-panel stages assembled 26 blocking defects
    # (lost fix commands, lost open follow-ups, two fabrications) into a
    # previewable patch set. The stages were documented as MANDATORY; nothing
    # enforced them.
    p.add_argument(
        "--findings", required=True,
        help='per-pass findings file the verification stages persist: JSON '
             '{"clusters": {"<cluster_id>": {"status": "clean"|"fixed"}}}; build '
             "refuses unless every drafted cluster is covered",
    )
    p.add_argument(
        "--stamp", required=True, type=AUDIT.date_value,
        help="YYYY-MM-DD revalidation date stamped into decay frontmatter "
             "(extracts get confidence/maturity/last_validated; decay-carrying "
             "survivors get last_validated bumped)",
    )
    p.add_argument(
        "--allow-index-growth", action="store_true",
        help="override the over-cap index-growth refusal (default: refuse and "
             "point at the archive tier; growing an over-cap index appends "
             "routing-invisible entries)",
    )
    p.set_defaults(func=run_build)


def _add_archive_parser(subparsers) -> None:
    p = subparsers.add_parser("archive", help="build an index-only archive patch set (demote cold entries)")
    config.add_root_args(p)
    p.add_argument("--project", required=True)
    p.add_argument("--cutoff", required=True, help="YYYY-MM-DD; entries whose latest date is on/before this are demoted")
    p.add_argument("--keep", action="append", default=[],
                    help="repeatable case-insensitive substring; matching candidates stay hot (content-hot despite age), recorded in the report")
    p.add_argument(
        "--out",
        default=None,
        help="patch-set output directory (default: a fresh directory under "
             "the configured pass root, config.pass_root())",
    )
    p.add_argument("--created-at-line", type=int, required=True)
    p.set_defaults(func=run_archive)
