# Book assets and browsing

## Quick startup

Run `literary` without arguments to reopen the last book or bookshelf used in
the current environment (sandbox by default). Successful `/focus`, `/open`,
`/newbook` and `/shelf` operations remember the location automatically. The existing
book-local conversation restoration still applies; `/new` starts a fresh discussion.

Explicit `-C` and `--shelf` always take precedence. Starting in another book also
opens that local book, not the remembered one. Empty databases left by early LG
versions in the agent source checkout do not override the remembered book.
Noninteractive commands and piped input never inherit the remembered workspace.

Only location and project identity are saved in
`~/.literarygiant/environments/<environment>/location.json`, never story content
or credentials. A missing/replaced book falls back to its existing bookshelf;
invalid state is ignored. Each environment remembers its own location.

## Resource layout

Classification happens when each validated result is persisted, not after the
workflow finishes. The shared output writer rejects unknown artifact categories
and routes new files (including calls with legacy default output paths) as follows:

| Result | Destination under ReferenceLibrary |
| --- | --- |
| Outline / plot / scene plan | plans/outlines, plans/plots, plans/scenes |
| World / character design | bible/worlds, bible/characters |
| Memory proposals | bible/proposals |
| Critique / consistency report | reviews/reports |
| Completed reference brief | sources/research |
| Prose / revision candidates | drafts/chapters, drafts/candidates |
| Source-validated specialist reviews | analyses |

Each category keeps its own latest pointer. Intermediate specialists persist
useful outputs under that category's stages/run-id/ with a provisional label and
a run metadata link, even if a later stage fails. Runtime checkpoints remain for
recovery. Chat, dry-run and code artifacts are not book reference assets and stay
in runtime directories. Unknown categories fail rather than creating ad hoc folders.
Custom output roots must stay inside the book's ReferenceLibrary and also receive
these category subdirectories. Legacy analysis records remain readable, but new
reports are written to ReferenceLibrary/analyses. No existing assets are deleted.

Creation-time routing verification: 238 regression tests passed in LitIsLand,
followed by 58 targeted tests against an isolated installed wheel (including a
complete outline and an interrupted outline with deterministic model fixtures).
The real CLI dry-run wrote only to `.literarygiant/previews`, with no model call.
The memory-updater skill passed its validator. Wheel SHA-256:
`e3d703f4d0178f96cd043f7b89c9922f435761cdf54f8647995beb8d0c9d8770`.

New books use lazy, book-local resource folders. Existing books are not silently
migrated on open. Close other writers before an explicit migration:

```sh
literary -C /path/to/book project organize
literary -C /path/to/book project organize --apply
```

The first command previews the full file mapping. Apply refuses destination
collisions, changed files and symlinks, and records original paths and SHA-256
hashes in `.literarygiant/assets.json`. Interrupted migrations can resume with
the same command. Files are moved byte-for-byte, not deleted. Empty legacy
directories remain until separately approved for cleanup.

```text
book/
  manuscript/chapters/        Editable chapter mirrors; explicit versioned import
  exports/                   Reading copies (MD/TXT/DOCX)
  ReferenceLibrary/
    sources/                 External research; custom knowledge search scope
    bible/                   Author notes and timeline records
    plans/chapters/           Book and chapter execution plans
    analyses/                Source-bound reviews, candidates and adjudications
    reviews/editorial/       Recorded editorial corrections
    reviews/discussions/     Review challenges and discussion records
    drafts/                  Generated working outputs, never implicitly Canonical
    archive/                 Legacy placeholders and earlier trial exports
  .literarygiant/
    story.sqlite3            Authoritative manuscript, versions and Canonical facts
    conversations/           Restorable project conversations
    conversation-artifacts/  Generated chat response artifacts
    runs/                    Prompts, events and recovery checkpoints
    chapter-sync/            Bidirectional edit baselines
    assets.json              Migration provenance and legacy artifact mapping
```

Folders are created when used. Empty Bible templates are no longer generated.
`ReferenceLibrary/bible` contains supplemental author notes, not a competing
database. `.literarygiant/config.toml`, `agent.toml`, provider runtime and logs
remain runtime assets. Run records are retained: prompts and failed outputs are
necessary for audit/recovery even when they are not reusable book knowledge.

For organized books, KnowledgeGateway only searches `ReferenceLibrary/sources`
as custom external references. Manuscript memory still comes from the database;
specialist history is loaded through version-validated BookAnalysisStore.
Drafts, stale reports, adjudications and archived placeholders must not re-enter
retrieval as independent external evidence. Explicit custom reference paths are
unchanged. Custom memory/output paths are also respected.

Historical conversations and run files retain their original text. RunStore
resolves moved artifact paths through the manifest for show/adopt operations.
Literal paths quoted in old conversations or historical documents are historical
and can be looked up in `assets.json`; they are not replaced with symlinks.

## Reading interface

`/novel` opens a chapter list and dedicated manuscript reader/editor. Use
`/novel chapter-slug` to open a chapter directly, or `literary novel` from the
shell (which also respects the remembered book). E opens editing; chapters with
multiple text parts ask which part to edit, preserving scene boundaries.
Escape or Ctrl-S leaves the editor through a save/continue/discard confirmation.
Unchanged text returns without a new version. Cancelling confirmation continues
editing rather than discarding the buffer.

Confirmed edits atomically update database main text with preserved version
history and `novel-editor` provenance. All chapter document versions are checked;
concurrent changes block the save. Conflicting edits may be kept as a candidate
in ReferenceLibrary/drafts/candidates. External chapter-file edits are never
overwritten; if mirror synchronization fails after a database save, the UI reports
that the database saved successfully and the file still needs reconciliation.
Existing analysis reports become stale when their source changes. The editor
does not call a model, change Canonical facts, or append a model conversation.

`/characters`, `/events`, `/storylines`, `/timeline` and `/browse` remain read-only.
Lists show entry counts; Enter opens detail, scrolling reads the detail, Escape
returns to the list and then the conversation. Detail views distinguish titles,
sections, source quotations and metadata, with a scrollbar and line position.
Analysis details show summary, evidence, findings and explicit adjudications
instead of raw JSON. Command-line browse output uses a compact table; `--json`
retains machine-readable entries. No model request or conversation append occurs.
Initial source validation runs off the UI thread. A browsing session retains its
read-only snapshot when returning from detail; reopen it to refresh external edits.
The unfiltered `/chapter list`, `/analysis list`, `/bible list`, `/timeline list`
and `/foreshadowing list` commands also open these read-only views inside the TUI.
Filtered or JSON command variants retain their command-line behavior.

Auth rows show provider and model ID. Profile names only appear when needed to
distinguish multiple accounts using the same provider/model pair. Keys, endpoints
and protocols are not repeated in the top-level model list.

## Verified migration

BeyondTheTide was explicitly migrated on 2026-09-23: 205 moved files, all 1,012
original files byte-compared against the pre-migration backup, twelve clean
chapter mirrors, sixty-five current analyses and one preserved adjudication.
Forty-two historical run artifacts resolve to their new paths. A real terminal
session verified reading, resizing, scrolling and return/exit without changing
conversations or run IDs. Empty legacy directories still require cleanup approval.

Final regression: 227 native-enabled tests in LitIsLand; 57 targeted tests against
the isolated installed wheel. Wheel SHA-256:
`21ccf17b0b1e678e6eddf18c36436104a9009a8c466636b836d6655288ee59e3`.
