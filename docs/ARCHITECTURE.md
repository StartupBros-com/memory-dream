# Architecture

memory-dream turns Claude Code's auto-memory from a write-only log into
something that can be consolidated on purpose. It does that by keeping almost
everything deterministic and pushing the one step that genuinely needs
judgment — drafting a consolidated note — into a subagent with no tools at
all. This document describes the three layers, the apply-time gate stack in
the order it actually runs, the build-time verification-coverage gate, mirror
semantics, and recovery.

## The three layers

### Layer 1 — deterministic triage and fix (`audit.py`)

No LLM, no session, no model call. `memory-dream triage` scores every live
note on structure only: supersession markers (`SUPERSEDED`/`CORRECTED`/
`RESOLVED` as a case-sensitive line lead), body size, file-modification-time
age, and inbound wikilink count. A note is *flagged* only on structural rot —
a supersession marker or an oversized body; age and zero-inbound links are
ranking boosts that never flag a note on their own, so a mature store full of
old-but-fine notes doesn't manufacture proposals out of nothing. Output is
project-level only (no filenames, no bodies) plus a trailing `flagged:N`
line — that line is a documented output contract other tooling can parse.

`memory-dream fix` (dry-run by default, `--apply` to write) does the
mechanical, one-answer repairs that need no semantic judgment at all:
single-candidate wikilink rewrites and index reconciliation (drop orphaned
entries, append unindexed notes, byte-for-byte format preservation). Because
this layer can write live files, `--apply` goes through the same backup
mechanism as the apply layer below before touching anything (see Gate 3).

### Layer 2 — deterministic assemble and apply plumbing (`assemble.py`, `apply.py`)

Still no model calls. This layer turns triage output and a subagent's drafts
into a reviewable, appliable artifact, and then applies only what the
operator approved.

`assemble.py plan` clusters flagged notes into per-pass groups (respecting
the per-pass cluster and per-cluster note caps), deferring overflow rather
than silently dropping it, and dropping clusters with a sensitive-flagged
member into `manual_review` — those are never drafted. `assemble.py build`
takes the plan plus a subagent's drafts and turns them into a **patch set**:
it schema-validates every proposal, confines every destination path, retargets
same-project inbound wikilinks to survivors, dry-run-audits every resulting
file, and writes `manifest.json`, per-proposal diffs, `report.json`, and a
`results/` directory of plain files — the patch-set directory is the *only*
surface allowed to hold memory bodies outside live memory itself.
`assemble.py archive` does a separate, fully deterministic operation: moving
settled index entries to a cold, never-auto-loaded file without touching any
note body (see "Archive" below).

`apply.py` takes a patch set plus an operator's `selection.json` and writes
the approved subset to live memory, through the gate stack below. It never
drafts, clusters, or judges anything — every decision it makes is a refusal
or a write of already-approved content.

### Layer 3 — the model-driven pass (the `dream` command)

Everything model-driven lives here, wrapped by the `/memory-dream:dream`
command, and every model call in this layer is either zero-tool or read-only:

- **Drafting.** A zero-tool subagent (`memory-dream:drafter`) receives one
  cluster's note bodies inline and returns one JSON object — a pure text
  transformation with no Read, Write, Bash, or Task access. Because note
  bodies are untrusted input (see SECURITY.md), a drafter with no tools
  cannot be steered by anything inside a note body into performing a write;
  the strongest guarantee is still downstream, since the drafter's JSON gets
  schema-validated, path-confined, sensitive-scanned, operator-reviewed, and
  gated at apply regardless of what tools it did or didn't have.
- **Fidelity verification.** One subagent per proposed file, each given that
  file plus the cluster's source note bodies inline, checks every
  substantive line against the sources and classifies problems (distorted,
  unsupported, dropped qualifier, over-specific, vague). Faithful
  rephrasing, compression, and added wikilinks are not findings.
- **Repo grounding.** Any survivor asserting task state (pending work, an
  open PR or issue, a plan) gets a read-only check against the actual
  repository before build, because fidelity verification only checks a
  draft against its *source notes* and inherits whatever staleness those
  sources already had.
- **Checker-check.** Every edit applied after fidelity verification is
  itself new, unverified text; a per-file pass re-checks only edit-introduced
  defects (broken sentences, inverted references, description/body
  contradictions).
- **Quality panel.** One zero-tool reviewer per lens reads only the *final*
  files with fresh eyes — none of the earlier stages can see outside the
  criteria they themselves authored.
- **Benefit check.** A dozen small judge agents route real questions against
  a shadow-applied copy of the changes versus the live index, to measure
  whether the consolidation actually improved retrieval instead of merely
  not breaking it.

Layer 3's output is the same `plan.json` / drafts / findings inputs that
Layer 2's `build` consumes deterministically — the model never touches live
memory directly, and every judgment it makes is checked by something
downstream that isn't a model, or by the operator.

## The apply gate stack

`apply.py` runs the following gates in order. Each one refuses or skips
rather than guesses: if a gate can't establish that a write is safe, nothing
gets written.

**Gate 1 — single-flight lock.** Before anything else, apply takes a
non-blocking exclusive lock (`compat.FileLock`: `fcntl.flock` on POSIX,
`msvcrt.locking` on Windows) and fails immediately, loudly, if another
process holds it. This exists to catch two concurrent applies racing the
same live tree — a retried command, an accidentally double-launched job — and
it fails fast rather than queuing, so the caller finds out immediately
instead of hanging.

**Gate 2 — consent trace.** In the default `trace` consent mode, apply
recomputes the patch set's content-bound identifier and verifies that a real
transcript turn, occurring *after* the preview was generated, exists in the
operator's session and carries that identifier. This exists to catch an
automated draft-then-apply sequence that never passed through a human at
all: a fully scripted pipeline has no way to produce a post-preview human
transcript turn, so it cannot approve its own output. What this gate proves
and does not prove is covered in full in SECURITY.md.

**Gate 3 — snapshot backup (default) or mirror freshness (optional mode).**
With no mirror configured (the default), apply snapshots every file it is
about to touch, plus each affected project's index, into the patch set's own
backup directory before writing anything — so `restore` can always reverse
the change even though nothing outside this machine ever saw it. With a
mirror configured, apply instead refuses per-project unless that project's
git-tracked mirror is already at least as fresh as live. Either way, this
gate exists to catch the case an apply turns out to be wrong: something must
already be true, before the write happens, that makes the change reversible.

**Gate 4 — destination confinement.** Every proposal's destination path is
checked before it is written: absolute paths, `..` traversal, symlink or
junction escapes (`compat.is_escaping_link`), and direct writes to a
project's index file are all rejected per proposal. This exists to catch a
proposal — whether from a drafting bug or from something adversarial sitting
in a note body — trying to write somewhere outside its own project's note
directory.

**Gate 5 — sensitive-skip and source-digest re-verification.** Two distinct
checks share one mechanism, "skip this whole proposal, apply the rest of the
batch": a proposal touching a sensitive-flagged note is skipped whole, never
partially applied, so a sensitive body can never leak through a half-applied
edit; and a proposal whose source note changed on disk since the draft was
made is skipped too, because applying a draft against content that no longer
exists there would silently discard someone else's newer edit. A vanished
project downgrades its proposals to skipped the same way — the batch never
aborts because one project disappeared.

**Gate 6 — per-project stage-then-commit atomicity.** Every file for a
project is staged to a temporary location first and only renamed into place
once the whole project's batch is ready, with one index reconciliation per
project (single writer). This exists to catch a mid-write crash or a single
bad file corrupting a project's memory tree: a failure at any point during
staging leaves that project's live memory exactly as it was before the
apply started.

**Gate 7 — apply manifest and completion line.** The last thing apply does
is write `apply-manifest.json` (listing every applied change and every skip,
with its reason) and print a `DREAM-APPLY-COMPLETE ...` line. Both are
documented output contracts: the manifest is what a mirror-mode operator
reads to know which files to remove from the mirror (see below), and the
completion line is what a wrapping command or script checks for success
without re-parsing prose.

## The verification-coverage gate (build time)

This gate runs earlier, inside `assemble.py build`, and it exists to enforce
that Layer 3's verification stages actually ran — because an earlier,
undocumented-only version of this pipeline shipped a patch set that skipped
them (see PROVENANCE.md).

Each verification stage persists its verdict, incrementally, to one findings
file keyed by cluster id: a status of `clean` or `fixed`, plus a
`drafts_digest` — the sha256 of that cluster's *exact* proposals payload.
`build` refuses to assemble any drafted cluster that doesn't have a
`clean`/`fixed` entry, **and** it independently recomputes the digest of the
drafts payload it is about to assemble and refuses if that doesn't match the
digest recorded in the findings entry. A status alone only proves that *some*
payload for that cluster id was checked at some point — not that it's the
exact bytes about to be assembled. Content-binding closes that gap: an
edited draft, or a stale findings entry left over from an earlier redraft of
the same cluster, can never ride through as "already verified."

## Deletions and the mirror (mirror mode)

In mirror mode, recording a pass copies live memory into the mirror but never
deletes from it — the mirror deliberately keeps its own history even for
content live memory no longer has. That means a note a pass consolidated
away is still present in the mirror's current tree, and would resurrect if
someone ever restored live memory from the mirror. The apply manifest lists
every applied deletion under `deleted`; removing those specific paths from
the mirror is a separate, explicit, git-recoverable operation the operator
performs after recording — never an implicit side effect of the record step
itself. This is deliberate: the mirror's *current* tree should reflect the
consolidation, while its *history* keeps the deleted content recoverable
regardless.

In snapshot (default) mode there is no separate mirror tree to prune — the
snapshot already captured pre-apply content, and `restore` is the undo path
instead.

## Recovery

**Snapshot mode (default).** `apply.py restore` reverses an applied patch
set from its own backup snapshot, per project, atomically. It refuses on a
digest mismatch (something changed since the snapshot was taken) unless
explicitly forced. Because Gate 6 makes apply itself per-project atomic and
Gate 1 makes it single-flight, a crash mid-apply and a subsequent re-run
never double-applies.

**Mirror mode.** Applied content is recoverable from the mirror's git
history regardless of whether the patch-set directory still exists. If a
session ends after apply but before the mirror is recorded, the mirror
simply lags live temporarily — that drift is detectable and a plain resync
reconciles it; nothing is lost because the git history is what recovery
relies on, not the patch-set directory.

## Glossary

The source harness this was extracted from used a set of internal shorthand
codes for these gates in code comments and doc references. They're listed
here only so anyone cross-referencing an older discussion of this pipeline
can map a code to the concept described above — every other document in
this repository uses only the public names.

| Shorthand | Public gate name |
|---|---|
| R3 | Schema validation |
| R6 | Operator preview + item-by-item approval |
| R7 | Mirror freshness gate (optional mode) / snapshot backup (default) |
| R8 | Active-session warning |
| R9 / R10 / R14 | Mirror record + git-recoverable deletions (mirror mode only) |
| R13 | Source-changed-since-draft skip |
| R15 | Post-build audit dry-run |
| R16 | Inbound-wikilink retargeting |
| R17 | Consent trace (post-preview approval-turn verification) |
| R18 | Destination confinement |
| R19 | Zero-tool drafter |
| — | Verification-coverage gate (`--findings` + `drafts_digest` content binding) |
