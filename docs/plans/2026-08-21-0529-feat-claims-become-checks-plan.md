---
title: Claims-Become-Checks Hardening - Plan
type: feat
date: 2026-08-21
topic: claims-become-checks
artifact_contract: ce-unified-plan/v1
artifact_readiness: requirements-only
product_contract_source: ce-brainstorm
execution: code
---

# Claims-Become-Checks Hardening - Plan

## Goal Capsule

- **Objective:** Every self-claim memory-dream makes — version-measured caps, consent-gate verification, panel quote checking, suppression memory, retention — is either enforced by code or visibly reported, so the operator learns about drift, fabrication, and accumulation from the tool rather than from the next incident.
- **Means:** Extend existing patterns (advisory doctor checks, the applied-suppression mechanism, the recall-eval substring check) across four components: a drift-honest doctor, a panel quote-existence gate, pipeline decision memory, and a read-only retention advisory.
- **Product authority:** This plan owns the four-component hardening pass. The other 2026-08-21 ideation survivors (Auto Dream positioning, review-attention tiering, sentinel recalibration) are not active scope.
- **Open blockers:** none.

---

## Product Contract

### Summary

Harden memory-dream so its self-claims are enforced or visible: a drift-honest doctor with one aggregate verdict, a code-level quote-existence gate on verification-panel findings, symmetric memory for rejected proposals plus repeat-deferral visibility, and a read-only retention advisory. Every addition is advisory; no new pipeline stages, no blocking behavior by default, no deletion code.

### Problem Frame

The repo asserts things its code does not enforce. The "measured against Claude Code v2.1.211" caveat is hand-synced across six files, and the doctor check meant to guard it passes a literal `True` (`memory_dream/cli.py:129-136`) — it structurally cannot fail. The consent gate depends on transcript flag keys that, if renamed upstream, would make a compaction turn read as a normal operator turn with no exception raised; the v0.2.1 incident (a compaction summary forging operator consent) came from exactly this class of silent host drift. `agents/scribe.md` claims "quote-existence checks on findings" that no code performs, while `docs/PROVENANCE.md` records a dated case (2026-07-20) of a panel quoting text that appeared in no file. The pipeline remembers approvals (`SUPPRESS_APPLIED_DAYS`) but not rejections, so a declined cluster is re-drafted and re-verified every pass, and deferred clusters surface only as counts — a cluster can lose the per-pass cap indefinitely without anyone seeing it. `PATCH_SET_RETENTION_DAYS` is documented as doing nothing on its own while patch-set copies of sensitive note content accumulate unbounded.

### Key Decisions

- **Retention is resolved read-only** — a doctor advisory, not a prune. (session-settled: user-directed — chosen over a cleanup command and deleting the knob: read-only keeps the never-deletes identity at the smallest diff.) Governs R12, R13, R14.
- **Full doctor package, not the minimal drift core.** (session-settled: user-directed — chosen over core-only variants: all parts are read-only, and the single aggregate line is the anti-noise discipline.) Governs R1-R7.
- **Advisory-first contract:** doctor's default (flag-less) exit-code behavior is unchanged; strictness is opt-in. Governs R6, R7.
- **Rejection memory mirrors the applied-suppression shape and includes its prerequisite:** no rejected-ID record exists today (verified — `selection.json` carries only `approved`, the apply manifest records only processed proposals), so recording rejections is part of the requirement, not an assumption. Governs R8, R9, R10.
- **The quote gate is the first programmatic consumer of panel findings** (verified — nothing parses the finding fields today, and the build-time coverage gate validates a separate schema), so the gate flags failures rather than discarding findings. Governs R15, R16, R17.

### Requirements

**Drift doctor**

- R1. One compatibility record owns the measured-host claim (measured Claude Code version plus the measured index-cap values); every site that states it today (`memory_dream/config.py`, `memory_dream/audit.py`, `memory_dream/cli.py`, `docs/TUNING.md`, `README.md`, `docs/EXTRACTION-DESIGN.md`) derives from or cites the record instead of hand-syncing the string.
- R2. Doctor compares the installed Claude Code version against the record and reports mismatch as a drift advisory; the index-cap check's result reflects this comparison instead of a constant pass.
- R3. Doctor verifies the consent-gate transcript flags: locate a compaction-shaped turn in a recent transcript and assert the expected flag keys are present; when no compaction sample exists, report "unverified — no compaction sample," never drift.
- R4. Doctor lists every config value that differs from its shipped default, naming the override source (env var or JSON config key).
- R5. Doctor surfaces the deterministic triage readiness count (flagged clusters), excluding clusters suppressed by either the applied or the rejected window.
- R6. A `--strict` flag makes doctor exit nonzero when any drift advisory fired; the default invocation's exit-code contract is unchanged.
- R7. Doctor output ends with one aggregate line stating whether anything drifted.

**Pipeline decision memory**

- R8. The selection/apply flow durably records which assembled proposals the operator did not approve.
- R9. Triage suppresses re-flagging clusters whose proposals were rejected within the suppression window, symmetric to the applied-side mechanism, and the suppression decays after the window.
- R10. Rejected-cluster identity survives note renames and merges well enough that an unchanged cluster is recognized across passes.
- R11. Pass reporting shows repeat-deferral identity — which clusters were deferred and for how many consecutive passes — from the per-item deferral data already persisted.

**Retention advisory**

- R12. Doctor reports patch-set directories older than `PATCH_SET_RETENTION_DAYS` (count and total size) using the existing advisory-check pattern.
- R13. Doctor checks the well-known preview-copy location (the same path computation `open-preview` uses) and reports a leftover copy.
- R14. The retention value's documentation names its consumer (the advisory), replacing "does nothing on its own."

**Panel quote gate**

- R15. Panel findings carry a machine-readable quoted span and source path alongside the existing fields.
- R16. Code verifies each finding's quoted span exists in its cited source before the finding reaches model adjudication; failures mark the finding unverifiable — visibly flagged, never dropped.
- R17. `agents/scribe.md` no longer claims a quote-existence check the code does not perform; after R16 the claim is true and cites the real mechanism.

### Key Flows

- F1. Post-upgrade drift check
  - **Trigger:** Operator upgrades Claude Code (or suspects drift) and runs doctor.
  - **Steps:** Doctor compares installed version to the compatibility record; runs the transcript-flag canary; lists non-default config; reports overdue patch sets and leftover preview copy; prints the aggregate verdict.
  - **Outcome:** Drift is visible in one run; `--strict` gives scripts a single exit code. **Covers R1-R7, R12, R13.**
- F2. Panel finding lifecycle
  - **Trigger:** A verification panel returns findings during a dream pass.
  - **Steps:** Each finding's quoted span is substring-checked against its cited source; verified findings proceed to model adjudication; failed findings are flagged unverifiable and shown with that label.
  - **Outcome:** A fabricated quote can no longer masquerade as evidence; the operator sees which findings earned trust. **Covers R15, R16.**
- F3. Rejection remembered across passes
  - **Trigger:** The operator declines a proposal during apply.
  - **Steps:** The rejection is recorded; the next pass's triage suppresses the cluster while the window holds; after decay, the cluster may be flagged again; the readiness count excludes suppressed clusters throughout.
  - **Outcome:** No re-drafting or re-verifying of a decision the operator already made, without making rejection permanent. **Covers R5, R8, R9.**

### Acceptance Examples

- AE1. **Covers R3, R6.** Given no compaction-shaped turn exists in recent transcripts, when doctor runs, then the canary reports "unverified — no compaction sample" and `--strict` still exits 0.
- AE2. **Covers R3, R6, R7.** Given a transcript where the consent-flag keys are renamed, when doctor runs, then the canary reports drift, the aggregate line says so, default exit stays 0, and `--strict` exits nonzero.
- AE3. **Covers R8, R9.** Given the operator rejected a cluster's proposal in pass N, when pass N+1 runs inside the suppression window, then the cluster is not re-flagged or re-drafted; when a pass runs after the window, it may be flagged again.
- AE4. **Covers R16.** Given a panel finding quoting text absent from its cited source, when the gate runs, then the finding is marked unverifiable and still displayed — never silently dropped.
- AE5. **Covers R4.** Given `MERGE_JACCARD` is overridden via env var, when doctor runs, then the value is listed as non-default with its env-var source.
- AE6. **Covers R12.** Given patch-set directories older than the retention window, when doctor runs, then their count and total size are reported and nothing is deleted.

### Success Criteria

- Every new advisory has a test that simulates its drift or failure condition — the checks are themselves checked.
- Existing doctor exit-code tests pass unchanged; a healthy layout produces the same default behavior as today.
- A clean dream pass requires no new operator action from any of this.

### Scope Boundaries

**Deferred for later**

- Auto Dream positioning rewrite, review-attention tiering (active-session warning fix, batch consent for the archive tier, preview rollup line), sentinel recalibration, and the generation counter — all remain candidates in `docs/ideation/2026-08-21-memory-dream-skill-ideation.html`.
- The `MIRROR_PUSH_HINT` placeholder papercut.

**Outside this product's identity**

- Any deletion or cleanup code; retention stays read-only per KD1.
- Any autonomy increase: no auto-apply, no scheduled passes, no blocking doctor behavior by default.

### Dependencies / Assumptions

- Doctor can determine the installed Claude Code version (CLI invocation or equivalent); when it cannot, the comparison reports "unverifiable," never drift.
- Patch sets live under the pass root (`memory_dream/config.py:119-122`); the advisory walks only that root.
- The preview copy lands at a deterministic path (`memory_dream/cli.py:193`); the advisory reuses that computation. Historical copies at other locations are not discoverable.
- Verified absences this plan builds on: no rejected-ID record exists (R8 creates it); no code parses panel finding fields (R16 is the first consumer).

### Outstanding Questions

**Deferred to Planning**

- The rejected-cluster signature mechanism for R10 (what identifies "the same cluster" across renames and merges).
- Where the rejection record lives (a field in `selection.json`, the apply manifest, or a sidecar).
- The compatibility record's concrete shape (module constant vs data file) and how docs cite it.
- How doctor obtains the installed Claude Code version.

**Resolve Before Planning:** none.

### Sources / Research

- `docs/ideation/2026-08-21-memory-dream-skill-ideation.html` — ideas 1-4 and their verified bases; the rejection table explains what was cut and why.
- Fresh-context verification (2026-08-21), key anchors: `memory_dream/cli.py:129-136` (always-True check), `memory_dream/transcript.py:77-113` (parse-only probe; silent-failure mode if flag keys rename), `memory_dream/apply.py:672` (approved-only selection), `memory_dream/assemble.py:897-964` (coverage gate does not bind the finding schema), `memory_dream/audit.py:717-826` (applied-suppression mechanism), `memory_dream/cli.py:193` (deterministic preview path).
- `docs/PROVENANCE.md` (panel false positives, 2026-07-20) and `CHANGELOG.md` v0.2.1 (compaction-summary consent forgery) — the incident record motivating R3 and R16.
- `memory_dream/recall_eval.py:183` — the existing substring-check precedent R16 replicates.
