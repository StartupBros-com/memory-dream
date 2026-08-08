# memory-dream

**The sleep cycle for Claude Code auto-memory.** Your agent writes notes every session and never cleans them up. memory-dream is the consolidation pass that was missing: it finds the rot deterministically, drafts fixes with a subagent that cannot touch your files, shows you a diff for every single change, and applies only what you approve. Then it measures whether recall actually got better.

Plenty of tooling captures memories. Nothing consolidates them. That is the gap this fills.

## The problem

Auto-memory grows monotonically. Notes supersede each other and both survive. Descriptions go stale and route future sessions to wrong answers. Long logs bury the one durable lesson. And the per-project index has a hard load cap: once `MEMORY.md` outgrows what Claude Code actually loads, your newest notes silently stop routing at all.

Deleting by hand is worse. Recall is agentic and invisible: you cannot see which note answered a question, so you cannot see what a deletion broke. Consolidation without measurement is guessing.

## What it does

One pass, operator-gated end to end:

1. **Triage** scores every note on structural rot: supersession markers, oversized bodies, decayed confidence. Deterministic, read-only, no model calls.
2. **Plan** clusters the flagged notes per project. Deterministic.
3. **Draft** hands each cluster to a **zero-tool subagent**: note bodies in, one JSON object out. A prompt injection inside a note body has no Bash, no Write, no file access, and its output is re-validated, path-confined, and operator-reviewed before it can change anything.
4. **Verify** runs the drafted changes through fidelity verification against sources, repo grounding for task-state claims, a checker pass over the corrections themselves, and a fresh-eyes quality panel. The build refuses any cluster without a recorded, content-bound verification verdict.
5. **Preview** builds a patch set with per-proposal diffs and an HTML review surface. You approve item by item, by token. Reject everything and nothing is written.
6. **Apply** runs a gate stack that refuses rather than guesses: single-flight lock, consent trace, destination confinement, sensitive-content skip, changed-since-draft skip, per-project atomic writes with rollback. Every touched file is snapshotted first; `memory-dream restore` undoes an applied pass.
7. **Measure** with the recall eval: frozen, content-anchored questions routed against the live index before and after. A consolidation that loses to the old index on its own target notes is a build defect, and the pipeline treats it as one.

**Proposal-only is a hard rule, not a mode.** No semantic change applies without your approval. The tool never merges, closes, or compresses a note on its own.

## Using it

```
/plugin marketplace add StartupBros-com/hov-marketplace
/plugin install memory-dream@hov
```

Then, in any project:

```
/memory-dream:dream        # run a consolidation pass (operator-invoked only)
/memory-dream:eval         # freeze a recall suite, score routing accuracy
```

Or drive the CLI directly. It is stdlib-only Python, zero dependencies:

```
python3 -m memory_dream doctor     # preflight: what works on this machine
python3 -m memory_dream triage     # what would a pass even do here?
```

Start with `doctor`. It tells you which mode you are in (snapshot vs mirror), whether the consent trace can see your transcripts, and which caps apply.

## What makes it trustworthy

- **Untrusted input cannot write.** Note bodies are data, never instructions. The drafting agent has no mutating tools, and four independent layers re-check its output: schema validation, path confinement, sensitive-content scan, and your own item-by-item review.
- **Consent is verified, not assumed.** Apply checks the session transcript for a real, post-preview human turn carrying the patch set's approval token. A pipeline that drafts and applies in one automated sequence cannot approve itself.
- **Everything is recoverable.** Apply snapshots every file it touches into the patch set before writing. Optional mirror mode adds git-history recoverability on top.
- **It measures.** The recall eval exists because two of the pipeline's worst defect classes (an accurate description that under-covers its body, a filename that keeps routing to a deleted claim) pass every static check and only show up as lost routes.

Every gate exists because a real defect got through without it. This pipeline ran 12+ supervised passes over the authors' own multi-project corpus before extraction; the defect classes each stage caught are documented in [docs/PROVENANCE.md](docs/PROVENANCE.md).

## Honest limits

- The trust model is a single-operator machine. The consent trace is defense in depth against accidental auto-apply, not cryptographic proof against a compromised orchestrator. Read [SECURITY.md](SECURITY.md) before installing on anything shared.
- The index load cap (200 lines / 25 KiB) was measured against Claude Code v2.1.211 and is not a documented API. `doctor` restates this; [docs/TUNING.md](docs/TUNING.md) covers re-measuring it.
- Thresholds were calibrated on one ~500-note English corpus. They are defaults, not truths, and every one is configurable.

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): the three layers and the apply gate stack, gate by gate.
- [docs/TUNING.md](docs/TUNING.md): every threshold, its provenance, and its failure symptoms.
- [docs/PROVENANCE.md](docs/PROVENANCE.md): the hardening campaign that shaped the pipeline.
- [SECURITY.md](SECURITY.md): trust model, consent modes, what is and is not defended.

## Attribution

Built by [StartupBros / House of Vibe](https://houseofvibe.ai). MIT license.
