---
name: memory-dream
description: Reach for this when a project's auto-memory has accumulated rot a consolidation pass would catch, or when you want to measure whether a memory change actually improved recall.
---

# memory-dream: the sleep cycle for Claude Code auto-memory

Auto-memory grows monotonically. Sessions write notes and nothing ever
consolidates them, so notes supersede each other and both survive, descriptions
go stale and route future sessions to wrong answers, long logs bury the one
durable lesson, and eventually `MEMORY.md` outgrows the index load cap and the
newest notes stop routing at all. memory-dream is the consolidation pass that
was missing — and the eval that proves the pass helped rather than assuming it.

**This skill orients; it does not act.** The work lives in two commands, each
operator-gated by design. When the situation below appears, surface the right
command to the operator and let them run it — consolidating memory by hand, or
calling the `apply` step directly, bypasses every gate the pass exists for.

## Reach for a command

- **`/memory-dream:dream`** — a full consolidation pass over live memory:
  deterministic triage finds the rot, a zero-tool subagent drafts fixes it
  cannot apply, the operator approves a diff item by item, and only approved
  changes are written (every one recoverable). Suggest this when triage would
  find flagged notes — its checks are mechanical: supersession markers,
  oversized bodies, decayed confidence, stale-dated content, or an index over
  the load cap (which the pass's archive tier demotes rather than deletes).
- **`/memory-dream:eval`** — a read-only recall-regression eval: it freezes a
  content-anchored question suite over the live corpus and scores whether a
  fixed judge, seeing only `MEMORY.md` descriptions, routes each question to a
  note that answers it. Suggest this to baseline recall before a pass and to
  measure the after delta, or any time the question is "did a memory change
  actually improve recall?"

Both commands are `disable-model-invocation: true`: the operator invokes them,
this skill only points the way.

## The invariant that makes it safe

Every semantic change is proposal-only. The pass drafts, verifies, previews,
and applies solely what the operator approved by token, item by item — it treats
note bodies as untrusted data throughout, and the drafting subagent has no
mutating tools, so a prompt injection inside a note cannot drive a write. Apply
snapshots every touched file first, and `memory-dream restore` reverses a pass.
Read [SECURITY.md](../../SECURITY.md) for the trust model before installing on a
shared machine.

## Beyond the plugin

memory-dream is also a standalone, stdlib-only Python CLI that works from a
clone alone — useful for a dry run before installing the plugin, or for driving
the pipeline outside Claude Code:

```bash
python3 -m memory_dream doctor     # preflight: what works on this machine
python3 -m memory_dream triage     # what a pass would find here, read-only
```

Start with `doctor`; it reports the operating mode, whether the consent trace
can see your transcripts, and which caps apply. Full documentation, the gate
stack, and every tunable threshold are in the repository
[README](../../README.md) and [docs/](../../docs).
