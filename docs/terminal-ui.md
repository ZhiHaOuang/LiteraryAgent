# Terminal Interface

`literary` keeps one full-screen terminal application open throughout the session.
The message region scrolls above the composer; the input, lower rule, and model
status remain at the bottom during command execution and terminal resizing.

## Book home

Startup opens the last active book's local dashboard. Switching books opens the
new book's dashboard. Type directly to start talking; `/home` returns without
discarding the current conversation. Scroll with the wheel, arrows or PageUp/Down;
`/browse` enters panel navigation: arrows select a panel, Enter opens its entries,
and Enter again reads details. Escape returns one level at a time and restores
native mouse selection. The footer shows the navigation hint once. F1 and Ctrl+O
remain optional compatibility bindings, but no workflow depends on them or requires
VS Code keybinding changes.

The original crowned slime is centered in a large gold rounded frame with the
book, author and model. This same animated header stays fixed above home and chat.
Short/narrow windows use the compact original mark while retaining the centered frame. First paint uses local data or a timestamped screen cache before workflow
imports; source-validated indices and usage summaries refresh in the background.
Personal caches live outside Git under `$XDG_CACHE_HOME/literarygiant/home`.

Book overview, characters, relationships, current events, full plans, preferences
and token usage have separate rounded panels. Narrow terminals stack them; medium
and wide terminals use two or three columns. Opening home makes no model request.
Missing graph data is shown explicitly. See `book-home-review.md` for source and
usage-accounting rules.

`/profile` (also available through `/preferences`) edits the author's name, default model, response detail and writing
preferences. Personal preferences live outside the repository; a book may override
writing preferences. `/resume` shows titles and timestamps without internal IDs;
`/rename` changes the current conversation title without modifying its messages.

## Commands

Type `/` to open a purpose-grouped palette. The first three entries follow your
personal command-use counts across books. Before any usage exists these are
`/resume`, `/novel`, and `/outlines`. Counts live outside the repository at
`$XDG_CONFIG_HOME/literarygiant/command-usage.json`, or
`~/.config/literarygiant/command-usage.json`. They are not project data, credentials,
or telemetry; multiple open books update them under a file lock.

The three favorites share one gold rounded frame. Gold rounded cards group conversation, reading/editing, creation, review/memory,
book/project, and settings commands. Up/Down selects; Enter opens a group or runs a
parameter-free command immediately. Required arguments open a field-by-field
prompt. Tab completes without executing. Escape returns from a group, then closes
the palette. Typing filters all commands by name, description, and category.

`/novel` opens the chapter reader/editor. `/outlines` reads and edits existing
outline documents and generated outline Markdown files, separate from `/outline`
which generates new material. The outline editor retains old database versions or
file backups in `.literarygiant/editor-history/outlines`; stale edits cannot
silently overwrite newer content. Saving requires an explicit editor confirmation.

## Input and Output

- Enter submits; Alt+Enter or Ctrl+J inserts a newline.
- Bracketed multi-line paste does not submit its individual lines.
- Up/Down scroll dialogue by three lines, not recalled commands; inside menus they
  select entries. Mouse wheel over the transcript scrolls dialogue too.
- PageUp/PageDown scroll the displayed message history by half a screen.
- Normal input and detail readers leave mouse capture off: drag to select and use
  the terminal's native Copy command (Cmd+C on macOS). `/browse` mode captures
  clicks; Escape returns to native selection.
- New output does not pull you back to the bottom while reading older messages.
- Input stays available while the main agent works. Enter sends a steering message
  to that active turn; rejected messages are restored to the composer. Ctrl+C interrupts.
  During an explicit CLI workflow, new messages reach the main agent, which can
  inspect and stop that job or retry it with revised author instructions.
  Redirection restarts the stopped workflow with the new guidance; successful
  workflows cannot be retried through these controls.
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
User messages align right in crown-gold rounded frames; their text remains left
aligned. Each request has one public reply frame on the left; blank streamed items
do not create empty frames. Public thinking summaries and tool execution records
have folded entries below that reply. `/browse` then arrows/Enter, or a click in
browse mode, expands them. Expanded public summaries
use dark-orange frames. Message widths adapt to narrow terminals. Full reasoning chains are never
shown. Code blocks, tables, and long execution logs start collapsed, retaining
surrounding prose and a selectable entry to expand their original contents.

The orange Thinking Orbs `working/20` decoration follows the newest conversation content. Its
curated “I am …ing” phrases change slowly and are unrelated to actual work. It
refreshes at 8 Hz while busy and disappears when idle. It is no longer anchored
above the input. Real task progress appears separately in a compact task strip;
each reviewer has its own identity, current step, and completion/failure status.
Selecting a task in `/browse` opens read-only details. Directing, stopping, or retrying reviewers is done
by messaging the main agent.
Errors remain visible, including missing results and nonzero process exit codes.
`--debug` and explicitly requested `--json` retain their diagnostic output.
Ordinary conversation uses a persistent app-server thread with LG's existing
provider bridge and writing MCP tools. `turn/steer` delivers new author messages
during work. Model, book, and conversation changes replace the active thread;
shutdown cleans up its process. Independent reviewers can run concurrently and
report to the main agent. Explicit workflow commands retain the existing CLI
execution path. Other commands stream stdout and stderr to the message region.
Cancellation also cleans up the independent Codex
process group. No model request is made by opening or filtering the command menu.

Starting a conversation keeps the same fixed centered slime frame and replaces
only the lower dashboard with the conversation. The header reads the current
book/name/model and refreshes after profile changes. It is hidden below 12 terminal
rows to preserve usable input.

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
The shared header displays the focused book, author name and current model.

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

## UI review and validation

Run `PYTHONPATH=lg-cli python scripts/preview_terminal.py --output /tmp/lg-previews`
to export actual prompt-toolkit screen captures as SVG and text at 40, 64, and 80
columns. Samples contain fictional text, use temporary preferences, and make no
model requests. `tests/test_live_runtime.py` is opt-in via
`LG_TEST_RUNTIME_MANIFEST`; it exercises the pinned core with a local fake model,
including live steering, two independent reviewers, and main-agent control of a
running CLI workflow through real MCP tools. It does not use paid APIs.

## Book synopsis and local management

Each book owns `ReferenceLibrary/bible/book-overview.json`. Agent prose generation
and revisions return a concise whole-book synopsis in the same model output; tools
validate it before saving prose and persist it with source/version or run metadata.
Candidate output remains a candidate; an updated synopsis does not promote facts
to canon. Reading home never generates it. Existing outline premises/loglines are
used until a generated synopsis is available; otherwise home says 尚未设置简介.

`/run list` opens a local list; Enter reads a record, Esc returns to the list.
`/run show <id>` opens one local record. Neither calls the agent. Other slash
commands retain explicit CLI routing; only writing operations invoke model-backed
workflows. Management commands do not appear as author chat bubbles or animate
an agent response. `/browse` also works during generation without steering it.

The loading geometry is adapted from MIT-licensed Thinking Orbs by Jakub Antalik,
commit `de85557ca220332586d070d8788c0e1d6e877a0d`. The `working` 20px preset is
rasterized to 8×8 terminal dots (4 columns × 2 rows) and tinted orange. It keeps
upstream motion, not browser pixel fidelity. Tests compare the port against
coordinates produced by the original JavaScript. Copyright and license are in
`lg_cli/resources/thinking-orbs-LICENSE.txt`. No network request is used at runtime.
