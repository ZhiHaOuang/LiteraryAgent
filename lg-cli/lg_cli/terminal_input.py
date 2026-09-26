from __future__ import annotations

import asyncio
import codecs
import json
import os
import signal
import time

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.processors import PasswordProcessor
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.lexers import DynamicLexer, Lexer, SimpleLexer
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.styles import Style

from .slash_commands import matching_commands
from .slime_animation import BODY, CROWN, FRAME_INTERVAL, prepare_frames
from .terminal_view import choice_rows, render_conversation


class BookReaderLexer(Lexer):
    def lex_document(self, document):
        def line(number):
            text = document.lines[number]
            if text.startswith("# "):
                return [("class:reader.title", text[2:])]
            if text.startswith(("## ", "### ")):
                return [("class:reader.heading", text.lstrip("# "))]
            if text.startswith("> "):
                return [("class:reader.quote", text)]
            if text.startswith(("Status:", "Source:", "Order:", "Decision:")):
                return [("class:reader.meta", text)]
            return [("", text)]
        return line


class HistoryControl(FormattedTextControl):
    def __init__(self, *args, on_scroll, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_scroll = on_scroll

    def mouse_handler(self, mouse_event):
        if mouse_event.event_type in {
            MouseEventType.SCROLL_UP,
            MouseEventType.SCROLL_DOWN,
        }:
            self.on_scroll(
                3 if mouse_event.event_type == MouseEventType.SCROLL_UP else -3
            )
            return None
        return NotImplemented


def input_style() -> Style:
    plain = {
        "bottom-toolbar": "noreverse",
        "bottom-toolbar.text": "noreverse",
        "command.selected": "reverse",
        "reader.title": "bold",
        "reader.heading": "bold",
    }
    if "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
        return Style.from_dict(plain)
    return Style.from_dict(
        {
            **plain,
            "lg-rule": "#808080",
            "lg-prompt": "#dc795f bold",
            "bottom-toolbar.text": "#808080 noreverse",
            "command.selected": f"bg:{BODY} #181818 bold noreverse",
            "command.edge": f"{CROWN} bg:default noreverse",
            "command.side": f"{CROWN} bg:default noreverse",
            "command.description": "#808080",
            "reader.title": f"{CROWN} bold",
            "reader.heading": f"{BODY} bold",
            "reader.quote": "#b7b7b7 italic",
            "reader.meta": "#929292",
        }
    )


class LiteraryInput:
    """One persistent screen: transcript above an anchored composer."""

    def __init__(
        self,
        history_path,
        *,
        input=None,
        output=None,
        welcome=None,
        compact_welcome=None,
        minimal_welcome=None,
        status_bar=None,
        commands=(),
    ):
        self.model = ""
        self.provider = ""
        self.welcome = welcome
        self.compact_welcome = compact_welcome
        self.minimal_welcome = minimal_welcome
        self.status_bar = status_bar
        self.commands = list(commands)
        self.transcript = ""
        self._blocks = []
        self._view_cache = None
        self.progress = "Working"
        self.started = 0.0
        self._workflow_error = False
        self.scroll = 0
        self.busy = False
        self.process = None
        self._handler = None
        self._task = None
        self._cancel_task = None
        self._matches = []
        self._selected = 0
        self._menu_hidden = False
        self.dialog = None
        self.viewer = None
        self.viewer_editable = False
        self.viewer_allow_edit = False
        self._viewer_result = None
        self.viewer_buffer = Buffer(read_only=Condition(lambda: not self.viewer_editable), multiline=True)
        self.viewer_window = Window(
            BufferControl(buffer=self.viewer_buffer, lexer=DynamicLexer(
                lambda: SimpleLexer() if self.viewer_editable else BookReaderLexer())),
            wrap_lines=True, right_margins=[ScrollbarMargin(display_arrows=False)],
            always_hide_cursor=Condition(lambda: not self.viewer_editable),
        )
        self._dialog_result = None
        self.dialog_buffer = Buffer(
            multiline=False,
            read_only=Condition(
                lambda: self._dialog_result is not None and self._dialog_result.done()
            ),
        )
        self.dialog_editor = Window(
            BufferControl(
                buffer=self.dialog_buffer, input_processors=[PasswordProcessor()]
            ),
            height=1,
        )
        self.dialog_plain_editor = Window(
            BufferControl(buffer=self.dialog_buffer), height=1
        )
        self.buffer = Buffer(
            multiline=True,
            history=FileHistory(str(history_path)),
            auto_suggest=AutoSuggestFromHistory(),
            read_only=Condition(lambda: self.busy),
            on_text_changed=self._changed,
        )
        self.editor = Window(
            BufferControl(buffer=self.buffer),
            wrap_lines=True,
            height=Dimension(min=1, max=8),
            dont_extend_height=True,
        )
        self.messages = Window(
            HistoryControl(
                lambda: ANSI(self._conversation()),
                on_scroll=self.scroll_history,
                get_cursor_position=lambda: Point(
                    0,
                    max(0, self._conversation().count("\n") - self.scroll),
                ),
            ),
            wrap_lines=False,
            get_vertical_scroll=lambda window: max(
                0,
                self._conversation().count("\n")
                + 1
                - (window.render_info.window_height if window.render_info else 1)
                - self.scroll,
            ),
            always_hide_cursor=True,
        )
        rule = "-" if os.environ.get("TERM") == "dumb" else "\u2500"

        def header_text():
            size = get_app().output.get_size()
            if self.welcome is None and self.status_bar:
                return self.status_bar(max(1, size.columns - 1))
            if self.minimal_welcome and (size.rows < 24 or size.columns < 40):
                return self.minimal_welcome(max(1, size.columns - 1))
            compact = size.rows < 32 or size.columns < 40
            callback = self.compact_welcome if compact else self.welcome
            return callback(max(1, size.columns - 1)) if callback else ""

        header = ConditionalContainer(
            Window(
                FormattedTextControl(lambda: ANSI(header_text())),
                height=lambda: Dimension.exact(len(header_text().splitlines())),
                dont_extend_height=True,
            ),
            filter=Condition(
                lambda: (
                    get_app().output.get_size().rows >= 12
                    and (
                        (self.welcome is None and self.status_bar is not None)
                        or (
                            self.welcome is not None
                            and self.minimal_welcome is not None
                        )
                        or (
                            self.welcome is not None
                            and get_app().output.get_size().rows
                            >= (24 if self.compact_welcome else 32)
                            and get_app().output.get_size().columns >= 40
                        )
                    )
                )
            ),
        )
        self._header = header
        self.dialog_choices = Window(
            FormattedTextControl(self._dialog_text),
            height=lambda: 1 + sum(text.count("\n") for _, text in self._dialog_text()),
            dont_extend_height=True,
            always_hide_cursor=True,
        )
        dialog_panel = ConditionalContainer(
            HSplit(
                [
                    Window(height=1, char=rule, style="class:lg-rule"),
                    Window(
                        FormattedTextControl(
                            lambda: (
                                self.dialog["title"] + (
                                    "\n" + self.dialog["text"]
                                    if get_app().output.get_size().rows >= 14 else ""
                                )
                                if self.dialog
                                else ""
                            )
                        ),
                        height=lambda: 2 if get_app().output.get_size().rows >= 14 else 1,
                    ),
                    ConditionalContainer(
                        self.dialog_choices,
                        Condition(
                            lambda: bool(
                                self.dialog and self.dialog["kind"] == "choice"
                            )
                        ),
                    ),
                    ConditionalContainer(
                        self.dialog_editor,
                        Condition(
                            lambda: bool(self.dialog and self.dialog.get("password"))
                        ),
                    ),
                    ConditionalContainer(
                        self.dialog_plain_editor,
                        Condition(
                            lambda: bool(
                                self.dialog
                                and self.dialog["kind"] == "input"
                                and not self.dialog.get("password")
                            )
                        ),
                    ),
                    Window(height=1, char=rule, style="class:lg-rule"),
                ]
            ),
            Condition(lambda: self.dialog is not None),
        )
        menu = ConditionalContainer(
            Window(
                FormattedTextControl(self._menu_text),
                height=lambda: 1 + sum(t.count("\n") for _, t in self._menu_text()),
                dont_extend_height=True,
            ),
            filter=Condition(
                lambda: bool(self._matches) and not self._menu_hidden and not self.busy
            ),
        )
        content = HSplit(
            [
                header,
                ConditionalContainer(self.messages, Condition(lambda: self.viewer is None)),
                ConditionalContainer(
                    HSplit([
                        Window(FormattedTextControl(lambda: self.viewer or ""), height=1),
                        self.viewer_window,
                        Window(FormattedTextControl(lambda: (
                            ("Esc  Save / Back    " if self.viewer_editable else
                             "E  Edit    Esc  Back    " if self.viewer_allow_edit else "Esc  Back    ")
                            + f"{self.viewer_buffer.document.cursor_position_row + 1}"
                            f" / {self.viewer_buffer.document.line_count}"
                        )), height=1, style="class:reader.meta"),
                    ]), Condition(lambda: self.viewer is not None),
                ),
                ConditionalContainer(
                    Window(FormattedTextControl(self._progress_text), height=1),
                    Condition(lambda: self.busy and self.dialog is None and self.viewer is None),
                ),
                menu,
                dialog_panel,
                ConditionalContainer(
                    HSplit(
                        [
                            Window(height=1, char=rule, style="class:lg-rule"),
                            VSplit(
                                [
                                    Window(
                                        FormattedTextControl(
                                            [("class:lg-prompt", "> ")]
                                        ),
                                        width=2,
                                        height=1,
                                        dont_extend_height=True,
                                    ),
                                    self.editor,
                                ]
                            ),
                            Window(height=1, char=rule, style="class:lg-rule"),
                        ]
                    ),
                    Condition(lambda: self.dialog is None and self.viewer is None),
                ),
                Window(
                    FormattedTextControl(
                        lambda: [
                            (
                                "class:bottom-toolbar.text",
                                f" {self.provider} | {self.model}"
                                + (" | Running" if self.busy else ""),
                            )
                        ]
                    ),
                    height=1,
                ),
            ]
        )
        layout = VSplit([content, Window(width=1)])
        keys = KeyBindings()

        @keys.add("enter")
        def submit(event):
            if self.viewer is not None:
                if self.viewer_editable:
                    self.viewer_buffer.insert_text("\n")
                return
            if self.dialog:
                value = (
                    self.dialog["values"][self.dialog["selected"]][0]
                    if self.dialog["kind"] == "choice"
                    else self.dialog_buffer.text
                )
                self._finish_dialog(value)
                return
            if self.busy:
                return
            if self._matches and not self._menu_hidden:
                chosen = self._matches[self._selected].text
                if self.buffer.text.rstrip() != chosen or self._has_children(chosen):
                    self._complete()
                    return
            value = self.buffer.text.strip()
            if not value:
                return
            self.buffer.append_to_history()
            if self._handler is None:
                event.app.exit(result=value)
                return
            self.buffer.reset()
            self._changed(self.buffer)
            self.welcome = None
            self.append(value, role="user")
            self.started = time.monotonic()
            self.progress = "Working"
            self.busy = True
            self._task = event.app.create_background_task(self._dispatch(value))

        @keys.add("tab")
        def complete(event):
            if self.viewer is not None:
                if self.viewer_editable:
                    self.viewer_buffer.insert_text("    ")
                return
            if not self.busy:
                self._complete()

        @keys.add(
            "up",
            filter=Condition(
                lambda: (
                    bool(self.dialog and self.dialog["kind"] == "choice")
                    or bool(self._matches)
                    and not self._menu_hidden
                )
            ),
        )
        def previous(event):
            if self.dialog:
                self.dialog["selected"] = (self.dialog["selected"] - 1) % len(
                    self.dialog["values"]
                )
                return
            self._selected = (self._selected - 1) % len(self._matches)

        @keys.add(
            "down",
            filter=Condition(
                lambda: (
                    bool(self.dialog and self.dialog["kind"] == "choice")
                    or bool(self._matches)
                    and not self._menu_hidden
                )
            ),
        )
        def next_command(event):
            if self.dialog:
                self.dialog["selected"] = (self.dialog["selected"] + 1) % len(
                    self.dialog["values"]
                )
                return
            self._selected = (self._selected + 1) % len(self._matches)

        @keys.add(
            "up",
            filter=Condition(
                lambda: (
                    not self.dialog
                    and (not self._matches or self._menu_hidden or self.busy)
                )
            ),
        )
        def scroll_up(event):
            if self.viewer is not None:
                self.viewer_buffer.cursor_up(count=1 if self.viewer_editable else 3)
                return
            self.scroll_history(3)

        @keys.add(
            "down",
            filter=Condition(
                lambda: (
                    not self.dialog
                    and (not self._matches or self._menu_hidden or self.busy)
                )
            ),
        )
        def scroll_down(event):
            if self.viewer is not None:
                self.viewer_buffer.cursor_down(count=1 if self.viewer_editable else 3)
                return
            self.scroll_history(-3)

        @keys.add("escape")
        def dismiss(event):
            if self.viewer is not None:
                self._finish_viewer()
                return
            if self.dialog:
                self._finish_dialog(None)
                return
            self._menu_hidden = True

        @keys.add("escape", "enter")
        @keys.add("c-j")
        def newline(event):
            if self.viewer is not None:
                if self.viewer_editable:
                    self.viewer_buffer.insert_text("\n")
                return
            if not self.busy:
                self.buffer.insert_text("\n")

        @keys.add("E", filter=Condition(lambda: self.viewer is not None and self.viewer_allow_edit))
        @keys.add("e", filter=Condition(lambda: self.viewer is not None and self.viewer_allow_edit))
        def edit_view(event):
            self._finish_viewer("edit")

        @keys.add("c-s", filter=Condition(lambda: self.viewer is not None and self.viewer_editable))
        def save_view(event):
            self._finish_viewer()

        @keys.add("pageup")
        def page_up(event):
            if self.viewer is not None:
                self.viewer_buffer.cursor_up(count=max(1, event.app.output.get_size().rows // 2))
                return
            self.scroll = min(
                self._conversation().count("\n"),
                self.scroll + max(1, event.app.output.get_size().rows // 2),
            )

        @keys.add("pagedown")
        def page_down(event):
            if self.viewer is not None:
                self.viewer_buffer.cursor_down(count=max(1, event.app.output.get_size().rows // 2))
                return
            self.scroll = max(
                0, self.scroll - max(1, event.app.output.get_size().rows // 2)
            )

        @keys.add("c-c")
        def cancel(event):
            if self.viewer is not None:
                self._finish_viewer()
                return
            if self.dialog:
                self._finish_dialog(None)
                return
            if self.busy:
                if self._cancel_task is None or self._cancel_task.done():
                    self._cancel_task = event.app.create_background_task(
                        self.cancel_process()
                    )
            elif self._handler is None:
                event.app.exit(exception=KeyboardInterrupt())
            else:
                self.buffer.reset()
                self._changed(self.buffer)

        @keys.add("c-d")
        def eof(event):
            if self.viewer is not None:
                self._finish_viewer()
                return
            if self.dialog:
                self._finish_dialog(None)
                return
            if self.busy:
                return
            if self.buffer.text:
                self.buffer.delete()
            elif self._handler is None:
                event.app.exit(exception=EOFError())
            else:
                event.app.exit(result=0)

        self.app = Application(
            layout=Layout(layout, focused_element=self.editor),
            key_bindings=keys,
            style=input_style(),
            full_screen=True,
            mouse_support=True,
            color_depth=ColorDepth.DEPTH_24_BIT,
            erase_when_done=True,
            input=input,
            output=output,
        )

    async def view_text(self, title, text, *, allow_edit=False):
        return await self._show_text(title, text, allow_edit=allow_edit)

    async def edit_text(self, title, text):
        return await self._show_text(title, text, editable=True)

    async def _show_text(self, title, text, *, editable=False, allow_edit=False):
        self.welcome = None
        self.viewer = title
        self.viewer_editable = editable
        self.viewer_allow_edit = allow_edit and not editable
        self.viewer_buffer.set_document(Document(text, 0), bypass_readonly=True)
        self._viewer_result = asyncio.get_running_loop().create_future()
        self.app.layout.focus(self.viewer_window)
        self.app.invalidate()
        try:
            return await self._viewer_result
        finally:
            self.viewer = None
            self.viewer_editable = False
            self.viewer_allow_edit = False
            self._viewer_result = None
            self.app.layout.focus(self.editor)
            self.app.invalidate()

    def _finish_viewer(self, action=None):
        if self._viewer_result and not self._viewer_result.done():
            self._viewer_result.set_result(self.viewer_buffer.text if self.viewer_editable else action)

    async def ask(self, *, kind, title, text, values=(), default="", password=False, record=True):
        self.welcome = None
        if kind == "choice" and not values:
            return None
        self.dialog = {
            "kind": kind,
            "title": title,
            "text": text,
            "values": values,
            "selected": 0,
            "password": password,
        }
        self._dialog_result = asyncio.get_running_loop().create_future()
        self.dialog_buffer.reset(Document(default, len(default)))
        target = (
            self.dialog_choices
            if kind == "choice"
            else self.dialog_editor
            if password
            else self.dialog_plain_editor
        )
        self.app.layout.focus(target)
        self.app.invalidate()
        try:
            value = await self._dialog_result
            if value is not None and record:
                label = (
                    next((label for key, label in values if key == value), str(value))
                    if kind == "choice"
                    else ("[hidden]" if password else value)
                )
                self.append(
                    f"{title} -> {label}\n"
                    if kind == "choice"
                    else f"{text}: {label}\n"
                )
            return value
        finally:
            self.dialog_buffer.reset()

    def close_dialog(self):
        self.dialog_buffer.reset()
        self.dialog = None
        self._dialog_result = None
        self.app.layout.focus(self.editor)
        self.app.invalidate()

    def _finish_dialog(self, value):
        if self._dialog_result and not self._dialog_result.done():
            self._dialog_result.set_result(value)

    def _dialog_text(self):
        if not self.dialog or self.dialog["kind"] != "choice":
            return []
        size = self.app.output.get_size()
        header_rows = self._header.preferred_height(
            max(1, size.columns - 1), size.rows
        ).preferred
        count = max(1, min(8, size.rows - header_rows - 8))
        selected = self.dialog["selected"]
        start = max(0, selected - count + 1)
        labels = [label for _, label in self.dialog["values"]]
        return choice_rows(
            labels[start : start + count], selected - start, max(4, size.columns - 1)
        )

    def _changed(self, buffer):
        self._matches = matching_commands(buffer.text, self.commands)
        self._selected = 0
        self._menu_hidden = False

    def _menu_rows(self):
        size = get_app().output.get_size()
        header_rows = self._header.preferred_height(
            max(1, size.columns - 1), size.rows
        ).preferred
        # Leave room for the composer, status, rules, and one transcript row.
        return max(1, min(8, size.rows // 3, size.rows - header_rows - 7))

    def _menu_text(self):
        start = max(0, self._selected - self._menu_rows() + 1)
        return choice_rows(
            [
                (c.text, c.description)
                for c in self._matches[start : start + self._menu_rows()]
            ],
            self._selected - start,
            get_app().output.get_size().columns - 1,
        )

    def _complete(self):
        if self._matches:
            value = self._matches[self._selected].text
            expand = self._has_children(value)
            if expand:
                value += " "
            self.buffer.document = Document(value, len(value))
            self._menu_hidden = not expand

    def _has_children(self, value):
        return value != "/auth" and any(
            command.text.startswith(value + " ") for command in self.commands
        )

    def _conversation(self):
        width = max(4, self.app.output.get_size().columns - 1)
        if self._view_cache is None or self._view_cache[0] != width:
            self._view_cache = (width, render_conversation(self._blocks, width))
        return self._view_cache[1]

    def append(self, text, *, role="system"):
        if not text:
            return
        old_lines = self._conversation().count("\n") if self.scroll else 0
        self.transcript = (self.transcript + text)[-250000:]
        if role == "system" and self._blocks and self._blocks[-1][0] == role:
            self._blocks[-1] = (role, (self._blocks[-1][1] + text)[-250000:])
        else:
            self._blocks.append((role, text))
        while len(self._blocks) > 1 and sum(len(t) for _, t in self._blocks) > 250000:
            self._blocks.pop(0)
        self._view_cache = None
        if self.scroll:
            self.scroll += max(0, self._conversation().count("\n") - old_lines)
        self.app.invalidate()

    def clear(self):
        self.transcript = ""
        self._blocks.clear()
        self._view_cache = None
        self.scroll = 0
        self.app.invalidate()

    def set_history(self, history_path):
        history_path.parent.mkdir(parents=True, exist_ok=True)
        self.buffer.history = FileHistory(str(history_path))
        self.buffer.reset()

    def scroll_history(self, amount):
        height = (
            self.messages.render_info.window_height if self.messages.render_info else 1
        )
        maximum = max(0, self._conversation().count("\n") + 1 - height)
        self.scroll = max(0, min(maximum, self.scroll + amount))
        self.app.invalidate()

    async def _dispatch(self, value):
        self.started = time.monotonic()
        self.progress = "Working"
        animation = asyncio.create_task(self._animate_progress())
        try:
            keep_running = await self._handler(value)
            if not keep_running:
                self.app.exit(result=0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep command failures inside the UI boundary.
            self.append(f"LG error: {exc}\n")
        finally:
            animation.cancel()
            await asyncio.gather(animation, return_exceptions=True)
            self.busy = False
            self.app.invalidate()

    def _progress_text(self):
        elapsed = max(0, time.monotonic() - self.started) if self.started else 0
        frames = "|/-\\" if os.environ.get("TERM") == "dumb" else "✶✸✹✺✹✸"
        return [
            ("class:lg-prompt", f" {frames[int(elapsed * 8) % len(frames)]} "),
            ("class:bottom-toolbar.text", f"{self.progress}... {elapsed:.0f}s"),
        ]

    async def _animate_progress(self):
        while True:
            await asyncio.sleep(0.125)
            if self.dialog is None:
                self.app.invalidate()

    def _workflow_line(self, line):
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            self.append(line + "\n")
            return False
        if not isinstance(record, dict):
            self.append(line + "\n")
            return False
        kind = record.get("type", "")
        if not kind and "run_id" in record and isinstance(record.get("categories"), list):
            status = record.get("status", "unknown")
            self._workflow_error = self._workflow_error or status != "completed"
            self.append(f"Analysis {status}: {', '.join(record['categories'])}\n")
            for blocker in record.get("blockers", []):
                self.append(f"Canon conflict: {blocker.get('explanation', '')}\n")
            return True
        if not kind and "slug" in record and "active_version_number" in record and "state" in record:
            self.append(f"{record['slug']} v{record['active_version_number']} [{record['state']}]\n")
            return True
        if not kind and "run_id" in record and "slug" in record and "version" in record and "active" in record:
            state = "active" if record["active"] else "candidate"
            self.append(f"Adopted {record['slug']} v{record['version']} [{state}]\n")
            return True
        if kind in {"run.result", "story.result"}:
            self._workflow_error = bool(
                self._workflow_error
                or record.get("error")
                or record.get("exit_code")
                or record.get("status") == "failed"
            )
            if record.get("text"):
                self.append(record["text"], role="assistant")
            if record.get("error"):
                self.append(f"Error: {record['error']}\n")
            return True
        if kind == "error":
            self._workflow_error = True
            self.append(f"Error: {record.get('error', 'Unknown error')}\n")
        elif kind == "context.started":
            self.progress = "Reading context"
        elif kind == "stage.started":
            self.progress = "Working: " + str(record.get("stage_id") or "answer")
        elif kind in {"stage.failed", "run.failed"}:
            self.progress = "Failed"
        self.app.invalidate()
        return False

    async def cancel_process(self):
        process = self.process
        if process is None or process.returncode is not None:
            return
        self.append("Cancelling...\n")
        try:
            os.killpg(process.pid, signal.SIGINT)
            await asyncio.wait_for(process.wait(), 8)
        except asyncio.TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        except ProcessLookupError:
            pass

    async def run_command(self, argv, *, env=None, cwd=None, structured=False):
        self.process = await asyncio.create_subprocess_exec(
            *argv,
            env=env,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            limit=65536,
        )
        process = self.process
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        received = False
        self._workflow_error = False
        started = time.monotonic()
        try:
            while data := await process.stdout.read(4096):
                chunk = decoder.decode(data)
                if structured:
                    pending += chunk
                    while "\n" in pending:
                        line, pending = pending.split("\n", 1)
                        received = self._workflow_line(line) or received
                else:
                    self.append(chunk)
            tail = decoder.decode(b"", final=True)
            if structured:
                if pending or tail:
                    received = self._workflow_line(pending + tail) or received
            else:
                self.append(tail)
            result = await process.wait()
            if result:
                self.append(f"Command exited with code {result}\n")
            elif structured and not received:
                self.append(
                    "No workflow result received. Use /run list to inspect the run.\n"
                )
            if structured:
                label = (
                    "Failed"
                    if result or not received or self._workflow_error
                    else "Completed"
                )
                self.append(f"\n{label} in {time.monotonic() - started:.1f}s\n")
            return result
        finally:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
            self.process = None

    async def _animate(self):
        while self.welcome is not None:
            await asyncio.sleep(FRAME_INTERVAL)
            if (
                self.app.output.get_size().rows >= 32
                and self.app.output.get_size().columns >= 40
            ):
                self.app.invalidate()

    def _start(self):
        if self.welcome and os.environ.get("TERM") != "dumb":
            self.app.create_background_task(self._animate())

    def run(self, handler, model, provider, *, initial_command=None):
        self._handler = handler
        self.model, self.provider = model, provider
        if self.welcome and os.environ.get("TERM") != "dumb":
            prepare_frames()

        def start():
            self._start()
            if initial_command is not None:
                self.started = time.monotonic()
                self.busy = True
                self.app.create_background_task(self._dispatch(initial_command))

        return self.app.run(pre_run=start)

    def prompt(self, model, provider):
        """Single-input adapter used by focused editor tests."""
        self.model, self.provider = model, provider
        self.buffer.reset()
        return self.app.run()
