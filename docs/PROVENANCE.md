# Provenance: how the verification stack got hardened

The verification stages this pipeline runs before a patch set reaches
preview were not designed in from a blank page. They were hardened across
12 or more supervised consolidation passes the authors ran over their own
multi-project memory corpus in July 2026, each one reviewed by a human
before anything applied. Every stage described below exists because a real
defect survived every stage that came before it, in a documented pass, on a
dated occasion. This document keeps the dates — they're the evidence — and
drops every private identifier attached to them.

The throughline: **routing quality and factual fidelity are independent
axes.** A drafted note can pass every check that measures whether it's
findable and still be quietly wrong, and no single stage below was enough on
its own to catch everything. Each one closes a gap the others structurally
cannot see.

## Fidelity verification

**Defect class: drafter-invented content that inverts the source's real
meaning while still reading as fluent, well-routed prose.**

A per-file audit dated 2026-07-18 found a drafted note that had coined its
own gloss for an abbreviation appearing in the source material — and gotten
the meaning backwards. Every routing-focused check the draft had already
passed (schema validation, description quality, path confinement) still
passed it, because none of them read the *content* against its *sources*;
they all evaluate structure or discoverability, and a fluent, well-formed,
plausible-sounding sentence structurally looks identical whether or not it's
true.

The fix: one fidelity-verifier subagent per proposed file, given that file
plus the cluster's source note bodies inline, checking every substantive
line and classifying problems as distorted, unsupported, a dropped
qualifier that changes meaning, over-specific content likely to rot, or
vague. Faithful rephrasing, compression, and added links are explicitly not
findings — the goal is catching invention, not penalizing editing.

A related, purely operational lesson from the same period: per-proposal
fidelity verifiers on large clusters (multi-note prompts) stalled
repeatedly under constrained capacity, while the same content split into
one verifier per proposed *file* completed reliably in under a minute per
batch. Scoping is load-bearing, not a style preference — an overloaded
verifier prompt doesn't just run slower, it's more likely to silently skip
real defects.

## Repo grounding

**Defect class: a draft that faithfully summarizes a source note, when the
source note itself has gone stale.**

Fidelity verification only checks a draft against its *source notes* — by
design, that's what "faithful" means at that stage. But if the source note
itself asserts something no longer true (a task described as pending that
has actually shipped), a perfectly faithful draft just carries that
staleness forward unchanged, and fidelity verification has no way to notice,
because nothing about the draft *disagrees* with its source.

A pass dated 2026-07-19 shipped exactly this: a survivor note described a
piece of work as pending when it had actually shipped a number of days
earlier, and five other verification layers — including fidelity
verification — had already passed the draft before this gap was caught.

The fix: any survivor asserting task state (pending work, an open item, a
plan) gets one additional, read-only check against the actual repository
before build — did the described thing actually ship, land, or resolve? If
so, the survivor gets rewritten as a resolved record with inline evidence
(what happened, when) rather than left asserting a state that's no longer
current.

## Checker-check

**Defect class: a correction that fixes one problem while introducing a
new one, because the correction itself was never verified.**

Every edit applied in response to an earlier verification stage's findings
— a high-severity fix, a repo-grounding correction — is itself brand-new,
unverified text. A splice into an existing sentence can invert a directional
reference after reordering; a correction can contradict its own note's
description; a fix applied without checking its own provenance can silently
become the very kind of unsupported claim the earlier stage was trying to
eliminate.

One documented round of edits, dated 2026-07-19, was re-checked specifically
for edit-introduced defects (as opposed to re-running the original fidelity
check) and found one real splice defect and several corrections missing
inline provenance, across roughly two dozen edits in that round.

The fix: a dedicated re-verification pass, scoped only to edits made after
the fidelity fleet ran, checking for broken sentences, inverted references,
description-versus-body contradictions, and facts stated without
provenance. The governing rule that came out of this: any correction that
overrules a source must carry its verification evidence inline — a merge
date, a check performed on a specific date, a doc reference — or a later,
independent checker has no way to distinguish a sourced correction from an
invented one.

## Quality panel

**Defect class: whole categories of defect that are invisible to every
stage before this one, because every stage before this one grades a draft
against criteria the same process authored.**

Fidelity verification checks against sources. Checker-check checks against
the earlier stage's own findings. Neither can see a defect that isn't on
either list — and a pass dated 2026-07-20 found exactly that gap: an
independent panel, reading only the *final* files with fresh eyes and one
reviewer per lens (reader value versus noise, durability of claims, routing
surfaces, cross-file consistency), surfaced 28 findings that no earlier,
criteria-bound stage had a way to notice. Among them: edit-history
narration leaking into note prose — phrasing that addresses a reviewer
("not X as previously noted," "resolving this note's internal
inconsistency") rather than a future reader, which reads as noise to anyone
who wasn't in the room for the edit.

A caveat earned the hard way, on the same 2026-07-20 date: panels
themselves produce confident false positives. Reviewing a panel's own
findings against ground truth before applying any fix is not optional —
one case that day flagged a quoted passage that did not actually appear in
any file (panels paraphrase, and paraphrase can drift into fabrication),
and a separate case flagged a date as "impossible" when it was in fact
faithfully citing a source document whose own filename happened to be a
typo. Applying that
suggested fix would have introduced an error where none existed. The
operating rule: verify a panel's claim against the actual text before
acting on it, every time — a panel earns scrutiny, not automatic trust,
precisely because it's the stage designed to catch what nothing else can
see, which also means nothing else is positioned to catch *it* if it's wrong.

## Benefit A/B

**Defect class: a description that is accurate, durable, and
non-destructive — and still buries the note's actual content, so the note
stops getting found.**

Every stage above this one checks a draft for problems with what it *says*.
None of them check whether the note still gets *routed to* — a
description can pass fidelity, grounding, checker-check, and the quality
panel while describing only a note's surface-level outcome and omitting the
mechanism-level lesson the note actually exists to carry. A pass dated
2026-07-18 shipped exactly this: a description that named only an outcome
caused routing judges to abstain on a question the *old* description had
answered correctly every time, and every other gate had already passed the
new description as clean, durable, and truthful.

The fix is the only stage in this pipeline that measures benefit directly
rather than checking for absence of harm: shadow-apply the patch set to a
throwaway copy of the affected projects, write a small number of
task-phrased questions per changed note answerable from content present on
*both* sides of the change, and route those questions through several
decorrelated judge-prompt variants against both the live and the shadow
index. Any question the live side answers and the shadow side loses is a
build defect — full stop, not a judgment call — and gets fixed (by
advertising the buried content in the description, or extracting it as its
own note) before the patch set is rebuilt and re-checked. Two disciplines
that make this measurement trustworthy rather than self-fulfilling: the
questions are frozen *before* any fix is drafted, and a fix is worded from
the note body's own content, never from the question text, so a fix that
passes the re-run also improves routing for phrasings nobody tested.

## Verification-coverage gate

**Defect class: an abbreviated pass that skips the stages above, and ships
the defects they exist to catch.**

This is the stage that made every stage above it non-optional. One
documented pass, run without the fidelity, grounding, checker-check, and
quality-panel stages, assembled a patch set that a post-hoc audit later
found to carry 26 blocking losses — a lost repair command, a lost
open-follow-up list, and outright fabricated claims among them — into a
patch set that had already reached the preview stage before anyone caught
it. Every one of those 26 defects would have been caught by a stage that
existed at the time and was simply not run. Documenting that the stages are
mandatory did not, on its own, prevent this.

The fix that followed is mechanical rather than procedural: `build` now
refuses to assemble any drafted cluster that doesn't carry a `clean` or
`fixed` verdict from the verification stages, and — because a status alone
only proves that *some* payload for that cluster id was checked, not that
it's the exact one about to ship — refuses again if the content digest of
that verdict doesn't match the actual drafts payload being assembled.
Skipping a stage, or assembling an edited draft that was never re-verified,
is now a hard build failure instead of a possible oversight. Full mechanics
in ARCHITECTURE.md, "The verification-coverage gate."
