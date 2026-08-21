# Tuning

Every value in this document lives in `memory_dream/config.py` and is read as
`config.NAME` at the point of use, never imported by value — so overriding it
at any of the three tiers below actually takes effect, including in tests
that patch `config` directly.

**Resolution order, highest priority first** (this is `config.py`'s own
documented contract): a subcommand's own CLI flag, when that subcommand
exposes one for this value; the environment variable `MEMORY_DREAM_<NAME>`;
the JSON config file at `<claude-config-dir>/memory-dream.json` (key is the
constant's name, lowercased — e.g. `triage_body_bytes`); the default in the
table below. `<claude-config-dir>` is `$CLAUDE_CONFIG_DIR` or `~/.claude`. An
unknown key in the JSON file is a hard error at startup, not a silent no-op.

**Read this before trusting any default blindly:** most of these were
calibrated once, on one corpus, at one point in time. They are defaults, not
truths. Two categories deserve explicit distrust:

- **The index caps (`INDEX_LOAD_MAX_LINES`, `INDEX_LOAD_MAX_BYTES`) are
  version-measured, not corpus-measured.** They describe Claude Code's own
  per-project memory-load accounting as observed against the Claude Code
  version recorded in `config.COMPATIBILITY_RECORD`. That accounting is not
  a documented, versioned API of Claude Code — it can drift on a CLI
  upgrade without notice. `memory-dream doctor` shells out to `claude
  --version` and compares it against the record itself now, reporting drift
  as an advisory; re-measure if it reports a mismatch.
- **The Jaccard thresholds (`MERGE_JACCARD`, `SIBLING_DESC_JACCARD`) are
  English-corpus-calibrated.** The tokenizer underneath them is
  Unicode-aware (`\w+` with `re.UNICODE`), so it will not crash or silently
  drop non-Latin text — but the *threshold values* were never re-tuned
  against a non-Latin corpus. Word-boundary density and overlap statistics
  behave differently across scripts (CJK text with no whitespace-separated
  words, for instance), so treat these two defaults as a starting point to
  re-measure, not a fact, on a non-English corpus.

## Triage

| Name | Default | Controls | Provenance | Too low | Too high | Override |
|---|---|---|---|---|---|---|
| `TRIAGE_BODY_BYTES` | 6000 | Body size that flags a note as oversized. | Calibrated on the authors' ~500-note English corpus, 2026-07. | Flags notes that aren't really hard to read yet; triage nags at non-problems. | Genuinely bloated notes never get flagged; rot accumulates unseen. | Env/config only (no dedicated flag). |
| `TRIAGE_BODY_BYTES_LARGE` | 10000 | Stronger ranking boost past this size (never flags alone). | Same corpus. | Moderately large notes get boosted before they're real outliers. | Truly huge notes don't rise to the top of triage output, competing poorly for limited per-pass cluster slots. | Env/config only. |
| `TRIAGE_AGE_DAYS` | 90 | mtime age that boosts ranking (never flags alone). | Same corpus; matches the 90-day decay half-life used elsewhere. | Barely-aged notes get boosted, crowding out genuinely stale ones. | Months-old orphan notes never surface via age alone. | Env/config only. |
| `TRIAGE_AGE_DAYS_OLD` | 180 | Stronger age-based ranking boost. | Same corpus. | Same direction as above, at the stronger tier. | Same direction as above, at the stronger tier. | Env/config only. |
| `TRIAGE_MAX_CLUSTERS` | 12 | Per-pass cluster cap; overflow goes to `deferred`. | Corpus + operational (drafting cost scales per cluster). | Real candidates pile up in `deferred` pass after pass. | A pass dispatches more drafting and verification subagents than one review sitting can absorb — every later stage's cost scales with this number. | `memory-dream plan --max-clusters`, env, or config. |
| `TRIAGE_MAX_NOTES_PER_CLUSTER` | 8 | Per-cluster note cap. | Corpus + drafting-prompt-size limit (note bodies go inline in the drafter prompt). | Topics that legitimately span more notes get split arbitrarily across cluster boundaries. | A single drafter prompt balloons with many bodies, raising both cost and the odds a fidelity verifier misses something in a larger diff. | `memory-dream plan --max-notes`, env, or config. |
| `TRIAGE_DESC_MIN_WORDS` | 5 | Descriptions shorter than this are flagged vague. | Same corpus. | Genuinely non-descriptive one/two-word descriptions stop getting flagged. | Legitimately terse, specific descriptions get flagged as vague, adding noise. | Env/config only. |
| `SUPPRESS_APPLIED_DAYS` | 14 | Suppresses re-flagging notes a recent pass just touched. | Corpus + operational (avoids re-litigating the same notes pass after pass). | A note just consolidated shows up flagged again next run, wasting drafting effort re-verifying it. | A note a previous pass mishandled, or that kept decaying, doesn't resurface for a long stretch. | `memory-dream triage --suppress-applied-days`, env, or config. |
| `SUPPRESS_REJECTED_DAYS` | 14 | Suppresses re-flagging notes whose proposal the operator recently rejected (read from `rejections.json`). | Mirrors `SUPPRESS_APPLIED_DAYS`'s operational rationale: a declined proposal should not be re-drafted the very next pass. | A proposal the operator just declined gets re-flagged and re-drafted immediately, re-litigating the same decision. | A note rejected "for now" stays invisible long after the operator would welcome it back into triage. | `memory-dream triage --suppress-rejected-days`, env, or config. |

## Index budget

| Name | Default | Controls | Provenance | Too low | Too high | Override |
|---|---|---|---|---|---|---|
| `INDEX_LOAD_MAX_LINES` | 200 | Loader-visible index line cap. | **Version-measured** against the Claude Code version in `config.COMPATIBILITY_RECORD`'s load accounting — not a documented API, re-verify after CLI upgrades. | Over-conservative refusals of legitimate index growth if the real cap is actually higher. | Notes silently fall outside what a session actually loads while this tool reports the index as healthy, if the real cap shrank. | Env/config only; `memory-dream doctor` compares the installed CLI version against the record and reports drift. |
| `INDEX_LOAD_MAX_BYTES` | 25600 (25 KiB) | Loader-visible index byte cap. | Same as above. | Same as above. | Same as above. | Env/config only. |
| `INDEX_BUDGET_FRACTION` | 0.7 | Fraction of the load cap that trips an early repo-hygiene warning. | Corpus-calibrated. | Warnings fire with plenty of headroom left; becomes noise. | Warning doesn't fire until the index is nearly or already at the hard cap, leaving no runway to fix it. | Env/config only. |
| `AUDIT_MAX_INDEX_BYTES` | 32768 | Repo-hygiene audit ceiling (distinct from the loader cap — roomier, informational). | Corpus-calibrated. | Audit nags well before the loader cap actually matters. | Audit stays quiet on an index already well past the load cap. | `memory-dream audit --max-index-bytes`, env, or config. |
| `AUDIT_MAX_INDEX_LINES` | 400 | Same, for line count. | Corpus-calibrated. | Same direction as above. | Same direction as above. | `memory-dream audit --max-index-lines`, env, or config. |
| `AUDIT_STALE_DAYS` | 90 | Age threshold for the audit's stale-content finding class. | Corpus-calibrated. | Recently-touched notes flag as stale prematurely. | Genuinely stale notes escape the finding entirely. | `memory-dream audit --stale-days`, env, or config. |

## Drafting and build validation

| Name | Default | Controls | Provenance | Too low | Too high | Override |
|---|---|---|---|---|---|---|
| `MERGE_JACCARD` | 0.5 | Near-duplicate detection for merge candidates (Jaccard over tokenized words). | English-corpus-calibrated, 2026-07 — see the caveat above on non-Latin text. | More pairs cross the threshold, producing false-positive merge suggestions that would collapse genuinely distinct notes. | Real near-duplicates never cross it; duplication rot accumulates unproposed. | Env/config only. |
| `SIBLING_DESC_JACCARD` | 0.6 | Build rejects a split's sibling descriptions when they're *more* similar than this. | English-corpus-calibrated — same non-Latin caveat. | Build rejects genuinely distinct sibling descriptions that happen to share vocabulary. | Build lets through siblings whose descriptions are too similar to route distinctly, recreating the discoverability problem the split was meant to fix. | Env/config only. |
| `MAX_SPLIT_EXTRACTS` | 6 | Cap on new notes one `split` proposal can produce. | Design choice (reviewability limit), not corpus-fit. | A note holding genuinely more than 6 distinct durable topics can't be split cleanly in one pass. | A single split can fragment a note into many thin, barely-distinct extracts. | Env/config only. |
| `ENTRY_DATE_SCAN_BYTES` | 4000 | Bytes of a note-body head scanned for dates when judging an archive candidate. | Corpus-calibrated (typical note length where relevant dates cluster near the top). | A note whose latest date sits deeper than the window reads as undated and stays hot even when settled. | Scanning more of a long note risks picking up an incidental old date and misjudging an active note as archivable. Undated notes deliberately stay hot (cold can't be proven), so a too-small window is the safer failure direction. | Env/config only. |

## Decay (mined and drafted notes carrying confidence/last_validated frontmatter)

| Name | Default | Controls | Provenance | Too low | Too high | Override |
|---|---|---|---|---|---|---|
| `DECAY_HALF_LIFE_DAYS` | 90.0 | Half-life for confidence decay. | Corpus-calibrated. | Confidence drops toward the flag threshold fast, forcing needless revalidation churn on notes still fresh in every other sense. | Genuinely stale notes keep reading as high-confidence for a long stretch, so consolidation never picks them up for revalidation. | Env/config only. |
| `DECAY_FLAG_THRESHOLD` | 0.3 | Effective confidence below this flags the note (`decayed_confidence`). | Corpus-calibrated. | Barely-decayed notes flag, adding noise. | Notes must decay a long way before flagging; stale content sits unflagged. | Env/config only. |
| `NEW_EXTRACT_CONFIDENCE` | `"0.8"` | Confidence stamped into new extracts at build. | Design choice, not a measured statistic — a starting value for freshly-verified content. | A lower value decays freshly-verified extracts toward the flag threshold almost immediately, re-flagging content that just passed every verification stage. | A value high enough to never get revisited, even though a dream-pass extract is exactly as fallible as any mined note. | Env/config only. |
| `NEW_EXTRACT_MATURITY` | `"candidate"` | Maturity frontmatter tag stamped into new extracts. | Design choice; a lifecycle label, not a numeric threshold. | N/A — changing it only relabels new extracts for downstream tooling that reads maturity. | N/A | Env/config only. |

## Apply

| Name | Default | Controls | Provenance | Too low | Too high | Override |
|---|---|---|---|---|---|---|
| `ACTIVE_WINDOW_SECONDS` | 900 (15 min) | Sibling-session activity warning window at apply preflight. | Corpus/operational heuristic for session overlap. | A genuinely-overlapping session that touched files just outside the window gets no warning, so two sessions can still race. | Apply warns about "another active session" long after it actually finished, becoming a false alarm the operator learns to ignore. | `memory-dream apply --active-window-seconds`, env, or config. |
| `PATCH_SET_RETENTION_DAYS` | 90 | Advisory retention horizon: `memory-dream doctor` flags patch-set directories under `config.pass_root()` older than this (count and total size) and separately flags a leftover WSL preview-copy; pruning itself is still operator-owned (no automatic delete ships in this package). | Corpus/operational, matches the 90-day cadence used elsewhere. | The doctor advisory flags patch sets that aren't genuinely overdue yet, nagging before the operator would actually consider them stale. | Genuinely stale patch sets pile up on disk longer before the doctor advisory ever surfaces them. | Env/config only. |

## Sensitive-content and mirror text

| Name | Default | Controls | Provenance | Notes | Override |
|---|---|---|---|---|---|
| `SENSITIVE_PATTERNS_EXTRA` | `[]` | Regex strings matched against note *filenames*, extending (never replacing) the built-in generic secret-shape patterns (PEM headers, `ghp_`/`sk_live`/`sk_test` prefixes). | None — empty by default, entirely operator-supplied. | Not a scalar threshold with a too-high/too-low direction: leaving it empty means filename patterns specific to your own environment (internal project names, hostnames) are invisible to the sensitive-skip gate at apply. | JSON config file key `sensitive_patterns_extra` only. |
| `MIRROR_PUSH_HINT` | `"sync your mirror, then retry"` | Remediation text shown inside the mirror-freshness-refusal message in mirror mode. | None — placeholder text meant to be replaced with your own sync command. | Leaving the default just gives operators running their own sync tooling a generic instruction instead of their actual command — a papercut, not a correctness issue. | JSON config file key `mirror_push_hint` only (no environment-variable form). |
