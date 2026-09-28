from __future__ import annotations

import asyncio
import codecs
import json
import os
import signal
import shlex
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

from .slash_commands import CATEGORIES, CommandUsage, SlashCommand, matching_commands
from .slime_animation import BODY, CROWN, FRAME_INTERVAL, prepare_frames
from .terminal_view import choice_rows, render_conversation, compact_content, clip
from .working_animation import choose_word, loading_text


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
    def __init__(self, *args, on_scroll, on_click=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_scroll = on_scroll
        self.on_click = on_click

    def mouse_handler(self, mouse_event):
        if mouse_event.event_type in {
            MouseEventType.SCROLL_UP,
            MouseEventType.SCROLL_DOWN,
        }:
            self.on_scroll(
                3 if mouse_event.event_type == MouseEventType.SCROLL_UP else -3
            )
            return None
        if mouse_event.event_type == MouseEventType.MOUSE_UP and self.on_click:
            self.on_click(mouse_event.position)
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
        usage_path=None,
    ):
        self.model = ""
        self.provider = ""
        self.welcome = welcome
        self.compact_welcome = compact_welcome
        self.minimal_welcome = minimal_welcome
        self.status_bar = status_bar
        self.home_renderer = None
        self.home_details_handler = None
        self.home_header_renderer = None
        self.home_refresh = None
        self.home_selection = 0
        self.detail_selection = 0
        self.browse_mode = False
        self._local_command = False
        self.expanded = set()
        self._detail_hotspots = {}
        self._home_positions = {}
        self.home_visible = False
        self.home_scroll = 0
        self._home_cache = None
        self.commands = list(commands)
        self.usage = CommandUsage(usage_path)
        self._category = None
        self._argument_pending = False
        self.transcript = ""
        self._blocks = []
        self._view_cache = None
        self._prefix_cache = None
        self.progress = "Working"
        self._loading_word = choose_word()
        self._loading_epoch = 0
        self.tasks = {}
        self.started = 0.0
        self._workflow_error = False
        self._workflow_task_keys = set()
        self.scroll = 0
        self.busy = False
        self._work_count = 0
        self.process = None
        self._handler = None
        self.steer_handler = None
        self.interrupt_handler = None
        self.shutdown_handler = None
        self._stream_blocks = {}
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
            read_only=Condition(lambda: self._argument_pending),
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
                on_click=self._click_history,
                get_cursor_position=lambda: Point(
                    0,
                    self.home_scroll if self.home_visible else max(0, self._conversation().count("\n") - self.scroll),
                ),
            ),
            wrap_lines=False,
            get_vertical_scroll=lambda window: self.home_scroll if self.home_visible else max(
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
            if self.home_visible:
                return self.home_header_renderer(max(1, size.columns - 1)) if self.home_header_renderer else ''
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
                        (self.home_visible and self.home_header_renderer is not None)
                        or
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
                            ("Esc 完成编辑    " if self.viewer_editable else
                             "E 编辑 · Esc 返回    " if self.viewer_allow_edit else "Esc 返回 · 可拖选复制    ")
                            + f"{self.viewer_buffer.document.cursor_position_row + 1}"
                            f" / {self.viewer_buffer.document.line_count}"
                        )), height=1, style="class:reader.meta"),
                    ]), Condition(lambda: self.viewer is not None),
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
                                clip((' 方向键选择 · Enter 展开 · Esc 返回拖选复制' if self.browse_mode else
                                 ' / 命令 · /browse 浏览 · 拖选复制')
                                + (' · 可追加指令' if self.busy else ''),
                                    max(1, get_app().output.get_size().columns - 2)),
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
            if self.browse_mode and self.home_visible:
                if self.home_details_handler:
                    event.app.create_background_task(self.home_details_handler(self.home_selection))
                return
            if self.browse_mode:
                lines = list(self._detail_hotspots)
                if lines:
                    self._click_history(Point(0, lines[min(self.detail_selection, len(lines) - 1)]))
                return
            if self._argument_pending:
                return
            if self.busy:
                value = self.buffer.text.strip()
                if value:
                    event.app.create_background_task(self._send_steering(value))
                return
            if self._matches and not self._menu_hidden:
                selected = self._matches[self._selected]
                if selected.text.startswith('#'):
                    self._category = selected.text[1:]
                    self._changed(self.buffer)
                    return
                if selected.children:
                    self._complete()
                    return
                value = selected.text
                if selected.arguments:
                    self._argument_pending = True
                    event.app.create_background_task(self._fill_arguments(selected))
                    return
            else:
                value = self.buffer.text.strip()
            self._submit_value(value)

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
            if self.browse_mode:
                self.browse_mode = False
                self._home_cache = None
                return
            if self._category is not None:
                self._category = None
                self.buffer.document = Document('/', 1)
                self._changed(self.buffer)
            else:
                self._menu_hidden = True

        @keys.add("escape", "enter")
        @keys.add("c-j")
        def newline(event):
            if self.viewer is not None:
                if self.viewer_editable:
                    self.viewer_buffer.insert_text("\n")
                return
            self.buffer.insert_text("\n")

        @keys.add("E", filter=Condition(lambda: self.viewer is not None and self.viewer_allow_edit))
        @keys.add("e", filter=Condition(lambda: self.viewer is not None and self.viewer_allow_edit))
        def edit_view(event):
            self._finish_viewer("edit")

        @keys.add("c-o", filter=Condition(lambda: self.dialog is None and self.viewer is None))
        def details(event):
            event.app.create_background_task(self.show_details())

        @keys.add('f1', filter=Condition(lambda: self.dialog is None and self.viewer is None))
        def browse(event):
            if self.browse_mode:
                self.browse_mode = False
                self._home_cache = None
            else:
                self.enter_browse()

        def move_home(delta):
            from .book_home import PANELS
            self.home_selection = max(0, min(len(PANELS) - 1, self.home_selection + delta))
            self._home_cache = None
            self._focus_home_panel()

        home_navigation = Condition(lambda: self.browse_mode and self.home_visible
            and self.dialog is None and self.viewer is None)
        @keys.add('left', filter=home_navigation)
        def home_left(event):
            move_home(-1)

        @keys.add('right', filter=home_navigation)
        def home_right(event):
            move_home(1)

        @keys.add('up', filter=home_navigation)
        def home_up(event):
            width = event.app.output.get_size().columns - 1
            move_home(-(3 if width >= 150 else 2 if width >= 92 else 1))

        @keys.add('down', filter=home_navigation)
        def home_down(event):
            width = event.app.output.get_size().columns - 1
            move_home(3 if width >= 150 else 2 if width >= 92 else 1)

        detail_navigation = Condition(lambda: self.browse_mode and not self.home_visible
            and self.dialog is None and self.viewer is None)

        @keys.add('up', filter=detail_navigation)
        @keys.add('left', filter=detail_navigation)
        def detail_previous(event):
            self.detail_selection = max(0, self.detail_selection - 1)
            self._focus_detail()

        @keys.add('down', filter=detail_navigation)
        @keys.add('right', filter=detail_navigation)
        def detail_next(event):
            self.detail_selection = min(max(0, len(self._detail_hotspots) - 1), self.detail_selection + 1)
            self._focus_detail()

        @keys.add("c-s", filter=Condition(lambda: self.viewer is not None and self.viewer_editable))
        def save_view(event):
            self._finish_viewer()

        @keys.add("pageup")
        def page_up(event):
            if self.viewer is not None:
                self.viewer_buffer.cursor_up(count=max(1, event.app.output.get_size().rows // 2))
                return
            self.scroll_history(max(1, event.app.output.get_size().rows // 2))

        @keys.add("pagedown")
        def page_down(event):
            if self.viewer is not None:
                self.viewer_buffer.cursor_down(count=max(1, event.app.output.get_size().rows // 2))
                return
            self.scroll_history(-max(1, event.app.output.get_size().rows // 2))

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
            mouse_support=Condition(lambda: self.browse_mode and self.viewer is None),
            color_depth=ColorDepth.DEPTH_24_BIT,
            erase_when_done=True,
            input=input,
            output=output,
        )
        self.app.timeoutlen = .3
        self.app.ttimeoutlen = .05

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

    def _submit_value(self, value):
        if not value:
            return
        self.buffer.document = Document(value, len(value))
        self.usage.record(value, self.commands)
        self.buffer.append_to_history()
        if self._handler is None:
            self.app.exit(result=value)
            return
        self._category = None
        self.buffer.reset()
        self._changed(self.buffer)
        if value == '/browse':
            self.enter_browse()
            return
        from .slash_commands import uses_model
        local = value.startswith('/') and not uses_model(value)
        self._local_command = local
        preserve_home = value in {'/home', '/profile', '/preferences', '/run list'} or value.startswith('/run show ')
        if not preserve_home:
            self.welcome = None
            self.home_visible = False
        if not local:
            self.append(value, role="user")
        self.started = time.monotonic()
        self.progress = "Working"
        self.busy = True
        self._task = self.app.create_background_task(self._dispatch(value))

    def enter_browse(self):
        self.browse_mode = True
        self._home_cache = None
        self._view_cache = None
        if self.home_visible:
            self._focus_home_panel()
        else:
            self._focus_detail()
        self.app.invalidate()

    async def _fill_arguments(self, command):
        values = [command.text]
        try:
            for argument in command.arguments:
                value = await self.ask(kind='choice' if argument.choices else 'input',
                    title=command.text, text=f'填写 {argument.name}',
                    values=[(v, v) for v in argument.choices], record=False)
                self.close_dialog()
                if value is None:
                    return
                if not str(value).strip():
                    self.append(f'{argument.name} 不能为空。\n')
                    return
                if argument.option:
                    values.append(argument.option)
                values.append(shlex.quote(str(value)))
            text = ' '.join(values)
            self.buffer.set_document(Document(text, len(text)), bypass_readonly=True)
            self._menu_hidden = True
            # Reuse the normal submission path after the last field is confirmed.
            self._argument_pending = False
            self._submit_value(text)
        finally:
            self._argument_pending = False
            self.close_dialog()

    def _changed(self, buffer):
        if buffer.text == '/':
            if self._category:
                self._matches = [c for c in self.commands if c.category == self._category and not c.children]
            else:
                self._matches = self.usage.favorites(self.commands) + [
                    SlashCommand('#' + key, label, key) for key, label in CATEGORIES.items()
                ]
        else:
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
        if self.buffer.text != '/' or self._category:
            start = max(0, self._selected - self._menu_rows() + 1)
            return choice_rows(
                [(c.text, c.description)
                 for c in self._matches[start:start + self._menu_rows()]],
                self._selected - start, get_app().output.get_size().columns - 1)
        from .terminal_view import clip, command_columns
        from prompt_toolkit.utils import get_cwidth
        width = max(8, get_app().output.get_size().columns - 3)
        size = get_app().output.get_size()
        header_rows = self._header.preferred_height(max(1, size.columns - 1), size.rows).preferred
        budget = max(1, min(25, size.rows - header_rows - 6))
        favorites = [(i, c) for i, c in enumerate(self._matches) if not c.text.startswith('#')]
        groups = [('常用命令', favorites)] + [(c.description, [(i, c)])
            for i, c in enumerate(self._matches) if c.text.startswith('#')]
        blocks = []
        for title, items in groups:
            lines = []
            for index, command in items:
                selected = index == self._selected
                if command.text.startswith('#'):
                    names = [c.text for c in self.commands if c.category == command.category and not c.children][:3]
                    value = clip(' · '.join(names), width - 5)
                else:
                    value = command_columns(command.text, command.description, width - 6) + ' '
                value = (' › ' if selected else '   ') + value
                lines.append(('class:command.selected' if selected else 'class:command.edge',
                    value + ' ' * max(0, width - 2 - get_cwidth(value))))
            heading = ' ' + clip(title, width - 4) + ' '
            fragments = [('class:command.edge', ' ╭' + heading + '─' * max(0, width - 2 - get_cwidth(heading)) + '╮\n')]
            for line in lines:
                fragments.extend([('class:command.edge', ' │'), line, ('class:command.edge', '│\n')])
            fragments.append(('class:command.edge', ' ╰' + '─' * (width - 2) + '╯'))
            blocks.append((fragments, len(lines) + 2, any(i == self._selected for i, _ in items)))
        selected_block = next((i for i, block in enumerate(blocks) if block[2]), 0)
        if budget < blocks[selected_block][1]:
            # Tiny terminals retain keyboard access without clipping the selected row.
            command = self._matches[self._selected]
            return [('class:command.selected', clip(' › ' + command.text + ' ' + command.description, width))]
        first = selected_block
        used = blocks[first][1]
        while first > 0 and used + blocks[first - 1][1] <= budget:
            first -= 1
            used += blocks[first][1]
        result, used = [], 0
        for fragments, height, _ in blocks[first:]:
            if used + height > budget:
                break
            if result:
                result.append(('', '\n'))
            result.extend(fragments)
            used += height
        return result

    def _complete(self):
        if self._matches:
            value = self._matches[self._selected].text
            if value.startswith('#'):
                self._category = value[1:]
                self._changed(self.buffer)
                return
            expand = self._has_children(value)
            if expand:
                value += " "
            self.buffer.document = Document(value, len(value))
            self._menu_hidden = not expand

    def _has_children(self, value):
        return any(command.text == value and command.children for command in self.commands)

    async def show_details(self):
        if self.home_visible and self.home_details_handler:
            await self.home_details_handler()
            return
        entries = []
        for index, (role, text) in enumerate(self._blocks):
            if text.strip() and role in {'assistant', 'progress', 'system', 'execution'}:
                label = {'assistant': '回复全文', 'progress': '思考摘要', 'system': '操作提示', 'execution':'执行记录'}[role]
                entries.append((f'message:{index}', f'{label} · {text.strip().splitlines()[0][:45]}'))
        entries.extend((f'task:{key}', f"{task['name']} · {task['step']}") for key, task in self.tasks.items())
        if not entries:
            return
        try:
            selected = await self.ask(kind='choice', title='工作详情', text='选择查看，Esc 返回对话', values=entries, record=False)
        finally:
            self.close_dialog()
        if selected is None:
            return
        if selected.startswith('message:'):
            index = int(selected.split(':')[1])
            self.expanded.add(index)
            self._view_cache = None
            self._prefix_cache = None
            await self.view_text({'assistant': '回复全文', 'progress': '思考摘要', 'system': '操作提示', 'execution':'执行记录'}[self._blocks[index][0]], self._blocks[index][1])
        else:
            task = self.tasks[selected[5:]]
            await self.view_text(task['name'], '\n'.join(task['details']))

    def _conversation(self):
        width = max(4, self.app.output.get_size().columns - 1)
        if self.home_visible and self.home_renderer:
            if self._home_cache is None or self._home_cache[0] != width:
                rendered = self.home_renderer(width)
                self._home_cache = (width, rendered)
                height = self.messages.render_info.window_height if self.messages.render_info else 1
                self.home_scroll = min(self.home_scroll, max(0, rendered.count('\n') + 1 - height))
            return self._home_cache[1]
        if self.busy and not self._local_command:
            elapsed = max(0, time.monotonic() - self.started)
            epoch = int(elapsed // 12)
            if epoch != self._loading_epoch:
                self._loading_word = choose_word(self._loading_word)
                self._loading_epoch = epoch
            loading = loading_text(elapsed, self._loading_word, plain=os.environ.get('TERM') == 'dumb')
        else:
            loading = ''
        key = (width, loading, self.browse_mode, self.detail_selection)
        if self._view_cache is None or self._view_cache[0] != key:
            self._detail_hotspots.clear()
            selection = self.detail_selection if self.browse_mode else None
            split = max((i for i, (role, _) in enumerate(self._blocks) if role == 'user'), default=0) if self.busy else 0
            prefix_key = (width, tuple(self._blocks[:split]), frozenset(self.expanded), selection)
            if self._prefix_cache is None or self._prefix_cache[0] != prefix_key:
                hotspots = {}
                prefix = render_conversation(self._blocks[:split], width, expanded=self.expanded,
                    hotspots=hotspots, selection=selection)
                self._prefix_cache = (prefix_key, prefix, hotspots)
            prefix, prefix_hotspots = self._prefix_cache[1:]
            tail_hotspots = {}
            tail = render_conversation(self._blocks[split:], width, tasks=list(self.tasks.values()),
                loading=loading, expanded=self.expanded, offset=split, hotspots=tail_hotspots,
                selection=selection - len(prefix_hotspots) if selection is not None else None)
            shift = prefix.count('\n') + 2 if prefix else 0
            self._detail_hotspots.update(prefix_hotspots)
            self._detail_hotspots.update({line + shift: indices for line, indices in tail_hotspots.items()})
            self._view_cache = (key, (prefix + '\n\n' if prefix else '') + tail)
        return self._view_cache[1]

    def _focus_home_panel(self):
        from .book_home import PANELS
        self._conversation()
        key = list(PANELS)[self.home_selection]
        self.home_scroll = self._home_positions.get(key, 0)
        self.app.invalidate()

    def _focus_detail(self):
        self._conversation()
        lines = list(self._detail_hotspots)
        if lines:
            row = lines[min(self.detail_selection, len(lines) - 1)]
            height = self.messages.render_info.window_height if self.messages.render_info else 1
            self.scroll = max(0, self._conversation().count('\n') + 1 - height - row)
        self.app.invalidate()

    def _click_history(self, position):
        if not self.browse_mode:
            return
        if self.home_visible and self.home_details_handler:
            from .book_home import PANELS
            width = self.app.output.get_size().columns - 1
            columns = 3 if width >= 150 else 2 if width >= 92 else 1
            keys = list(PANELS)
            row = max((line for line in self._home_positions.values() if line <= position.y), default=-1)
            choices = [key for key in keys if self._home_positions.get(key) == row]
            column = min(columns - 1, position.x * columns // max(1, width))
            if column < len(choices):
                self.home_selection = keys.index(choices[column])
                self._home_cache = None
                self.app.create_background_task(self.home_details_handler(self.home_selection))
            return
        indices = self._detail_hotspots.get(position.y)
        if indices:
            if isinstance(indices[0], str) and indices[0].startswith('task:'):
                task = list(self.tasks.values())[int(indices[0].split(':')[1])]
                self.app.create_background_task(self.view_text(task['name'], '\n'.join(task['details'])))
                return
            if any(index in self.expanded for index in indices):
                self.expanded.difference_update(indices)
            else:
                self.expanded.update(indices)
            self._view_cache = None
            self.app.invalidate()

    async def _send_steering(self, value):
        if value == '/browse':
            self.buffer.reset()
            self.enter_browse()
            return
        if value.startswith('/'):
            self.append('工作中可直接发送指令；命令菜单请在本轮结束后使用。\n')
            return
        if self.steer_handler is None:
            self.append('当前操作尚不能接收追加消息，输入内容已保留。\n')
            return
        self.buffer.append_to_history()
        self.buffer.reset()
        self.append(value, role='user')
        self.begin_work()
        animation = asyncio.create_task(self._animate_progress())
        try:
            await self.steer_handler(value)
        except Exception as exc:
            self.append(f'追加指令未送达：{exc}\n')
            if not self.buffer.text:
                self.buffer.document = Document(value, len(value))
        finally:
            animation.cancel()
            await asyncio.gather(animation, return_exceptions=True)
            self.end_work()

    def set_task(self, key, *, name, status, step, details):
        self.tasks[key] = {'name': name, 'status': status, 'step': step, 'details': details}
        self._view_cache = None
        self.app.invalidate()

    def stream_message(self, key, text, *, replace=False, role='assistant'):
        old_lines = self._conversation().count('\n') if self.scroll else 0
        if key not in self._stream_blocks:
            self._blocks.append((role, ''))
            self._stream_blocks[key] = len(self._blocks) - 1
        index = self._stream_blocks[key]
        previous = self._blocks[index][1]
        self._blocks[index] = (role, text if replace else previous + text)
        self._view_cache = None
        if self.scroll:
            self.scroll += max(0, self._conversation().count('\n') - old_lines)
        self.app.invalidate()

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
            self.expanded = {index - 1 for index in self.expanded if index > 0}
            self._stream_blocks = {key: index - 1 for key, index in self._stream_blocks.items() if index > 0}
        self._view_cache = None
        if self.scroll:
            self.scroll += max(0, self._conversation().count("\n") - old_lines)
        self.app.invalidate()

    def clear(self):
        self.transcript = ""
        self._blocks.clear()
        self.tasks.clear()
        self._stream_blocks.clear()
        self.expanded.clear()
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
        if self.home_visible:
            self.home_scroll = max(0, min(maximum, self.home_scroll - amount))
            self.app.invalidate()
            return
        self.scroll = max(0, min(maximum, self.scroll + amount))
        self.app.invalidate()

    def show_home(self):
        self.welcome = None
        self.home_visible = True
        self.home_scroll = 0
        self.browse_mode = False
        self._home_cache = None
        self.app.invalidate()

    def begin_work(self):
        self._work_count += 1
        self.busy = True
        self.app.invalidate()

    def end_work(self):
        self._work_count = max(0, self._work_count - 1)
        self.busy = self._work_count > 0
        self.app.invalidate()

    async def _dispatch(self, value):
        from .slash_commands import uses_model
        self._local_command = value.startswith('/') and not uses_model(value)
        self.begin_work()
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
            self.home_visible = False
            self.append(f"LG error: {exc}\n")
        finally:
            animation.cancel()
            await asyncio.gather(animation, return_exceptions=True)
            self._local_command = False
            self.end_work()

    def _progress_text(self):
        elapsed = max(0, time.monotonic() - self.started) if self.started else 0
        return [('class:lg-prompt', loading_text(elapsed, self._loading_word,
            plain=os.environ.get('TERM') == 'dumb'))]

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
        data = record.get('data') or {}
        stage = record.get('stage_id')
        if stage and kind in {'stage.started', 'stage.completed', 'stage.failed'}:
            key = str(record.get('run_id', '')) + ':' + stage
            self._workflow_task_keys.add(key)
            task = self.tasks.setdefault(key, {'name': str(data.get('agent') or stage),
                'status': 'running', 'step': '', 'details': []})
            task['status'] = {'stage.started': 'running', 'stage.completed': 'completed', 'stage.failed': 'failed'}[kind]
            task['step'] = str(record.get('message') or stage)
            task['details'].append(task['step'])
            self._view_cache = None
            if task['name'] == 'director':
                self.append(task['step'], role='progress')
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
        if self.interrupt_handler:
            await self.interrupt_handler()
        await self.stop_workflow_process()

    async def stop_workflow_process(self):
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
        self._workflow_task_keys = set()
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
            for key in self._workflow_task_keys:
                task = self.tasks.get(key)
                if task and task['status'] == 'running':
                    task['status'] = 'cancelled' if result in {-2, -9, 130, 137} else 'failed' if result else 'completed'
                    task['step'] = {'cancelled': '已停止', 'failed': '执行失败', 'completed': '已完成'}[task['status']]
            self._view_cache = None
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
        while True:
            await asyncio.sleep(1 / 15)
            if (
                (self.home_header_renderer is not None or self.welcome is not None)
                and not self.browse_mode and self.viewer is None and self.dialog is None
            ):
                self.app.invalidate()

    def _start(self):
        if (self.welcome or self.home_header_renderer) and os.environ.get("TERM") != "dumb":
            self.app.create_background_task(self._animate())

    def run(self, handler, model, provider, *, initial_command=None):
        self._handler = handler
        self.model, self.provider = model, provider

        def start():
            self._start()
            if self.home_refresh:
                self.app.create_background_task(self.home_refresh())
            if initial_command is not None:
                self.started = time.monotonic()
                self.busy = True
                self.app.create_background_task(self._dispatch(initial_command))

        async def run_session():
            try:
                return await self.app.run_async(pre_run=start)
            finally:
                if self.shutdown_handler:
                    await self.shutdown_handler()
        return asyncio.run(run_session())

    def prompt(self, model, provider):
        """Single-input adapter used by focused editor tests."""
        self.model, self.provider = model, provider
        self.buffer.reset()
        return self.app.run()
