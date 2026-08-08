"""Recall-regression eval for Claude Code auto-memory: the feedback metric a
consolidation pass needs before it can claim it helped.

Claude Code recall is 100% agentic and routed by MEMORY.md's one-line
descriptions, so "did memory get better" is measurable: freeze a synthetic
question suite over the corpus, have a fixed judge route each question using
ONLY the index descriptions, and score routing accuracy before and after a
consolidation pass.

Deterministic plumbing lives here (sampling, suite freezing/validation, judge
input assembly, scoring, run comparison); the two LLM stages (question
writing, routing judgment) are dispatched as subagents by the surrounding
`eval` command and validated here.

Ground truth is CONTENT-ANCHORED, not path-anchored: every question carries a
verbatim answer_snippet that freeze verifies against the source note
byte-for-byte (fabricated questions are rejected deterministically), and
score counts a routed note correct when its CURRENT body contains the
snippet. A suite therefore survives split/merge/period-close, which is
exactly when we need to measure.

Shares its structural helpers with `audit.py`; every root and threshold is
config- or flag-overridable; this module carries no mirror/git assumptions
of its own.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from memory_dream import audit as AUDIT
from memory_dream import config

SUITE_SCHEMA_VERSION = 1
ROUTES_SCHEMA_VERSION = 1
ARCHETYPES = {"direct", "multihop", "distractor", "unanswerable"}
# Judge-noise band from published LLM-judge evals (Mem0 reports ~1pt judge variance);
# a same-suite delta inside this band is noise, not signal.
NOISE_BAND_POINTS = 2.0
# A suite where more than this fraction of questions lost their anchor (snippet no
# longer found anywhere in the project) needs regeneration, not interpretation.
BROKEN_SUITE_FRACTION = 0.2
SNIPPET_MIN_CHARS = 20
SNIPPET_MAX_CHARS = 300
BODY_SAMPLE_CAP = 6000  # bytes of body handed to a question writer per note


def normalize(text: str) -> str:
    """Whitespace-collapsed, case-folded form used for all snippet matching."""
    return " ".join(text.split()).casefold()


def question_id(project: str, question: str, snippet: str) -> str:
    key = f"{project}|{normalize(question)}|{normalize(snippet)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def sensitive_note(rel: str, content: str) -> bool:
    """Never hand a sensitive-flagged note to a question writer (the same signals
    a consolidation pass uses to drop clusters to manual review)."""
    name = Path(rel).name
    if AUDIT.SENSITIVE_FILENAME_RE.search(name):
        return True
    if any(re.search(pattern, name) for pattern in config.SENSITIVE_PATTERNS_EXTRA):
        return True
    return any(pattern.search(content) for pattern in AUDIT.CONTENT_SIGNATURES)


# --- sample ------------------------------------------------------------------


def run_sample(args: argparse.Namespace) -> int:
    """Emit note bodies for the question-writer subagents: per project, the most
    recently modified substantial notes (recency is the best proxy for what a
    future session will actually ask about).

    The JSON sample goes to stdout; a per-project coverage summary (how many
    notes were kept, and how many were dropped for being under ``--min-bytes``
    or sensitive) goes to stderr, so a project silently contributing zero
    notes is always visible rather than looking like it has no memory.
    """
    live_root = Path(args.live_root).expanduser()
    live = AUDIT.project_dirs(live_root, live=True)
    notes: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    for project in sorted(live):
        memory_dir = live[project]
        if memory_dir.is_symlink() or memory_dir.parent.is_symlink():
            coverage.append({"project": project, "kept": 0, "too_small": 0, "sensitive": 0, "skipped": "symlinked"})
            continue
        records = AUDIT.scan_project_notes(memory_dir)
        ranked = sorted(records.items(), key=lambda item: -item[1]["mtime"])
        kept = 0
        too_small = 0
        sensitive = 0
        for rel, record in ranked:
            if kept >= args.per_project:
                break
            if record["body_bytes"] < args.min_bytes:
                too_small += 1
                continue
            content = (memory_dir / rel).read_text(encoding="utf-8", errors="replace")
            if sensitive_note(rel, content):
                sensitive += 1
                continue
            body = record["body"]
            if len(body.encode("utf-8")) > BODY_SAMPLE_CAP:
                body = body.encode("utf-8")[:BODY_SAMPLE_CAP].decode("utf-8", errors="ignore") + "\n[truncated]"
            notes.append(
                {
                    "project": project,
                    "path": rel,
                    "name": record.get("name"),
                    "description": record.get("description"),
                    "body": body,
                }
            )
            kept += 1
        coverage.append({"project": project, "kept": kept, "too_small": too_small, "sensitive": sensitive})
    print(json.dumps({"schema_version": SUITE_SCHEMA_VERSION, "notes": notes}, sort_keys=True))
    # Coverage summary to stderr (never pollutes the machine-readable stdout).
    total_kept = sum(c["kept"] for c in coverage)
    print(
        f"memory-dream eval sample: {total_kept} note(s) from {len(coverage)} project(s) "
        f"(--min-bytes {args.min_bytes}, --per-project {args.per_project})",
        file=sys.stderr,
    )
    for c in coverage:
        if c["kept"] == 0:
            reason = c.get("skipped") or (
                f"all under --min-bytes ({c['too_small']} too small)" if c["too_small"] and not c["sensitive"]
                else f"{c['too_small']} too small, {c['sensitive']} sensitive" if (c["too_small"] or c["sensitive"])
                else "no notes"
            )
            print(f"  WARNING {c['project']}: 0 notes sampled — {reason}", file=sys.stderr)
        elif c["too_small"] or c["sensitive"]:
            print(
                f"  {c['project']}: {c['kept']} kept, {c['too_small']} under --min-bytes, {c['sensitive']} sensitive",
                file=sys.stderr,
            )
    return 0


# --- freeze ------------------------------------------------------------------


def validate_question(entry: dict[str, Any], memory_dirs: dict[str, Path]) -> str | None:
    """Reject a drafted question unless it is structurally sound AND its answer
    snippet verifiably appears in the claimed source note (anti-fabrication)."""
    if not isinstance(entry, dict):
        return "not an object"
    for key in ("project", "question", "archetype"):
        if not isinstance(entry.get(key), str) or not entry[key].strip():
            return f"missing {key}"
    if entry["archetype"] not in ARCHETYPES:
        return f"archetype must be one of {sorted(ARCHETYPES)}"
    memory_dir = memory_dirs.get(entry["project"])
    if memory_dir is None:
        return "unknown project"
    if entry["archetype"] == "unanswerable":
        if entry.get("answer_snippet") or entry.get("source"):
            return "unanswerable questions carry no snippet or source"
        return None
    source = entry.get("source")
    snippet = entry.get("answer_snippet")
    if not isinstance(source, str) or not source.strip():
        return "missing source"
    if not isinstance(snippet, str) or not (SNIPPET_MIN_CHARS <= len(snippet.strip()) <= SNIPPET_MAX_CHARS):
        return f"answer_snippet must be {SNIPPET_MIN_CHARS}-{SNIPPET_MAX_CHARS} chars"
    note_path = AUDIT.confined_path(memory_dir, source)
    if note_path is None or not note_path.is_file():
        return "source note does not exist"
    body = note_path.read_text(encoding="utf-8", errors="replace")
    if normalize(snippet) not in normalize(body):
        return "answer_snippet not found in source note (fabricated?)"
    return None


def run_freeze(args: argparse.Namespace) -> int:
    live_root = Path(args.live_root).expanduser()
    live = AUDIT.project_dirs(live_root, live=True)
    try:
        drafted = json.loads(Path(args.questions).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"memory-dream eval freeze: cannot read questions: {error}", file=sys.stderr)
        return 2
    entries = drafted.get("questions", drafted) if isinstance(drafted, dict) else drafted
    if not isinstance(entries, list):
        print("memory-dream eval freeze: questions must be a list", file=sys.stderr)
        return 2
    questions: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        error = validate_question(entry, live)
        if error:
            dropped.append({"question": str(entry)[:120], "reason": error})
            continue
        qid = question_id(entry["project"], entry["question"], entry.get("answer_snippet", ""))
        if qid in seen:
            dropped.append({"question": entry["question"][:120], "reason": "duplicate"})
            continue
        seen.add(qid)
        questions.append(
            {
                "id": qid,
                "project": entry["project"],
                "archetype": entry["archetype"],
                "question": entry["question"].strip(),
                "source": entry.get("source"),
                "answer_snippet": entry.get("answer_snippet"),
            }
        )
    questions.sort(key=lambda q: (q["project"], q["id"]))
    suite = {
        "schema_version": SUITE_SCHEMA_VERSION,
        "suite_id": hashlib.sha256(
            json.dumps([q["id"] for q in questions]).encode("utf-8")
        ).hexdigest()[:16],
        "live_root": str(live_root),
        "questions": questions,
    }
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    AUDIT.atomic_write(out, (json.dumps(suite, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    print(
        f"memory-dream eval freeze: froze {len(questions)} question(s) "
        f"({len(dropped)} dropped), suite {suite['suite_id']} -> {out}"
    )
    for item in dropped:
        print(f"  dropped: {item['reason']}: {item['question']}", file=sys.stderr)
    return 0


# --- routing-input -----------------------------------------------------------


def run_routing_input(args: argparse.Namespace) -> int:
    """Per project: the index text the judge routes from (descriptions ONLY, the
    same surface a real session sees before deciding what to Read) + questions."""
    live_root = Path(args.live_root).expanduser()
    suite = json.loads(Path(args.suite).expanduser().read_text(encoding="utf-8"))
    live = AUDIT.project_dirs(live_root, live=True)
    by_project: dict[str, list[dict[str, Any]]] = {}
    for question in suite["questions"]:
        by_project.setdefault(question["project"], []).append(question)
    batches = []
    for project in sorted(by_project):
        # The judge sees the LOADED index surface only: stripped and hard-truncated
        # exactly as Claude Code loads it, so routing from entries a real session
        # cannot see is impossible by construction.
        batches.append(
            {
                "project": project,
                "index_text": AUDIT.loaded_index_text(raw_index_text(live.get(project))),
                "questions": [
                    {"id": q["id"], "question": q["question"]} for q in by_project[project]
                ],
            }
        )
    print(json.dumps({"suite_id": suite["suite_id"], "batches": batches}, sort_keys=True))
    return 0


def raw_index_text(memory_dir: Path | None) -> str:
    index_path = memory_dir / "MEMORY.md" if memory_dir else None
    if index_path and index_path.is_file() and not index_path.is_symlink():
        return index_path.read_text(encoding="utf-8", errors="replace")
    return ""


# --- score -------------------------------------------------------------------


def score_question(
    question: dict[str, Any],
    route: dict[str, Any] | None,
    memory_dir: Path | None,
    body_cache: dict[str, str],
    visible: set[str],
    anchored: bool,
) -> dict[str, Any]:
    """One auditable record per question (Mem0 pattern): verdict + score + why.

    Gate order matters: anchor drift is checked FIRST for answerable questions (a
    disappeared answer is `broken` whatever the judge did, so suite decay is never
    masked by abstentions), then route structure is strictly validated (a malformed
    or index-invisible route corrupts the metric and must score as `invalid`, never
    silently coerce), then correctness."""
    record: dict[str, Any] = {
        "id": question["id"],
        "project": question["project"],
        "archetype": question["archetype"],
    }
    answerable = question["archetype"] != "unanswerable"
    if answerable and not anchored:
        record.update(score=0.0, verdict="broken", reason="answer content no longer exists in the corpus")
        return record
    if route is None:
        record.update(score=0.0, verdict="unjudged", reason="no route returned for this question")
        return record
    routed_raw = route.get("routed")
    abstained_raw = route.get("abstained")
    if (
        not isinstance(abstained_raw, bool)
        or not isinstance(routed_raw, list)
        or not all(isinstance(rel, str) for rel in routed_raw)
    ):
        record.update(score=0.0, verdict="invalid", reason="malformed route (routed must be list[str], abstained a real bool)")
        return record
    routed = list(dict.fromkeys(routed_raw))
    record["routed"] = routed
    outside = [rel for rel in routed if rel not in visible]
    if outside:
        # A real session routes from loaded index entries; credit for anything else
        # (hallucinated or unindexed-but-existing paths) would inflate the metric.
        record.update(score=0.0, verdict="invalid", reason=f"routed outside the loaded index: {', '.join(sorted(outside))}")
        return record
    abstained = abstained_raw

    def body_of(rel: str) -> str:
        if memory_dir is None:
            return ""
        if rel not in body_cache:
            path = AUDIT.confined_path(memory_dir, rel)
            body_cache[rel] = (
                path.read_text(encoding="utf-8", errors="replace")
                if path is not None and path.is_file()
                else ""
            )
        return body_cache[rel]

    if not answerable:
        if abstained and not routed:
            record.update(score=1.0, verdict="correct", reason="abstained on unanswerable")
        else:
            record.update(score=0.0, verdict="wrong", reason="routed on an unanswerable question")
        return record

    if abstained or not routed:
        record.update(score=0.0, verdict="miss", reason="abstained on an answerable question")
        return record
    snippet = normalize(question["answer_snippet"])
    hits = [rel for rel in routed if snippet in normalize(body_of(rel))]
    if not hits:
        record.update(score=0.0, verdict="miss", reason="routed note(s) do not contain the answer")
        return record
    # multihop legitimately opens the linked pair; only opens beyond the hop set
    # are extraneous (Letta leaderboard penalty). Everything else expects one note.
    allowed_opens = 2 if question["archetype"] == "multihop" else 1
    if len(routed) <= allowed_opens:
        record.update(score=1.0, verdict="correct", reason="routed within the expected note set")
    else:
        record.update(score=0.5, verdict="loose", reason=f"answer found but {len(routed)} notes routed")
    # Staleness check: routing landed on a note carrying supersession markers.
    stale = [rel for rel in hits if AUDIT.SUPERSESSION_RE.search(body_of(rel))]
    if stale:
        record["stale_route"] = stale
    return record


def load_routes(path: str) -> tuple[dict[str, dict[str, Any]], int]:
    """Load one judge pass's routes, refusing loudly on a schema this eval does not
    understand rather than silently treating unrecognized shapes as empty."""
    routes_path = Path(path).expanduser()
    routes_raw = json.loads(routes_path.read_text(encoding="utf-8"))
    if isinstance(routes_raw, dict):
        schema_version = routes_raw.get("schema_version")
        if schema_version is not None and schema_version != ROUTES_SCHEMA_VERSION:
            raise SystemExit(
                f"memory-dream eval score: {routes_path}: routes schema_version "
                f"{schema_version!r} is not supported (expected {ROUTES_SCHEMA_VERSION}); "
                "this judge pass is incompatible with this eval version"
            )
        routes_list = routes_raw.get("routes", routes_raw)
    else:
        routes_list = routes_raw
    if not isinstance(routes_list, list):
        raise SystemExit(
            f"memory-dream eval score: {routes_path}: expected a routes list "
            '(a bare JSON list, or an object with a "routes" key holding one); '
            f"got {type(routes_list).__name__}"
        )
    routes: dict[str, dict[str, Any]] = {}
    duplicates = 0
    for entry in routes_list:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            if entry["id"] in routes:
                duplicates += 1  # keep the first; a duplicate is judge noise
                continue
            routes[entry["id"]] = entry
    return routes, duplicates


def run_score(args: argparse.Namespace) -> int:
    live_root = Path(args.live_root).expanduser()
    suite = json.loads(Path(args.suite).expanduser().read_text(encoding="utf-8"))
    # Multiple --routes files = independent judge passes over the same suite; the
    # per-question score is the median across passes (measured 2026-07-18 during
    # the authors' consolidation campaign: single-pass noise on byte-identical
    # inputs was ~4pts, twice the published judge-variance band).
    passes: list[dict[str, dict[str, Any]]] = []
    duplicate_routes = 0
    for routes_path in args.routes:
        routes, duplicates = load_routes(routes_path)
        passes.append(routes)
        duplicate_routes += duplicates
    live = AUDIT.project_dirs(live_root, live=True)

    records: list[dict[str, Any]] = []
    per_pass_scores: list[list[float]] = [[] for _ in passes]
    body_caches: dict[str, dict[str, str]] = {}
    norm_bodies: dict[str, list[str]] = {}
    visible_by_project: dict[str, set[str]] = {}
    for question in suite["questions"]:
        project = question["project"]
        memory_dir = live.get(project)
        if project not in norm_bodies:
            norm_bodies[project] = (
                [
                    normalize((memory_dir / rel).read_text(encoding="utf-8", errors="replace"))
                    for rel in AUDIT.scan_project_notes(memory_dir)
                ]
                if memory_dir is not None
                else []
            )
            targets, _escaping = AUDIT.index_targets(AUDIT.loaded_index_text(raw_index_text(memory_dir)))
            visible_by_project[project] = targets - {"MEMORY.md"}
        anchored = question["archetype"] == "unanswerable" or any(
            normalize(question["answer_snippet"]) in body for body in norm_bodies[project]
        )
        cache = body_caches.setdefault(project, {})
        candidates = [
            score_question(
                question, pass_routes.get(question["id"]), memory_dir, cache,
                visible_by_project[project], anchored,
            )
            for pass_routes in passes
        ]
        median = statistics.median(record["score"] for record in candidates)
        chosen = min(candidates, key=lambda record: abs(record["score"] - median))
        if len(passes) > 1:
            chosen = dict(chosen)
            chosen["score"] = median
            chosen["pass_scores"] = [record["score"] for record in candidates]
        for index, record in enumerate(candidates):
            if record["verdict"] != "broken":
                per_pass_scores[index].append(record["score"])
        records.append(chosen)

    broken = [r for r in records if r["verdict"] == "broken"]
    scoreable = [r for r in records if r["verdict"] != "broken"]
    # Accuracy covers ANSWERABLE questions only: writers cannot verify a question is
    # truly unanswerable corpus-wide (they see a sample), so abstention discipline is
    # reported as its own metric, never folded into accuracy.
    answerable = [r for r in scoreable if r["archetype"] != "unanswerable"]
    negatives = [r for r in scoreable if r["archetype"] == "unanswerable"]
    by_archetype: dict[str, list[float]] = {}
    for record in scoreable:
        by_archetype.setdefault(record["archetype"], []).append(record["score"])
    accuracy = round(100 * sum(r["score"] for r in answerable) / len(answerable), 1) if answerable else 0.0
    abstention = round(100 * sum(r["score"] for r in negatives) / len(negatives), 1) if negatives else None
    index_tokens = 0
    for project in sorted({q["project"] for q in suite["questions"]}):
        loaded = AUDIT.loaded_index_text(raw_index_text(live.get(project)))
        index_tokens += len(loaded.encode("utf-8")) // 4
    run = {
        "schema_version": SUITE_SCHEMA_VERSION,
        "run_id": args.run_id,
        "suite_id": suite["suite_id"],
        "fingerprint": args.fingerprint,
        "created": time.time(),
        "accuracy": accuracy,
        "abstention": abstention,
        "by_archetype": {
            key: round(100 * sum(values) / len(values), 1) for key, values in sorted(by_archetype.items())
        },
        "questions": len(records),
        "answerable_scored": len(answerable),
        "broken": len(broken),
        "stale_routes": sum(1 for r in scoreable if r.get("stale_route")),
        "unjudged": sum(1 for r in scoreable if r["verdict"] == "unjudged"),
        "invalid": sum(1 for r in scoreable if r["verdict"] == "invalid") + duplicate_routes,
        "index_tokens": index_tokens,
        "passes": len(passes),
        "pass_accuracies": [
            round(100 * sum(scores) / len(scores), 1) if scores else 0.0 for scores in per_pass_scores
        ],
        "records": sorted(records, key=lambda r: (r["project"], r["id"])),
    }
    # Paired-flip analysis against an explicit baseline run: the primary readout
    # (aggregate accuracy hides which questions moved and mixes in judge noise
    # from untouched projects).
    if args.baseline:
        baseline_path = Path(args.out_dir).expanduser() / f"run-{args.baseline}.json"
        try:
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            print(f"memory-dream eval score: cannot read baseline run {args.baseline}: {error}", file=sys.stderr)
            return 2
        if baseline.get("suite_id") != run["suite_id"]:
            print("memory-dream eval score: baseline is a different suite; refusing the paired comparison", file=sys.stderr)
            return 2
        base_records = {r["id"]: r for r in baseline.get("records", [])}
        ups, downs = [], []
        decayed_excluded = 0
        net = 0.0
        for record in records:
            before = base_records.get(record["id"])
            if before is None:
                continue
            # A record broken in either run is suite decay (the anchor no longer
            # exists verbatim), not a routing flip: pairing it would report every
            # consolidation rephrase as a regression.
            if record["verdict"] == "broken" or before.get("verdict") == "broken":
                decayed_excluded += 1
                continue
            net += record["score"] - before["score"]
            if record["score"] > before["score"]:
                ups.append(record)
            elif record["score"] < before["score"]:
                downs.append(record)
        run["paired"] = {
            "baseline": args.baseline,
            "up": len(ups),
            "down": len(downs),
            "decayed_excluded": decayed_excluded,
            "net": round(net, 1),
            "flips": [
                {"id": r["id"], "project": r["project"], "direction": direction, "verdict": r["verdict"]}
                for direction, group in (("up", ups), ("down", downs))
                for r in group[:20]
            ],
        }

    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    AUDIT.atomic_write(out_dir / f"run-{args.run_id}.json", (json.dumps(run, indent=2, sort_keys=True) + "\n").encode("utf-8"))

    lines = [
        f"memory-dream eval score: suite {run['suite_id']} run {args.run_id}: "
        f"accuracy {accuracy} over {len(answerable)} answerable "
        f"(abstention {abstention}, {len(broken)} broken, {run['invalid']} invalid, "
        f"{run['stale_routes']} stale-routed) index~{index_tokens} tokens"
    ]
    if len(passes) > 1:
        lines.append(f"  passes: {len(passes)}, per-pass accuracy {run['pass_accuracies']} (median-scored)")
    for archetype, value in run["by_archetype"].items():
        lines.append(f"  {archetype}: {value}")
    if run.get("paired"):
        paired = run["paired"]
        lines.append(
            f"paired vs {paired['baseline']}: {paired['up']} up, {paired['down']} down, "
            f"net {paired['net']:+} ({paired['decayed_excluded']} decayed excluded)"
        )
    # Delta vs the newest prior run of the SAME suite AND SAME harness fingerprint
    # (judge model + prompt): anything else is not a comparable baseline.
    previous = None
    for path in out_dir.glob("run-*.json"):
        if path.name == f"run-{args.run_id}.json":
            continue
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if candidate.get("suite_id") != run["suite_id"]:
            continue
        if candidate.get("fingerprint") != args.fingerprint:
            continue
        if previous is None or candidate.get("created", 0) > previous.get("created", 0):
            previous = candidate
    if previous:
        delta = round(accuracy - previous["accuracy"], 1)
        noise = " (within the +/-2pt judge-noise band, treat as flat)" if abs(delta) < NOISE_BAND_POINTS else ""
        lines.append(
            f"delta vs run {previous.get('run_id')}: {delta:+} points{noise}; "
            f"index {index_tokens - previous.get('index_tokens', 0):+} tokens"
        )
    if broken and len(broken) / max(1, len(records)) > BROKEN_SUITE_FRACTION:
        lines.append(
            f"WARN suite decay: {len(broken)}/{len(records)} questions lost their content anchor; regenerate the suite"
        )
    print("\n".join(lines))
    return 0


# --- discriminability ---------------------------------------------------------


def run_discriminability(args: argparse.Namespace) -> int:
    """Per-project pairwise description similarity: the deterministic complement to
    the judged eval (zero noise), measuring exactly the failure mode where sibling
    descriptions are too blurred to route between."""
    live_root = Path(args.live_root).expanduser()
    live = AUDIT.project_dirs(live_root, live=True)
    overall: list[float] = []
    for project in sorted(live):
        if args.project and project != args.project:
            continue
        memory_dir = live[project]
        if memory_dir.is_symlink() or memory_dir.parent.is_symlink():
            continue
        records = AUDIT.scan_project_notes(memory_dir)
        described = sorted(
            (rel, record["description"]) for rel, record in records.items() if record.get("description")
        )
        if len(described) < 2:
            continue
        pairs = [
            (AUDIT.description_similarity(a_desc, b_desc), a_rel, b_rel)
            for i, (a_rel, a_desc) in enumerate(described)
            for b_rel, b_desc in described[i + 1 :]
        ]
        values = [similarity for similarity, _a, _b in pairs]
        overall.extend(values)
        worst = sorted(pairs, reverse=True)[: args.top]
        print(
            f"{project}: {len(described)} described notes, "
            f"mean {statistics.mean(values):.3f}, max {worst[0][0]:.3f}"
        )
        for similarity, a_rel, b_rel in worst:
            if similarity > 0:
                print(f"  {similarity:.2f} {a_rel} ~ {b_rel}")
    if overall:
        print(f"corpus mean {statistics.mean(overall):.3f} over {len(overall)} pairs")
    return 0


# --- CLI ---------------------------------------------------------------------


def add_parsers(subparsers) -> None:
    """Register the single top-level `eval` subcommand and its nested
    sample/freeze/routing-input/score/discriminability subcommands."""
    eval_parser = subparsers.add_parser(
        "eval",
        help="recall-regression eval: sample notes, freeze a suite, emit judge input, score routes",
        description=__doc__.splitlines()[0],
    )
    eval_parser.add_argument(
        "--live-root",
        default=str(config.default_live_root()),
        help="root of per-project live memory (default: <claude-config-dir>/projects)",
    )
    eval_sub = eval_parser.add_subparsers(dest="eval_command", required=True, metavar="<eval-command>")

    sample = eval_sub.add_parser("sample", help="emit note bodies for the question writers")
    sample.add_argument(
        "--per-project", type=int, default=8,
        help="max notes sampled per project, most-recent first (default: 8)",
    )
    sample.add_argument(
        "--min-bytes", type=int, default=500,
        help="skip notes whose body is under this many bytes; a per-project drop "
        "count is reported on stderr (default: 500, use 0 to include tiny notes)",
    )
    sample.set_defaults(func=run_sample)

    freeze = eval_sub.add_parser("freeze", help="validate drafted questions and freeze the suite")
    freeze.add_argument("--questions", required=True)
    freeze.add_argument("--out", default=str(config.eval_home() / "suite.json"))
    freeze.set_defaults(func=run_freeze)

    routing = eval_sub.add_parser(
        "routing-input", help="emit per-project judge inputs (index text + questions)"
    )
    routing.add_argument("--suite", default=str(config.eval_home() / "suite.json"))
    routing.set_defaults(func=run_routing_input)

    score = eval_sub.add_parser("score", help="score judge routes against the frozen suite")
    score.add_argument("--suite", default=str(config.eval_home() / "suite.json"))
    score.add_argument(
        "--routes", required=True, action="append",
        help="judge routes JSON; repeat for independent passes (per-question median)",
    )
    score.add_argument("--run-id", required=True, help="caller-supplied run tag (e.g. a timestamp or pre/post label)")
    score.add_argument(
        "--fingerprint", default="",
        help="harness fingerprint (judge model + prompt version); deltas only compare runs sharing it",
    )
    score.add_argument("--baseline", default="", help="run id for the paired-flip comparison (primary readout)")
    score.add_argument("--out-dir", default=str(config.eval_home() / "runs"))
    score.set_defaults(func=run_score)

    disc = eval_sub.add_parser(
        "discriminability",
        help="deterministic pairwise description-similarity report (no judge, no noise)",
    )
    disc.add_argument("--project", default="", help="optional project filter")
    disc.add_argument("--top", type=int, default=3, help="worst pairs to show per project")
    disc.set_defaults(func=run_discriminability)
