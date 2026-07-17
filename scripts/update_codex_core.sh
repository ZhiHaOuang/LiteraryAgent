#!/usr/bin/env bash
set -euo pipefail

REMOTE="${LG_CODEX_REMOTE:-upstream}"
REF="${LG_CODEX_REF:-main}"
PREFIX="${LG_CODEX_PREFIX:-core/codex}"
PIN_FILE="${LG_CODEX_PIN_FILE:-CODEX_CORE_COMMIT}"
MODE="check"

usage() {
  cat <<'USAGE'
Update or inspect the vendored Codex core under core/codex.

Usage:
  scripts/update_codex_core.sh [--check]
  scripts/update_codex_core.sh --verify
  scripts/update_codex_core.sh --apply
  scripts/update_codex_core.sh --apply --commit

Environment overrides:
  LG_CODEX_REMOTE=upstream
  LG_CODEX_REF=main
  LG_CODEX_PREFIX=core/codex
  LG_CODEX_PIN_FILE=CODEX_CORE_COMMIT

The script runs from the LiteraryAgent repository, not the parent
LiteraryGiant repository. It uses `git merge -s subtree` because this repo
started as a Codex fork and then moved Codex into core/codex.
USAGE
}

auto_commit=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)
      MODE="check"
      ;;
    --apply)
      MODE="apply"
      ;;
    --verify)
      MODE="verify"
      ;;
    --commit)
      auto_commit=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && git rev-parse --show-toplevel)"
cd "$repo_root"

if [[ ! -d "$PREFIX" ]]; then
  echo "Missing Codex core prefix: $PREFIX" >&2
  exit 1
fi

if [[ ! -f "$PIN_FILE" ]]; then
  echo "Missing Codex pin file: $PIN_FILE" >&2
  exit 1
fi

pinned_sha="$(tr -d '[:space:]' < "$PIN_FILE")"
if ! git cat-file -e "$pinned_sha^{commit}" 2>/dev/null; then
  echo "Pinned Codex commit is unavailable locally: $pinned_sha" >&2
  exit 1
fi

verify_committed_core() {
  if ! git diff --quiet -- "$PREFIX" || ! git diff --cached --quiet -- "$PREFIX"; then
    echo "Cannot verify a dirty Codex prefix: $PREFIX" >&2
    return 2
  fi

  local expected_tree actual_tree
  expected_tree="$(git rev-parse "$pinned_sha^{tree}")"
  actual_tree="$(git rev-parse "HEAD:$PREFIX")"
  if [[ "$expected_tree" != "$actual_tree" ]]; then
    echo "Codex core does not match $PIN_FILE." >&2
    echo "Expected tree: $expected_tree" >&2
    echo "Actual tree:   $actual_tree" >&2
    return 1
  fi
  echo "Codex core verified: $PREFIX == $pinned_sha"
}

verify_index_core() {
  local expected_tree index_root actual_tree
  expected_tree="$(git rev-parse "$1^{tree}")"
  index_root="$(git write-tree)"
  actual_tree="$(git rev-parse "$index_root:$PREFIX")"
  [[ "$expected_tree" == "$actual_tree" ]]
}

if ! git remote get-url "$REMOTE" >/dev/null 2>&1; then
  echo "Missing remote '$REMOTE'." >&2
  echo "Expected something like:" >&2
  echo "  git remote add upstream https://github.com/openai/codex.git" >&2
  exit 1
fi

echo "LiteraryAgent repo: $repo_root"
echo "Codex prefix:       $PREFIX"
echo "Pinned commit:      $pinned_sha"
echo "Codex upstream:     $REMOTE/$REF ($(git remote get-url "$REMOTE"))"
echo

git fetch "$REMOTE" "$REF"
remote_ref="$REMOTE/$REF"
remote_sha="$(git rev-parse "$remote_ref")"
head_sha="$(git rev-parse HEAD)"

echo "Current HEAD:       $head_sha"
echo "Upstream HEAD:      $remote_sha"
if git merge-base --is-ancestor "$pinned_sha" "$remote_ref"; then
  echo "Upstream commits:   $(git rev-list --count "$pinned_sha..$remote_ref") ahead of pin"
else
  echo "Upstream relation:  pin is not an ancestor of $remote_ref"
fi
echo
echo "Recent upstream commits:"
git log --oneline --decorate -5 "$remote_ref"
echo

dirty=0
if ! git diff --quiet || ! git diff --cached --quiet; then
  dirty=1
  echo "Working tree has uncommitted changes."
  echo "Commit or stash them before applying a Codex core update."
  echo
fi

if [[ "$MODE" == "verify" ]]; then
  verify_committed_core
  exit $?
fi

if [[ "$MODE" == "check" ]]; then
  if git diff --quiet -- "$PREFIX" && git diff --cached --quiet -- "$PREFIX"; then
    verify_committed_core
  else
    echo "Core verification deferred until the current migration is committed."
  fi
  echo
  echo "Check complete. No files changed."
  echo
  echo "When the LiteraryAgent migration is committed, update core/codex with:"
  echo "  scripts/update_codex_core.sh --apply"
  echo
  echo "After resolving conflicts and running tests:"
  echo "  git diff -- core/codex"
  echo "  git commit -m \"Update Codex core from $REMOTE/$REF\""
  exit 0
fi

if [[ "$dirty" -ne 0 ]]; then
  exit 2
fi

verify_committed_core

echo "Applying subtree merge into $PREFIX..."
git merge -s subtree --no-ff --no-commit "$remote_ref"

printf '%s\n' "$remote_sha" > "$PIN_FILE"
git add "$PIN_FILE"

if ! verify_index_core "$remote_sha"; then
  echo "Updated core tree does not match upstream $remote_sha; aborting commit." >&2
  echo "The merge is still present for inspection." >&2
  exit 1
fi

echo
echo "Subtree merge applied but not committed."
echo "Updated $PIN_FILE to $remote_sha."
echo "The staged Codex tree matches the pinned upstream tree."
echo "Review the diff and run tests before committing."
echo
echo "Suggested checks:"
echo "  git diff --stat -- $PREFIX"
echo "  git diff -- $PREFIX"

if [[ "$auto_commit" -eq 1 ]]; then
  git commit -m "Update Codex core from $REMOTE/$REF"
  echo "Committed Codex core update."
else
  echo
  echo "To finish:"
  echo "  git commit -m \"Update Codex core from $REMOTE/$REF\""
fi
