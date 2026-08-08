# memory-dream: OSS extraction design (v0.1.0)

Target: a community-grade Claude Code plugin on the hov marketplace, extracted
from a personal harness. Source of record for the port: the coupling audit in
dotfiles issue #111 (50 findings, 19 blocking) plus per-file coupling maps
generated 2026-08-08.

## What this is

Claude Code auto-memory grows monotonically: notes accrete, supersede each
other, and rot, and nothing in the product consolidates them. memory-dream is
the missing sleep cycle: an **operator-gated consolidation pass** over
per-project memory stores, plus a **recall eval** that measures whether a pass
actually improved retrieval instead of assuming it.

Pipeline (one pass): deterministic triage → deterministic clustering → drafting
by a **zero-tool subagent** (note bodies are untrusted input and can never
drive a write) → fidelity/grounding/quality verification stages → deterministic
patch-set build → HTML preview + item-by-item operator approval → gated apply
that refuses rather than guesses → snapshot-backed recovery.

## Repo layout

```
memory-dream/
  .claude-plugin/plugin.json      # name: memory-dream
  commands/
    dream.md                      # /memory-dream:dream — the consolidation pass
    eval.md                       # /memory-dream:eval  — the recall eval
  agents/
    drafter.md                    # zero-tool consolidation drafter (memory-dream:drafter)
    scribe.md                     # zero-tool eval writer/judge     (memory-dream:scribe)
  memory_dream/                   # real Python package, stdlib-only
    __init__.py                   # __version__
    __main__.py                   # python3 -m memory_dream
    cli.py                        # single argparse entry; also runnable as a file
    config.py                     # every path + threshold, one resolution order
    compat.py                     # POSIX/Windows shims: file lock, chmod
    transcript.py                 # consent-trace adapter (Claude Code JSONL backend)
    audit.py                      # from memory-audit.py
    assemble.py                   # from memory-dream-assemble.py
    apply.py                      # from memory-dream-apply.py
    recall_eval.py                # from memory-recall-eval.py
  templates/
    fidelity-prompt.md            # canonical per-file fidelity-verifier prompt
    routing-prompts.json          # 3 decorrelated judge prompt variants
  tests/                          # unittest, no model calls, no network
  scripts/
    check_stdlib_only.py          # CI: AST check, no third-party imports
    check_no_private_refs.py      # CI: sanitization regression guard
  docs/
    ARCHITECTURE.md               # layers + the gate stack, why each gate exists
    TUNING.md                     # every threshold: default, provenance, symptoms
    PROVENANCE.md                 # the 12-pass hardening campaign, anonymized
    EXTRACTION-DESIGN.md          # this file
  .github/workflows/test.yml     # ubuntu/macos/windows × py3.10/3.12
  README.md  SECURITY.md  LICENSE(MIT)  VERSION  pyproject.toml  .gitignore
```

## Module mapping and required changes

Shared rules for every ported module:
- Underscore module names, normal `from memory_dream import audit as AUDIT`
  imports. Delete every `importlib.util.spec_from_file_location` sibling loader.
- No module-level work that can fail (the old `AUDIT = load_auditor()` at
  import time broke `--help`); import of siblings is fine, filesystem probing
  at import time is not.
- Every path and threshold comes from `config.py`. No `Path(__file__)`-relative
  data-directory derivation anywhere (the old grandparent mirror default
  silently repointed under worktrees — dotfiles pass 9 incident).
- No personal references: see "Sanitization contract" below.
- Keep the machine-parseable output contracts: the trailing `flagged:N` triage
  line and the `DREAM-APPLY-COMPLETE ...` completion line are documented API.

### audit.py (from memory-audit.py, 1455 lines)
- Subcommands preserved: full audit (default), `triage`, `fix [--apply]`.
- Mirror handling: `--mirror-root` default becomes **None** (feature off).
  All `mirror_*` finding classes and the fix-apply freshness gate run only when
  a mirror root is configured. Without one, `fix --apply` writes a pre-fix
  snapshot (same snapshot helper as apply.py) before touching files.
- Decay constants (0.3 threshold, 90-day half-life) deduplicated into named
  config values (they are inline literals at 3 call sites today).
- Index-cap accounting (200 lines / 25 KiB, HTML-comment stripping) stays, with
  the version caveat surfaced in `doctor` and TUNING.md: measured against
  Claude Code v2.1.211, configurable via config.
- Tokenizer: word tokenization becomes Unicode-aware (`\w+` with re.UNICODE);
  note in TUNING.md that Jaccard thresholds were calibrated on an English corpus.
- Orphaned `*.dream-tmp` staging files: `doctor` and the full audit warn about
  them (crash-recovery leftovers).

### assemble.py (from memory-dream-assemble.py, 1420 lines)
- Subcommands preserved: `plan`, `build`, `archive`, `trace`.
- `trace` moves its transcript parsing to `transcript.py` (shared with apply).
- Scratch defaults (`$CLAUDE_JOB_DIR/tmp`) → `config.scratch_dir()`.
- Patch-set root default: `~/.claude/logs/memory-dream/passes/` (respecting
  `CLAUDE_CONFIG_DIR`), overridable.
- `os.chmod(0o700)` via `compat.restrict_permissions` (no-op on Windows, keeps
  POSIX behavior).
- All Jaccard/caps/suppression constants from config.

### apply.py (from memory-dream-apply.py, 667 lines)
- Flat CLI preserved (`--patch-set --selection --transcript --preflight ...`).
- **Backup provider abstraction** (the mirror-hard-dependency fix, gate 3):
  - Default (no mirror configured): before any write, snapshot every affected
    file plus each project's MEMORY.md into `<patch-set>/backup/<project>/…`,
    then apply. New `restore` subcommand reverses an applied patch set from
    that snapshot (per-project, atomic, refuses on digest mismatch with
    `--force` escape).
  - Mirror mode (mirror root configured): today's per-project freshness gate,
    verbatim behavior. Remediation message names the user's own configured
    sync command (config `mirror_push_hint`, default text "sync your mirror"),
    never `sync.sh memory-push`.
- **Consent modes** (the trace-coupling fix, gate 2):
  - `--consent trace` (default): today's post-preview transcript-turn
    verification via `transcript.py`.
  - `--consent token --acknowledge-reduced-consent-check`: skips transcript
    verification; approval is the operator-typed token in `selection.json`
    alone. Both flags required together; SECURITY.md documents exactly what is
    lost. This exists for non-Claude-Code harnesses and future transcript
    schema drift — the failure mode must be a loud, explicit downgrade, never
    silent breakage.
- `fcntl` single-flight lock via `compat.FileLock` (fcntl on POSIX,
  msvcrt.locking on Windows).
- The completion line keeps its shape; `next=memory-push` becomes
  `next=<config mirror_push_hint or "none">`.

### recall_eval.py (from memory-recall-eval.py, 635 lines)
- Subcommands preserved: `sample`, `freeze`, `routing-input`, `score`,
  `discriminability`.
- Eval-home defaults (`~/.claude/logs/memory-eval/…`) → config
  (`~/.claude/logs/memory-dream/eval/`), env-overridable.
- Writes become atomic (reuse audit's atomic-write helper); routes files gain a
  `schema_version` check that errors loudly on mismatch.
- Sensitive-filename redaction regexes move to config with a documented
  `sensitive_patterns_extra` extension point.

### New modules
- **config.py**: resolution order flag > env (`MEMORY_DREAM_*`,
  `CLAUDE_MEMORY_LIVE_ROOT`, `CLAUDE_MEMORY_MIRROR_ROOT` kept for compat) >
  optional JSON config at `<claude-config-dir>/memory-dream.json` > defaults.
  `<claude-config-dir>` = `$CLAUDE_CONFIG_DIR` or `~/.claude`. Exposes:
  live_root, mirror_root (None default), pass_root, eval_home, scratch_dir()
  (env → `$CLAUDE_JOB_DIR/tmp` → `tempfile.mkdtemp(prefix="memory-dream-")`),
  mirror_push_hint, and every threshold. A dataclass; every CLI wires
  `add_config_args(parser)` / `Config.from_args(args)`.
- **compat.py**: `FileLock` (context manager; fcntl.flock POSIX /
  msvcrt.locking Windows), `restrict_permissions(path)` (chmod 0700 POSIX,
  no-op Windows), `is_reparse_or_symlink(path)` (symlink check that also
  catches Windows junctions where detectable).
- **transcript.py**: Claude Code JSONL backend. `locate(cwd)` implements the
  cwd→slug derivation (documented as reverse-engineered, with a probe that
  verifies the derived directory actually exists); `extract_user_text(entry)`
  with a schema probe — if an entry doesn't match the known shape, raise a
  descriptive error naming the expected schema instead of silently returning
  nothing (the old behavior silently broke the consent gate on schema drift).
- **cli.py**: `memory-dream <sub>` with subcommands `audit`, `triage`, `fix`,
  `plan`, `build`, `archive`, `trace`, `apply`, `restore`, `eval <sub>`,
  `doctor`, `scratch` (prints resolved scratch dir for skill snippets),
  `open-preview --patch-set DIR` (platform-appropriate browser open including
  the WSL copy-to-Windows-home dance, ported out of the old skill's shell).
  Runnable three ways: console script, `python3 -m memory_dream`, and
  `python3 <path>/memory_dream/cli.py` (sys.path bootstrap when run as file —
  the plugin invocation path).
- **doctor** checks: Python ≥ 3.10, live root exists and holds ≥1 project,
  transcript dir locatable + schema probe result, mirror configured/fresh (or
  "off — snapshot mode"), lock capability on this platform, orphaned
  `*.dream-tmp` files, index caps in effect (with the version-measured caveat),
  git/gh presence (optional features), scratch dir writability.

## Commands and agents (markdown ports)

- `commands/dream.md` from `skills-local/memory-dream/SKILL.md` (615 lines).
  `disable-model-invocation: true`, `argument-hint` kept. Every script call
  becomes `python3 "${CLAUDE_PLUGIN_ROOT}/memory_dream/cli.py" …`. Drafters
  dispatch `subagent_type: memory-dream:drafter`. The sync.sh/mirror section
  becomes a "Record" step with two documented modes (snapshot default / mirror
  optional). The memory-mine staleness preflight becomes a generic conditional
  ("if you run a capture pipeline, check its freshness first") with no script
  call. Step 0 baseline points at `/memory-dream:eval`. The preview-open shell
  block is replaced by `cli.py open-preview`. Transcript location uses
  `cli.py transcript-locate` (in transcript.py via cli). R#/KTD#/AE# codes
  replaced by gate names (below). Private PR numbers, pass labels, and project
  codenames removed; dated lessons keep their dates, attributed to "the
  authors' consolidation campaign" (see docs/PROVENANCE.md).
- `commands/eval.md` from `skills-local/memory-eval/SKILL.md` (184 lines):
  same treatment; scribe dispatch `subagent_type: memory-dream:scribe`; all
  artifact paths via config defaults, shown as commands.
- `agents/drafter.md` from `memory-dream-drafter.md`: content essentially
  verbatim (it is already portable); drop shorthand codes; keep `model: sonnet`
  as a suggested-default with a one-line compatibility note.
- `agents/scribe.md` from `memory-eval-scribe.md`: verbatim modulo naming;
  keep the deliberate single-dummy-tool (`Glob`) grant and its explanation.
- `templates/fidelity-prompt.md`: port; replace "stage 3.5/3.6" numbering with
  stage names ("fidelity verification", "repo grounding").

## Gate names (replaces R#/KTD#/AE# shorthand)

| Old | Public name |
|-----|-------------|
| R3  | schema validation |
| R6  | operator preview + item-by-item approval |
| R7  | mirror freshness gate (optional mode) / snapshot backup (default) |
| R8  | active-session warning |
| R9/R10/R14 | mirror record + git-recoverable deletions (mirror mode only) |
| R13 | source-changed-since-draft skip |
| R15 | post-build audit dry-run |
| R16 | inbound-wikilink retargeting |
| R17 | consent trace (post-preview approval-turn verification) |
| R18 | destination confinement |
| R19 | zero-tool drafter |
| findings gate | verification-coverage gate (`--findings` + `drafts_digest` content binding) |

GLOSSARY note lives in ARCHITECTURE.md; the public docs use only the names.

## Sanitization contract (CI-enforced)

`scripts/check_no_private_refs.py` fails CI if any shipped file (everything
except `docs/EXTRACTION-DESIGN.md`) matches:
`/home/will`, `~/dotfiles`, `dotfiles`, `sync.sh`, `memory-push`,
`wsl-cdp|prbot|pi-evals|arena|startupbros-com/hov` (codenames),
`\bR1?[0-9]\b` in gate context (shorthand), `harness-weekly`, `CLAUDE_JOB_DIR`
(allowed only in config.py scratch resolution + its docs), private PR/issue
refs (`#\d+` outside CHANGELOG context), `memory-mine`.
Personal dates on measured lessons are kept deliberately; they are provenance.

## Tests

Port both suites to `tests/` with package imports and subprocess calls via
`[sys.executable, "-m", "memory_dream", …]` (cwd=repo root). Keep the mirror
tests as mirror-mode tests. New coverage: snapshot backup + restore round-trip,
consent token mode (both flags required; trace mode unaffected), transcript
schema-probe loud failure, config resolution order, doctor exit codes,
compat.FileLock on the running platform, cli-as-file invocation. CI runs
ubuntu/macos/windows × 3.10/3.12, plus check_stdlib_only and
check_no_private_refs. No test calls a model or the network.

## Versioning / marketplace

VERSION + plugin.json version bumped together (hand commit), tag `v0.1.0`,
`gh release create`. hov-marketplace entry pins URL + 40-char SHA; the
marketplace PR must also add the repo URL to `expected_source_url()` in
`scripts/validate-marketplace.sh`. Publication (public repo + tag) is
operator-owned; the marketplace PR stays draft until then.

## Out of scope for v0.1 (documented in README roadmap)

Scheduled headless drafting; cross-project merges; non-Claude-Code memory
layouts beyond the config roots; a second consent backend; Windows-native
browser preview polish beyond `open-preview`'s best effort; swapping the
authors' private harness to consume this plugin (tracked separately).
