# Terminal Interface

`literary` keeps one full-screen terminal application open throughout the session.
The message region scrolls above the composer; the input, lower rule, and model
status remain at the bottom during command execution and terminal resizing.

## Commands

Type `/` to show the command menu immediately above the input. Keep typing to
filter. Up/Down select an entry; Tab completes it. Enter completes a partial entry
first, then submits the completed command. Command groups such as `/run` expand
their subcommands. Escape dismisses the menu without discarding input.

The catalog is generated from the CLI parser, including nested commands, so the
menu does not invent unsupported operations. `/model` opens model/profile settings;
`/auth` opens the existing credential manager. `/help` lists the root commands.
`/clear` clears only the displayed transcript, not saved workflow data or history.

## Input and Output

- Enter submits; Alt+Enter or Ctrl+J inserts a newline.
- Bracketed multi-line paste does not submit its individual lines.
- Up/Down scroll dialogue by three lines, not recalled commands; inside menus they
  select entries. Mouse wheel over the transcript scrolls dialogue too.
- PageUp/PageDown scroll the displayed message history by half a screen.
- New output does not pull you back to the bottom while reading older messages.
- Input is locked while a command is running; Ctrl+C cancels the command.
- Ctrl+C while idle clears input; Ctrl+D with empty input or `/exit` closes LG.
- The on-screen transcript retains its latest 250,000 characters. Durable workflow
  artifacts and input history remain in the project directory.

Auth menus and inputs replace the composer at the bottom of the same application.
The status header stays above a session-only operation transcript. Passwords are
masked, never appended to the transcript or input history, and cleared from the
input buffer after submission or cancellation. Standalone `literary auth` also
uses an alternate screen; closing it restores the external terminal rather than
leaving menus in its scrollback. Device-login output stays inside the TUI.

External-editor and native-terminal commands temporarily suspend the UI because
they explicitly hand control to another interactive program. Other commands run
as child CLI processes without a shell. Writing workflows use JSONL events to
update an animated progress line and display the final answer as Markdown,
without mixing context logs, artifact paths, or model-internal payloads into prose.
This is stage progress, not simulated token streaming or a display of model reasoning.
User messages have a full-width gray background; answers have a bullet and indentation.
The progress indicator refreshes at 8 Hz and stops when the command finishes.
Errors remain visible, including missing results and nonzero process exit codes.
`--debug` and explicitly requested `--json` retain their diagnostic output.
Other commands stream stdout and stderr to the message region.
Cancellation also cleans up the independent Codex
process group. No model request is made by opening or filtering the command menu.

The first submitted command replaces the full welcome panel with a persistent
bordered seven-row header: a small crowned slime, the active model, and the workspace path.
Small terminals use a bordered five-row text header. The model refreshes after `/auth`;
the input remains at the bottom. Long paths are ellipsized, not wrapped over output.
The startup animation is visually unchanged; rollback uses the Git snapshot below.

Auth/profile/provider and slash menus use Up/Down and Enter. The selected entry
has slime-orange fill (`#dc795f`), dark text, and crown-gold (`#e8b866`)
rounded line borders. Border cells use the terminal background, so the orange
fill cannot extend outside the vertical lines.
The frame is inset one character from each side of the menu, directly beside
the fill. Text and fill stay in place; the selected item remains three rows
and other items one row.
There are no extra blank rows between the fill and the border. Line glyphs sit
within their terminal cells; exact half-cell padding and stroke weight depend on
the terminal font, rather than pixel geometry. Unicode has no matching heavy
rounded corners, so these use continuous light strokes, not a promised adjustable
line width. No block-based corners are used. Unselected
entries occupy one row; the selected entry reserves three. Plain terminals use
a single reverse-video row.
Command names align left and descriptions align right, with cell-aware truncation.
Escape or Ctrl+C returns without making a selection. Key entry remains hidden.
`NO_COLOR` uses reverse-video selection instead of orange. For scripts, use
`auth add/use/model/list`, not the interactive selector.

Restart `literary` after updating its Python modules. Auth code is loaded at
startup so a long-lived screen does not lazily mix old widgets with new handlers.

## Book Directories and Conversations

Start with `literary --shelf /path/to/books`. This selects a bookshelf, not a novel:
it has `.literarygiant/bookshelf.json` and command history, but no manuscript,
story bible, or writing conversation. Writing requests require a focused book.
Inside the TUI, `/shelf /another/bookshelf` changes this root explicitly.

`/newbook "Title"` creates `/path/to/books/Title` and immediately focuses it;
`/newbook` alone asks for the name. When no bookshelf is selected, it first asks
for the parent directory for books. Creation reports the full resulting path.
An existing book cannot be selected as a bookshelf; choose its parent or a separate
directory to preserve its assets. Names cannot contain path separators or overwrite
an existing directory. For scripts use `literary --shelf /path/to/books newbook "Title"`.
`/focus` lists only initialized direct child books of this bookshelf (not a global
registry); `/focus "Title"` selects one directly. Relative paths resolve from the
bookshelf even when another book is focused. Outside/symlink escapes are rejected.

Focusing replaces configuration, input history, displayed messages, and active
conversation. It restores the target book's most recently updated conversation,
or starts empty when none exists. `/new` starts a fresh discussion within that book;
`/resume` chooses a different saved discussion. No book assets are moved or merged.
The status header distinguishes Book from Bookshelf and displays the shelf name
and book-relative location, for example `books / Title` (or `books / .` at the root).

Existing standalone books remain compatible with `literary -C /path/to/book` and
`/open`. A direct child book detects its parent's bookshelf marker on startup,
so `/newbook` still creates siblings, not nested novels. Without a bookshelf marker,
legacy `/open`, `/project create`, and global registered-book selection remain
available; switching such standalone books starts a new discussion as before.

Every book has `.literarygiant/agent.toml`, next to its conversations and memory:

```toml
[context]
instructions = "Writing rules specific to this book."
conversation_chars = 10000
allow_external_reference = false
```

Changes are loaded on the next workflow request. The conversation budget caps
model context, not saved history. A zero budget disables previous-discussion input.
Project paths, knowledge settings, and writing behavior come from project files,
not another project's/global writing configuration. Global model credentials,
runtime selection and a default request timeout remain reusable. The underlying
workflow engine does not auto-load ancestor `AGENTS.md`; book instructions above
are added explicitly to each stage. This is context isolation, not a new OS sandbox.

Each book owns:

- `.literarygiant/story.sqlite3`: manuscript, story bible, versions, and project data.
- `.literarygiant/memory/`: writing memory, including `STORY_BIBLE.md`.
- `.literarygiant/output/`: generated chapters, plans, and other workflow artifacts.
- `.literarygiant/output/conversations/`: existing single-run chat Markdown artifacts.
- `.literarygiant/conversations/<id>.jsonl`: ordered author/assistant messages,
  timestamps, errors, and run links for the writing discussion.
- `.literarygiant/history`: input recall, not a replacement for dialogue history.
- `.literarygiant/runs/`: workflow events, requests, and execution state.

`/resume` selects a saved discussion in the current book; `/resume <id>` restores
one directly. `literary -C /path/to/book --conversation <id>` opens it at startup.
`/new` starts a fresh discussion while keeping project memory. `/clear` clears only
the visible screen, not the active discussion or its stored messages. Restored
dialogue is included in subsequent writing workflow requests, capped at 10,000
characters (or one third of the stage context budget); full records stay on disk.
`/run resume <id>` remains workflow recovery, distinct from conversation recovery.
Auth dialogs and keys are not saved in conversation assets. Model credentials
remain in the existing isolated LG environment outside the book.

Writing memory/output overrides must resolve inside the book directory, including
symlink resolution. Memory symlinks outside the book are skipped. Reference library
discovery does not walk parent directories by default. External reference paths
require `context.allow_external_reference = true` in that book's `agent.toml`;
raw source text still requires its separate explicit opt-in. Existing old
chat artifacts are preserved but are not automatically reconstructed into sessions,
since a single final-answer file does not represent a complete ordered discussion.

Welcome layout uses full, compact, and minimal headers according to terminal size.
Its border fills the available terminal width in both resize directions, with no
88-column cap. Animation pixels retain their original size and are re-centered.
Long labels are ellipsized rather than adding rows. Under 12 rows the header is
hidden to preserve the composer and command menu. Normal-sized welcome animation
frames are unchanged.

## Model Instructions

Writing requests identify the assistant as LiteraryGiant, not its execution engine.
Both workflow and native launchers disable bundled engine skills and automatic
apps/collaboration instructions. LG writing skills, subagent prompts, project rules,
structured-output contracts, and filesystem permissions remain active. This changes
the model-facing persona/context, not the pinned engine source or its access controls.
Native integration tests capture the final StepFun-compatible Messages payload
using a local fake provider and reject bundled skill/coding-agent identity leakage.
Old stored conversations are not rewritten; `/new` starts without their dialogue.

## Snapshot and Cleanup

The pre-status-header version is archived on GitHub at LiteraryAgent commit
`b6afbf7aa` (branch `literary-agent`), referenced by LiteraryGiant commit `140afbe`.
The current refactor removes unused welcome story-summary queries and shares the
Rich rendering/model-label helpers. Golden tests compare every startup animation
frame and full/compact panels at three widths with the archived appearance.

The duplicate legacy animation and runtime mode switch were removed with author
approval; only the smooth implementation remains. Packaged `resources/` files are required for wheel installations;
the preview script and animation tests remain useful verification tools, not
disposable generated assets. No credential stores or generated manuscript data
belong in the repository.
