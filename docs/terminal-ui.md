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
- PageUp/PageDown scroll the displayed message history.
- Input is locked while a command is running; Ctrl+C cancels the command.
- Ctrl+C while idle clears input; Ctrl+D with empty input or `/exit` closes LG.
- The on-screen transcript retains its latest 250,000 characters. Durable workflow
  artifacts and input history remain in the project directory.

Auth, external-editor, and native-terminal commands temporarily suspend the UI
and return afterward. Credentials entered there are not copied into the transcript.
Other commands run as child CLI processes without a shell, streaming stdout and
stderr to the message region. Cancellation also cleans up the independent Codex
process group. No model request is made by opening or filtering the command menu.

The welcome animation stops after the first submitted command. Its existing
`LG_ANIMATION=legacy` rollback option remains available.
