import unittest
import tempfile
import io
import asyncio
import os
from unittest.mock import patch
from pathlib import Path
from contextlib import redirect_stdout
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.data_structures import Size
from lg_cli.terminal_input import LiteraryInput
from lg_cli.dashboard import render_dashboard
from tests.helpers import make_config


class TerminalInputTests(unittest.TestCase):
    def test_real_welcome_switches_to_static_on_small_terminal(self):
        class ResizableOutput(DummyOutput):
            size = Size(rows=40, columns=80)

            def get_size(self):
                return self.size

        async def exercise(editor, output):
            task = asyncio.create_task(editor.app.run_async())
            try:
                for rows in (40, 24, 32, 40):
                    output.size = Size(rows=rows, columns=80)
                    await asyncio.sleep(0.03)
                    editor.app._on_resize()
                    screen = editor.app.renderer.last_rendered_screen
                    lines = ["".join(screen.data_buffer[y][x].char for x in range(80)) for y in range(screen.height)]
                    title = next(i for i, line in enumerate(lines) if "What shall" in line)
                    model = next(i for i, line in enumerate(lines) if "auto (core default)" in line)
                    self.assertEqual(sum("LiteraryGiant" in line for line in lines), 1)
                    self.assertEqual(sum(line.startswith("\u2500") for line in lines), 2)
                    self.assertEqual(model - title - 1, 12 if rows >= 32 else 8)
            finally:
                editor.app.exit(result="")
                await task

        with tempfile.TemporaryDirectory() as raw, create_pipe_input() as pipe, patch.dict(os.environ, {"TERM": "xterm-256color"}):
            config = make_config(Path(raw))
            output = ResizableOutput()
            def welcome(width, compact=False):
                return render_dashboard(config, mode="interactive", skills_count=0, agents_count=0, terminal_width=width, compact=compact)
            editor = LiteraryInput(Path(raw) / "history", input=pipe, output=output, welcome=welcome, compact_welcome=lambda width: welcome(width, True))
            asyncio.run(exercise(editor, output))

    def test_persistent_session_enters_alternate_screen_once(self):
        class RecordingOutput(DummyOutput):
            entered = 0
            left = 0

            def enter_alternate_screen(self):
                self.entered += 1

            def quit_alternate_screen(self):
                self.left += 1

        with tempfile.TemporaryDirectory() as raw, create_pipe_input() as pipe:
            output = RecordingOutput()
            editor = LiteraryInput(Path(raw) / "history", input=pipe, output=output, welcome=lambda width: "WELCOME")
            async def exercise():
                calls = []
                async def handler(value):
                    calls.append(value)
                    editor.append("reply\n")
                    return value != "/exit"
                editor._handler = handler
                task = asyncio.create_task(editor.app.run_async())
                pipe.send_text("first\r")
                while editor.welcome is not None or editor.busy:
                    await asyncio.sleep(0.01)
                self.assertTrue(editor.app.full_screen)
                self.assertIn("reply", editor.transcript)
                self.assertEqual(output.entered, 1)
                self.assertEqual(output.left, 0)
                pipe.send_text("/exit\r")
                self.assertEqual(await asyncio.wait_for(task, 2), 0)
                self.assertEqual(calls, ["first", "/exit"])
                self.assertEqual(output.entered, 1)
                self.assertGreaterEqual(output.left, 1)
            asyncio.run(exercise())

    def test_resize_keeps_exactly_two_rules_and_fits_screen(self):
        class ResizableOutput(DummyOutput):
            size = Size(rows=40, columns=100)

            def get_size(self):
                return self.size

        async def exercise(editor, output):
            task = asyncio.create_task(editor.app.run_async())
            try:
                for rows, columns in [(40, 100), (32, 40), (12, 24), (40, 100)]:
                    output.size = Size(rows=rows, columns=columns)
                    await asyncio.sleep(0.03)
                    editor.app._on_resize()
                    screen = editor.app.renderer.last_rendered_screen
                    self.assertIsNotNone(screen)
                    lines = ["".join(screen.data_buffer[y][x].char for x in range(columns)) for y in range(screen.height)]
                    self.assertEqual(sum("\u2500" in line for line in lines), 2)
                    occupied = [index for index, line in enumerate(lines) if line.strip()]
                    self.assertEqual(max(occupied), rows - 1)
                    self.assertTrue(lines[rows - 3].startswith("> "))
                    self.assertTrue(all(line[-1] == " " for line in lines))
                    self.assertEqual(any("WELCOME" in line for line in lines), rows >= 32 and columns >= 40)
            finally:
                editor.app.exit(result="")
                await task

        with tempfile.TemporaryDirectory() as raw, create_pipe_input() as pipe, patch.dict(os.environ, {"TERM": "xterm-256color"}):
            output = ResizableOutput()
            editor = LiteraryInput(Path(raw) / "history", input=pipe, output=output, welcome=lambda width: f"WELCOME {width}")
            asyncio.run(exercise(editor, output))

    def test_submit_multiline_and_bracketed_paste(self):
        for keys, expected in [
            ("hello\r", "hello"),
            ("one\x1b\rtwo\r", "one\ntwo"),
            ("one\ntwo\r", "one\ntwo"),
            ("\x1b[200~one\ntwo\x1b[201~\r", "one\ntwo"),
        ]:
            with self.subTest(keys=repr(keys)), tempfile.TemporaryDirectory() as raw, create_pipe_input() as pipe:
                editor = LiteraryInput(Path(raw) / "history", input=pipe, output=DummyOutput())
                pipe.send_text(keys)
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(editor.prompt("model", "provider"), expected)
                self.assertIn(expected.splitlines()[0], (Path(raw) / "history").read_text())

    def test_cancel_and_eof(self):
        for key, exception in [("\x03", KeyboardInterrupt), ("\x04", EOFError)]:
            with tempfile.TemporaryDirectory() as raw, create_pipe_input() as pipe:
                editor = LiteraryInput(Path(raw) / "history", input=pipe, output=DummyOutput())
                pipe.send_text(key)
                with self.assertRaises(exception):
                    editor.prompt("model", "provider")
