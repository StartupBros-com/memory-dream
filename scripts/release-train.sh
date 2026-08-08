#!/usr/bin/env bash
set -euo pipefail

# Announce-only release train for memory-dream.
#
# Posts a Tool Drop card for a published (or edited) GitHub release, but only
# after the hov-marketplace entry already lists this exact release tuple.
# Unlike the pro-gate/token-eater full trains, this variant never writes to
# the marketplace: promotion repins land as reviewed PRs, and this script
# verifies them read-only before announcing. No deploy key, no push access.
#
# Two deliberate soft exits (loud in the run UI, green in the checks list):
#   - marketplace not yet repinned to this release -> notice + exit 0
#     (edit the release after the repin merges to re-fire the announce);
#   - TOOL_RELEASE_ANNOUNCE_SECRET not granted to this repo -> warning +
#     exit 0 (staged rollout: the org secret is scoped per-repo, and a red X
#     on every release before the grant would be worse than a loud warning).

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require() {
  local name="$1"
  [[ -n "${!name:-}" ]] || fail "$name is required"
}

is_uint() {
  [[ "$1" =~ ^(0|[1-9][0-9]*)$ ]]
}

notes_summary() {
  # Card-ready release bullets for the announcement endpoint's "What's new" section.
  # Preference order:
  #   1. An author-written "## Highlights" section in the release body (manual control for
  #      releases that deserve hand-picked copy);
  #   2. GitHub's auto-generated "What's Changed" PR-title bullets, cleaned: conventional-
  #      commit prefixes, "by @user in <url>" tails, and "(vX.Y.Z)" suffixes stripped;
  #   3. Fallback: the body's first paragraph, flattened, so a hand-written prose body
  #      still announces something.
  # Up to 3 bullets, one per line, each <=180 chars; total stays under the endpoint's
  # 600-char schema cap.
  local notes bullets
  notes="$(printf '%s' "${RELEASE_NOTES:-}" | tr -d '\r')"
  [[ -n "$notes" ]] || return 0
  bullets="$(printf '%s\n' "$notes" | sed -n '/^##[[:space:]]*Highlights/,/^## /p' | grep -E '^[*•-][[:space:]]' || true)"
  if [[ -z "$bullets" ]]; then
    if printf '%s\n' "$notes" | grep -qE '^##[[:space:]]*What.?.?s Changed'; then
      bullets="$(printf '%s\n' "$notes" | sed -n '/^##[[:space:]]*What.\{0,2\}s Changed/,/^## /p' | grep -E '^\*[[:space:]]' || true)"
    elif [[ "$(printf '%s\n' "$notes" | grep -vE '^[[:space:]]*$' | head -n 1)" == \** ]]; then
      bullets="$(printf '%s\n' "$notes" | awk '/^[[:space:]]*$/{exit} /^\*[[:space:]]/{print}')"
    fi
  fi
  if [[ -n "$bullets" ]]; then
    # Character-safe slicing: cut -c is BYTES under GNU coreutils, which can split a
    # multibyte character mid-sequence and ship invalid UTF-8. This path runs only on the
    # Actions runner, where python3 is guaranteed.
    printf '%s\n' "$bullets" \
      | sed -E 's/^[*•-][[:space:]]+//; s/[[:space:]]+by @[A-Za-z0-9_[:punct:]]+ in http[^[:space:]]*[[:space:]]*$//; s/^(feat|fix|perf|chore|docs|refactor|test|ci|build)(\([^)]*\))?!?:[[:space:]]*//; s/[[:space:]]*\(v[0-9]+\.[0-9]+\.[0-9]+\)[[:space:]]*$//' \
      | python3 -c 'import sys
out = []
for line in sys.stdin.read().splitlines():
    line = line.strip()
    if line:
        out.append(line[:180])
    if len(out) == 3:
        break
print("\n".join(out)[:600])'
    return 0
  fi
  printf '%s' "$notes" | awk 'BEGIN{RS=""} NR==1' | tr '\n' ' ' | cut -c1-600
}

announce() {
  require ANNOUNCE_URL
  local payload
  payload="$(jq -cn \
    --arg operation announce \
    --arg repository "$REPOSITORY" \
    --arg releaseId "$RELEASE_ID" \
    --arg tag "$RELEASE_TAG" \
    --arg releaseName "$RELEASE_NAME" \
    --arg releaseUrl "$RELEASE_URL" \
    --arg notesSummary "$(notes_summary)" \
    '{operation: $operation, repository: $repository, releaseId: $releaseId, tag: $tag, releaseName: $releaseName, releaseUrl: $releaseUrl} + (if $notesSummary == "" then {} else {notesSummary: $notesSummary} end)')"
  curl --fail-with-body --silent --show-error \
    -X POST \
    -H 'content-type: application/json' \
    -H "x-tool-release-announce-secret: $ANNOUNCE_SECRET" \
    --data "$payload" \
    "$ANNOUNCE_URL"
}

verify_release() {
  require SOURCE_ROOT
  require SOURCE_SHA
  local version manifest_version expected_tag
  version="$(tr -d '[:space:]' < "$SOURCE_ROOT/VERSION")"
  manifest_version="$(jq -er '.version' "$SOURCE_ROOT/.claude-plugin/plugin.json")"
  expected_tag="v$version"
  [[ "$RELEASE_TAG" == "$expected_tag" ]] || fail "release tag $RELEASE_TAG does not match VERSION $version"
  [[ "$manifest_version" == "$version" ]] || fail "plugin manifest version $manifest_version does not match VERSION $version"
  [[ "$(git -C "$SOURCE_ROOT" rev-parse HEAD)" == "$SOURCE_SHA" ]] || fail "checked-out source does not match release commit"
  [[ "$(git -C "$SOURCE_ROOT" rev-list -n 1 "$RELEASE_TAG")" == "$SOURCE_SHA" ]] || fail "release tag does not resolve to the exact release commit"
  printf '%s\n' "$version"
}

marketplace_lists_release() {
  # Read-only promotion check against the marketplace's live default branch.
  local manifest_url manifest
  manifest_url="${MARKETPLACE_MANIFEST_URL:-https://raw.githubusercontent.com/StartupBros-com/hov-marketplace/main/.claude-plugin/marketplace.json}"
  manifest="$(curl --fail --silent --show-error "$manifest_url")" || fail 'could not fetch marketplace manifest'
  jq -e \
    --arg name "$REPOSITORY" \
    --arg sha "$SOURCE_SHA" \
    --arg version "$RELEASE_VERSION" \
    --argjson release_id "$RELEASE_ID" \
    --arg release_tag "$RELEASE_TAG" \
    'any(.plugins[]; .name == $name and .source.sha == $sha and .metadata.version == $version and .metadata.releaseId == $release_id and .metadata.releaseTag == $release_tag)' \
    <<<"$manifest" >/dev/null
}

main() {
  require EVENT_ACTION
  require REPOSITORY
  require RELEASE_ID
  require RELEASE_TAG
  require RELEASE_NAME
  require RELEASE_URL
  is_uint "$RELEASE_ID" || fail "RELEASE_ID must be an unsigned integer"
  [[ "$REPOSITORY" == memory-dream ]] || fail "this release train only announces memory-dream"

  if [[ "${RELEASE_PRERELEASE:-false}" == true || "${RELEASE_DRAFT:-false}" == true ]]; then
    printf 'prerelease or draft release ignored\n'
    return
  fi
  [[ "$EVENT_ACTION" == published || "$EVENT_ACTION" == edited ]] || fail "unsupported release action: $EVENT_ACTION"

  require LATEST_STABLE_ID
  is_uint "$LATEST_STABLE_ID" || fail "LATEST_STABLE_ID must be an unsigned integer"
  if [[ "$RELEASE_ID" != "$LATEST_STABLE_ID" ]]; then
    printf 'release %s is not latest stable %s; no-op\n' "$RELEASE_ID" "$LATEST_STABLE_ID"
    return
  fi

  RELEASE_VERSION="$(verify_release)"

  if ! marketplace_lists_release; then
    printf '::notice title=Not announced::hov-marketplace does not yet list %s %s at %s. Merge the repin PR, then edit the release to re-fire the announce.\n' \
      "$REPOSITORY" "$RELEASE_TAG" "$SOURCE_SHA"
    return
  fi

  if [[ -z "${ANNOUNCE_SECRET:-}" ]]; then
    printf '::warning title=Announce secret not granted::TOOL_RELEASE_ANNOUNCE_SECRET is not scoped to this repository yet; skipping the Tool Drop announcement.\n'
    return
  fi

  announce
}

main "$@"
