from __future__ import annotations

import asyncio
import codecs
import os
import signal

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
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.styles import Style

from .slash_commands import matching_commands
from .slime_animation import FRAME_INTERVAL, prepare_frames


def input_style() -> Style:
    plain = {
        "bottom-toolbar": "noreverse",
        "bottom-toolbar.text": "noreverse",
        "command.selected": "reverse",
    }
    if "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
        return Style.from_dict(plain)
    return Style.from_dict(
        {
            **plain,
            "lg-rule": "#808080",
            "lg-prompt": "#dc795f bold",
            "bottom-toolbar.text": "#808080 noreverse",
            "command.selected": "#dc795f reverse",
            "command.description": "#808080",
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
        commands=(),
    ):
        self.model = ""
        self.provider = ""
        self.welcome = welcome
        self.compact_welcome = compact_welcome
        self.commands = list(commands)
        self.transcript = ""
        self.scroll = 0
        self.busy = False
        self.process = None
        self._handler = None
        self._task = None
        self._cancel_task = None
        self._matches = []
        self._selected = 0
        self._menu_hidden = False
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
            FormattedTextControl(
                lambda: ANSI(self.transcript),
                get_cursor_position=lambda: Point(
                    len(self.transcript.rsplit("\n", 1)[-1]) if not self.scroll else 0,
                    max(0, self.transcript.count("\n") - self.scroll),
                ),
            ),
            wrap_lines=True,
            always_hide_cursor=True,
        )
        rule = "-" if os.environ.get("TERM") == "dumb" else "\u2500"

        def welcome_text():
            size = get_app().output.get_size()
            compact = size.rows < 32 or size.columns < 40
            callback = self.compact_welcome if compact else self.welcome
            return ANSI(callback(max(1, size.columns - 1))) if callback else ""

        header = ConditionalContainer(
            Window(FormattedTextControl(welcome_text), dont_extend_height=True),
            filter=Condition(
                lambda: (
                    self.welcome is not None
                    and get_app().output.get_size().rows
                    >= (24 if self.compact_welcome else 32)
                    and get_app().output.get_size().columns >= 40
                )
            ),
        )
        self._header = header
        menu = ConditionalContainer(
            Window(
                FormattedTextControl(self._menu_text),
                height=lambda: min(len(self._matches), self._menu_rows()),
                dont_extend_height=True,
            ),
            filter=Condition(
                lambda: bool(self._matches) and not self._menu_hidden and not self.busy
            ),
        )
        content = HSplit(
            [
                header,
                self.messages,
                menu,
                Window(height=1, char=rule, style="class:lg-rule"),
                VSplit(
                    [
                        Window(
                            FormattedTextControl([("class:lg-prompt", "> ")]),
                            width=2,
                            height=1,
                            dont_extend_height=True,
                        ),
                        self.editor,
                    ]
                ),
                Window(height=1, char=rule, style="class:lg-rule"),
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
            self.welcome = None
            self.append("> " + value + "\n")
            self.busy = True
            self._task = event.app.create_background_task(self._dispatch(value))

        @keys.add("tab")
        def complete(event):
            if not self.busy:
                self._complete()

        @keys.add(
            "up",
            filter=Condition(lambda: bool(self._matches) and not self._menu_hidden),
        )
        def previous(event):
            self._selected = (self._selected - 1) % len(self._matches)

        @keys.add(
            "down",
            filter=Condition(lambda: bool(self._matches) and not self._menu_hidden),
        )
        def next_command(event):
            self._selected = (self._selected + 1) % len(self._matches)

        @keys.add("escape")
        def dismiss(event):
            self._menu_hidden = True

        @keys.add("escape", "enter")
        @keys.add("c-j")
        def newline(event):
            if not self.busy:
                self.buffer.insert_text("\n")

        @keys.add("pageup")
        def page_up(event):
            self.scroll = min(
                self.transcript.count("\n"),
                self.scroll + max(1, event.app.output.get_size().rows // 2),
            )

        @keys.add("pagedown")
        def page_down(event):
            self.scroll = max(
                0, self.scroll - max(1, event.app.output.get_size().rows // 2)
            )

        @keys.add("c-c")
        def cancel(event):
            if self.busy:
                if self._cancel_task is None or self._cancel_task.done():
                    self._cancel_task = event.app.create_background_task(
                        self.cancel_process()
                    )
            elif self._handler is None:
                event.app.exit(exception=KeyboardInterrupt())
            else:
                self.buffer.reset()

        @keys.add("c-d")
        def eof(event):
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
            mouse_support=False,
            color_depth=ColorDepth.DEPTH_24_BIT,
            erase_when_done=True,
            input=input,
            output=output,
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
        return max(1, min(8, size.rows // 3, size.rows - header_rows - 5))

    def _menu_text(self):
        start = max(0, self._selected - self._menu_rows() + 1)
        result = []
        for index in range(start, min(len(self._matches), start + self._menu_rows())):
            command = self._matches[index]
            if result:
                result.append(("", "\n"))
            result.append(
                (
                    "class:command.selected" if index == self._selected else "",
                    " " + command.text + "  ",
                )
            )
            result.append(("class:command.description", command.description))
        return result

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

    def append(self, text):
        self.transcript = (self.transcript + text)[-250000:]
        self.scroll = 0
        self.app.invalidate()

    def clear(self):
        self.transcript = ""
        self.scroll = 0
        self.app.invalidate()

    async def _dispatch(self, value):
        try:
            keep_running = await self._handler(value)
            if not keep_running:
                self.app.exit(result=0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep command failures inside the UI boundary.
            self.append(f"LG error: {exc}\n")
        finally:
            self.busy = False
            self.app.invalidate()

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

    async def run_command(self, argv):
        self.process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            limit=65536,
        )
        process = self.process
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while data := await process.stdout.read(4096):
                self.append(decoder.decode(data))
            self.append(decoder.decode(b"", final=True))
            result = await process.wait()
            if result:
                self.append(f"Command exited with code {result}\n")
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

    def run(self, handler, model, provider):
        self._handler = handler
        self.model, self.provider = model, provider
        if self.welcome and os.environ.get("TERM") != "dumb":
            prepare_frames()
        return self.app.run(pre_run=self._start)

    def prompt(self, model, provider):
        """Single-input adapter used by focused editor tests."""
        self.model, self.provider = model, provider
        self.buffer.reset()
        return self.app.run()
