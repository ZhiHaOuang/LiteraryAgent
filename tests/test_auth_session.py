import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from lg_cli.auth_ui import _manage_auth, run_auth
from lg_cli.credentials import read_profiles, save_profile
from lg_cli.terminal_input import LiteraryInput, choice_rows
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput


class AuthSessionTests(unittest.TestCase):
    def test_activate_profile_closes_dialog_without_error(self):
        async def exercise(editor, pipe):
            async def handle(_):
                await run_auth(editor, lambda: _manage_auth("test"))
                return False

            editor._handler = handle
            task = asyncio.create_task(editor.app.run_async())
            try:
                pipe.send_text("/auth\r")
                for title in ("LiteraryGiant | test", "one"):
                    for _ in range(300):
                        if editor.dialog and editor.dialog["title"] == title:
                            break
                        await asyncio.sleep(0.01)
                    else:
                        self.fail(f"Missing dialog: {title}")
                    pipe.send_text("\r")
                self.assertEqual(await asyncio.wait_for(task, 3), 0)
                self.assertIsNone(editor.dialog)
                self.assertIn("Activate", editor.transcript)
                self.assertNotIn("LG error", editor.transcript)
            finally:
                if not task.done():
                    if editor._task:
                        editor._task.cancel()
                        await asyncio.sleep(0.05)
                    editor.app.exit(result=0)
                    await task

        with (
            tempfile.TemporaryDirectory() as raw,
            create_pipe_input() as pipe,
            patch.dict(os.environ, {"HOME": raw}),
        ):
            save_profile("test", "one", "fake-one")
            save_profile("test", "two", "fake-two")
            editor = LiteraryInput(
                Path(raw) / "history", input=pipe, output=DummyOutput()
            )
            asyncio.run(exercise(editor, pipe))
            self.assertEqual(read_profiles("test")["active"], "one")

    def test_stale_session_fails_before_auth_changes(self):
        action = Mock()
        with self.assertRaisesRegex(TypeError, "restart literary"):
            asyncio.run(run_auth(object(), action))
        action.assert_not_called()

    def test_selection_has_rounded_lines_and_no_extra_blank_rows(self):
        with patch.dict(os.environ, {"TERM": "xterm-256color"}):
            fragments = choice_rows(["Activate", "Change model"], 0, 40)
        text = "".join(text for _, text in fragments)
        self.assertEqual(len(text.splitlines()), 4)
        self.assertEqual(text.splitlines()[0], " ╭" + "─" * 36 + "╮ ")
        self.assertTrue(text.splitlines()[1].startswith(" │Activate"))
        self.assertEqual(text.splitlines()[2], " ╰" + "─" * 36 + "╯ ")
        self.assertFalse(any(char in text for char in "▗▄▖▌▐▝▀▘"))

    def test_auth_stays_in_one_screen_and_never_records_key(self):
        class Output(DummyOutput):
            entered = 0
            left = 0
            size = Size(rows=32, columns=80)

            def get_size(self):
                return self.size

            def enter_alternate_screen(self):
                self.entered += 1

            def quit_alternate_screen(self):
                self.left += 1

        async def exercise(editor, output, pipe):
            async def handle(value):
                await run_auth(editor, lambda: _manage_auth("test"))
                return False

            async def wait_dialog(text):
                for _ in range(300):
                    if editor.dialog and editor.dialog["text"] == text:
                        return
                    await asyncio.sleep(0.01)
                self.fail(f"Dialog did not appear: {text}")

            editor._handler = handle
            task = asyncio.create_task(editor.app.run_async())
            try:
                pipe.send_text("/auth\r")
                await wait_dialog("Model profiles")
                for rows, columns in ((32, 80), (18, 40), (12, 24)):
                    output.size = Size(rows=rows, columns=columns)
                    editor.app._on_resize()
                    screen = editor.app.renderer.last_rendered_screen
                    lines = [
                        "".join(screen.data_buffer[y][x].char for x in range(columns))
                        for y in range(rows)
                    ]
                    self.assertIn("STATUS", lines[0])
                    self.assertTrue(any("DeepSeek" in line for line in lines[-10:]))
                    self.assertNotIn("Window too small", "\n".join(lines))
                output.size = Size(rows=32, columns=80)
                pipe.send_text("\r")
                for _ in range(300):
                    if editor.dialog and editor.dialog["title"] == "one":
                        break
                    await asyncio.sleep(0.01)
                pipe.send_text("\x1b[B\x1b[B\r")
                await wait_dialog("New API key")
                pipe.send_text("fake-private-replacement")
                await asyncio.sleep(0.05)
                editor.app._redraw()
                screen = editor.app.renderer.last_rendered_screen
                visible = "".join(
                    cell.char
                    for row in screen.data_buffer.values()
                    for cell in row.values()
                )
                self.assertNotIn("fake-private-replacement", visible)
                pipe.send_text("\r")
                await wait_dialog("Model profiles")
                self.assertEqual(output.entered, 1)
                self.assertEqual(output.left, 0)
                self.assertIn("Replace API key", editor.transcript)
                self.assertIn("[hidden]", editor.transcript)
                self.assertNotIn("fake-private-replacement", editor.transcript)
                self.assertEqual(editor.dialog_buffer.text, "")
                pipe.send_text("\x03")
                await asyncio.wait_for(task, 3)
                self.assertEqual(output.left, 1)
            finally:
                if not task.done():
                    if editor._task:
                        editor._task.cancel()
                        await asyncio.sleep(0.05)
                    editor.app.exit(result=0)
                    await task

        with (
            tempfile.TemporaryDirectory() as raw,
            create_pipe_input() as pipe,
            patch.dict(os.environ, {"HOME": raw, "TERM": "xterm-256color"}),
        ):
            save_profile("test", "one", "fake-original")
            output = Output()
            history = Path(raw) / "history"
            editor = LiteraryInput(
                history,
                input=pipe,
                output=output,
                welcome=lambda width: "WELCOME",
                status_bar=lambda width: "STATUS",
            )
            asyncio.run(exercise(editor, output, pipe))
            self.assertEqual(
                read_profiles("test")["profiles"]["one"]["key"],
                "fake-private-replacement",
            )
            self.assertNotIn("fake-private-replacement", history.read_text())
