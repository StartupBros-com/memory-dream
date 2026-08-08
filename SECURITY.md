# Security

## Trust model

memory-dream is built for **one operator on one machine**. That assumption
runs through every gate described in `docs/ARCHITECTURE.md`, and it's worth
stating explicitly rather than leaving implicit:

- **The operator's own agent harness is trusted.** Claude Code, the shell it
  runs commands in, and whatever process invokes `memory-dream`'s
  subcommands are all part of the operator's own machine and assumed to be
  acting on the operator's behalf.
- **Note bodies are not trusted, ever, at any layer.** A memory note's
  content can contain arbitrary text — including text that reads like an
  instruction ("ignore your previous instructions," "write this to file
  X") — and this pipeline is built around the assumption that some note,
  someday, will contain exactly that, whether by accident or by something
  upstream of memory-dream having been compromised. Every stage that
  touches note bodies treats them as data to summarize or verify, never as
  instructions to follow.

That second assumption is the actual threat this pipeline defends against,
and it's why the architecture looks the way it does:

- **The drafter has no tools.** The subagent that turns note bodies into a
  consolidation proposal cannot Read, Write, run Bash, or dispatch another
  agent. Whatever a note body says, the worst it can do to a zero-tool
  subagent is produce a bad *text* output — never a write, a shell command,
  or a further dispatch.
- **The drafter's output is schema-validated**, not trusted as well-formed
  JSON on faith — a malformed or unexpected shape is a build failure, not a
  best-effort parse.
- **Destination confinement rejects any path escape** in a proposal
  (absolute paths, traversal, symlink/junction escapes, direct index
  writes), regardless of whether that path came from a drafting bug or from
  something adversarial embedded in a note body.
- **Nothing reaches live memory without the operator reviewing the actual
  diff first.** The patch-set preview is the one surface that shows note
  bodies at all outside live memory itself, and apply refuses to run without
  passing the consent gate below.

None of this defends against a malicious *operator* — that's out of scope
by definition, since the operator is the trust anchor. It defends against
malicious or merely broken *content* flowing through a pipeline the operator
otherwise trusts.

## What the consent trace proves, and what it doesn't

The default consent mode, `--consent trace`, requires that a real,
post-preview human transcript turn — occurring in the operator's own Claude
Code session, after the patch-set preview was generated — exists and
carries that specific patch set's approval token (the `id` field in its
manifest). Apply independently recomputes the patch set's content-bound
identifier and checks it against that transcript turn.

**What this proves:** that a human being, in this operator's own session,
typed something containing this exact patch set's token, at some point
after this exact patch set's preview existed. That defeats the two failure
modes this pipeline is actually built to prevent: a fully automated
draft-then-apply sequence with no human step at all (there is no way for a
script to produce a *post-preview human transcript turn* on its own), and
an accidental or careless mis-application, since the token has to belong to
*this* patch set specifically — approving one patch set can't accidentally
or maliciously apply a different one.

**What this does not prove: a cryptographic guarantee that the approval is
genuine.** This pipeline runs as a model with Bash access. If the
orchestrating agent process is itself compromised — for instance, steered
by a prompt injection it consumed earlier in the same session, from a note
body, a fetched page, or any other untrusted content — that agent could, in
principle, fabricate a transcript-shaped record and forge the trace. This is
a real residual, and it's **accepted as a residual, not treated as
solved**, for one concrete reason: an agent with Bash access that is
already compromised to the degree required to forge a transcript could just
as easily write to live memory directly, with no need to go through this
pipeline, its schema validation, or any of its gates at all. Accepting this
residual therefore adds no new attack surface beyond what a Bash-capable
compromised agent already has — the pipeline isn't the thing that would be
making the machine more attackable; a compromised agent already was.

The compensating controls that hold regardless of whether the trace itself
could theoretically be forged: the zero-tool drafter (a note body can't
drive a write even if it tries), schema validation and destination
confinement on every proposal, and the fact that nothing here is the only
line of defense — an operator who actually reads the diff review before
approving catches what an automated forgery cannot anticipate.

## Consent modes

**`--consent trace`** (the default). Described above. Requires a locatable
transcript directory for the current session and a schema match on its
entries; `memory-dream doctor` reports whether both are available from the
current working directory.

**`--consent token --acknowledge-reduced-consent-check`**. Skips transcript
verification entirely. Approval is just the operator-typed token present in
`selection.json` — nothing about *when* or *by whom* that token was typed is
checked. Both flags are required together, deliberately: there is no way to
opt into this mode by accident, and no flag name that reads as anything
other than what it is.

**What token mode gives up, precisely:** proof that a human looked at *this
specific preview* before approving it. In trace mode, forging approval
requires forging a transcript entry inside the operator's own session
store. In token mode, anything capable of writing `selection.json` —
including a malfunctioning script, a misconfigured automation, or a
compromised orchestrator that never needed to forge anything — can produce
a valid approval with no human step at all. This mode exists for two
legitimate reasons: harnesses other than Claude Code have no transcript to
check in the first place, and any future change to Claude Code's transcript
schema should degrade to an explicit, operator-chosen downgrade rather than
a silent, undetected break of the consent gate. Use it only when you
understand you are substituting "a token exists in a file" for "a human
turn happened after seeing the diff," and only with a stronger consent
mechanism of your own already in place around it.

## Shared and team machines

**Out of scope for v0.1, stated plainly.** The entire trust model above
assumes one operator with exclusive control over the machine, its Claude
Code transcript store, and its live memory root. There is no per-user
isolation, no access-control model beyond POSIX owner-only permissions on
scratch and patch-set directories (a no-op on Windows, where the operator's
own ACLs are the only protection), and no defense against a second local
user racing the single-flight lock or simply reading a patch-set preview —
which holds full note bodies in plaintext HTML and JSON by design, since
that's what the operator is meant to review. Do not point memory-dream at a
memory root, or run it on a machine, shared with another party you do not
fully trust with the contents of your memory store.

## Reporting a vulnerability

Report security issues through this repository's GitHub Security
Advisories ("Security" tab → "Report a vulnerability"), not a public issue.
