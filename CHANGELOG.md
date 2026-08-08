# Changelog

## v0.1.1 — 2026-08-08

Windows correctness release; no behavior change on POSIX.

- **Fix**: the transcript slug derivation (`transcript.cwd_slug`) now replaces
  Windows drive colons and backslashes, so the derived transcripts directory
  always stays inside `<claude-config-dir>/projects/` on native Windows.
  Previously `doctor` and the consent trace probed a wrong (repo-local) path.
- **Fix**: all file I/O — product and test fixtures — now uses explicit UTF-8
  with LF newlines, matching what Claude Code itself writes. Previously
  Windows wrote locale-encoded (e.g. cp1252) CRLF files that later strict
  UTF-8 reads rejected with `UnicodeDecodeError`.
- **Tests**: the lock-contention test holds the lock via `compat.FileLock`
  (flock on POSIX, msvcrt on Windows) instead of raw `fcntl`, so the whole
  suite imports and runs on Windows; POSIX-only 0700 mode assertions are
  skipped on Windows per `compat.restrict_permissions`'s documented no-op.

## v0.1.0 — 2026-08-08

Initial public release.

- **Two surfaces**: a Claude Code plugin (`/memory-dream:dream`,
  `/memory-dream:eval`) and a standalone stdlib-only Python CLI
  (`python3 -m memory_dream`, or `pipx install .` for a `memory-dream`
  console script). Zero dependencies.
- **`/memory-dream:dream`** — an operator-gated consolidation pass over
  Claude Code auto-memory: deterministic triage and clustering, drafting by a
  zero-tool subagent (note bodies are untrusted input and can never drive a
  write), fidelity/repo-grounding/quality verification, per-proposal diff
  preview with item-by-item approval, and a gated apply with single-flight
  locking, consent-trace verification, per-project atomic writes, snapshot
  backup, and `memory-dream restore`.
- **`/memory-dream:eval`** — a frozen, content-anchored recall-regression
  suite that scores index routing accuracy before and after a pass instead of
  assuming it improved.
- **Security model** (SECURITY.md): single-operator trust model, zero-tool
  drafter, verified post-preview consent, and an explicit opt-in
  reduced-consent token mode for non-Claude-Code harnesses.
- **180-test suite** (stdlib unittest, no network, no model calls) across
  ubuntu/macos/windows × Python 3.10/3.12, plus two CI-enforced hygiene
  guards: stdlib-only imports and no private-harness references.
- **Docs**: architecture (docs/ARCHITECTURE.md), full threshold and tuning
  reference (docs/TUNING.md), and the hardening campaign that shaped the gate
  stack (docs/PROVENANCE.md).
