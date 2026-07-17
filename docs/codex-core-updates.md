# Updating Codex Core

LiteraryGiant keeps Codex as a vendored engine under `core/codex`.

This project has two Git layers:

1. The parent `LiteraryGiant` repository tracks `LiteraryAgent` as a submodule.
2. The nested `LiteraryAgent` repository keeps LG product code and the vendored Codex core together.

`core/codex` is intentionally not a submodule. The `LiteraryAgent` repository started as a Codex fork, then the Codex tree was moved under `core/codex`. Because of that history, the practical update path is a Git subtree merge from upstream Codex into that prefix.

`CODEX_CORE_COMMIT` records the exact upstream commit represented by the current
`core/codex` tree. Product branding and LG workflow code must stay outside that
prefix so its Git tree hash can be compared directly with upstream.

## Current Remotes

Inside `LiteraryAgent`:

```bash
git remote -v
```

Expected:

```text
origin    https://github.com/ZhiHaOuang/LiteraryAgent.git
upstream  https://github.com/openai/codex.git
```

`upstream` is fetch-only for Codex updates. Do not push there.

## Check For Updates

```bash
cd LiteraryAgent
scripts/update_codex_core.sh --check
```

This fetches `upstream/main`, prints recent upstream commits, and does not change files.

Verify that the committed prefix exactly matches its pin:

```bash
scripts/update_codex_core.sh --verify
```

## Apply An Update

Run this only after the current LG migration has been committed and the worktree is clean:

```bash
cd LiteraryAgent
scripts/update_codex_core.sh --apply
```

The script runs:

```bash
git merge -s subtree --no-ff --no-commit upstream/main
```

It leaves the merge uncommitted so the core diff can be reviewed before finishing.
It also updates `CODEX_CORE_COMMIT` and verifies the staged `core/codex` tree
against the fetched upstream tree before allowing an automatic commit.

Then inspect and test:

```bash
git diff --stat -- core/codex
git diff -- core/codex
```

When satisfied:

```bash
git commit -m "Update Codex core from upstream/main"
```

## Conflict Rule

Prefer keeping LG product logic outside `core/codex`. If a conflict touches LG files outside `core/codex`, stop and inspect carefully; a Codex upstream update should normally affect the vendored prefix only.

Do not place LG branding or product behavior in `core/codex`. If a core patch is
unavoidable, maintain it as an explicit patch outside the prefix and document
why it must be replayed after an update.
