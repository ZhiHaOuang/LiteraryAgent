import asyncio
import sys
import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from lg_cli.dashboard import render_dashboard
from lg_cli.main import build_parser
from lg_cli.slash_commands import command_catalog, matching_commands
from lg_cli.terminal_input import LiteraryInput
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from tests.helpers import make_config


class SlashCommandTests(unittest.TestCase):
    def test_menu_preserves_real_welcome_and_bottom_input(self):
        from prompt_toolkit.data_structures import Size

        class Output(DummyOutput):
            size = Size(rows=40, columns=80)

            def get_size(self):
                return self.size

        async def exercise(editor, output):
            task = asyncio.create_task(editor.app.run_async())
            try:
                for rows in (40, 32, 24, 40):
                    output.size = Size(rows=rows, columns=80)
                    for value in ("/", "/sta", ""):
                        editor.buffer.text = value
                        await asyncio.sleep(0.03)
                        editor.app._on_resize()
                        screen = editor.app.renderer.last_rendered_screen
                        lines = [
                            "".join(screen.data_buffer[y][x].char for x in range(80))
                            for y in range(rows)
                        ]
                        self.assertEqual(
                            sum("LiteraryGiant" in line for line in lines), 1
                        )
                        self.assertTrue(any("What shall" in line for line in lines))
                        self.assertTrue(
                            any("auto (core default)" in line for line in lines)
                        )
                        self.assertTrue(lines[rows - 3].startswith("> " + value))
                        if value:
                            self.assertTrue(
                                any(
                                    "/status" in line
                                    if value == "/sta"
                                    else "/" in line
                                    for line in lines[:-4]
                                )
                            )
                        self.assertIsNotNone(editor.welcome)
            finally:
                editor.app.exit(result="")
                await task

        with (
            tempfile.TemporaryDirectory() as raw,
            create_pipe_input() as pipe,
            patch.dict(os.environ, {"TERM": "xterm-256color"}),
        ):
            config = make_config(Path(raw))
            output = Output()

            def welcome(width, compact=False):
                return render_dashboard(
                    config,
                    mode="interactive",
                    skills_count=0,
                    agents_count=0,
                    terminal_width=width,
                    compact=compact,
                )

            editor = LiteraryInput(
                Path(raw) / "history",
                input=pipe,
                output=output,
                welcome=welcome,
                compact_welcome=lambda width: welcome(width, True),
                commands=command_catalog(build_parser()),
            )
            asyncio.run(exercise(editor, output))

    def test_menu_is_above_anchored_input_during_resize_and_output(self):
        from prompt_toolkit.data_structures import Size

        class Output(DummyOutput):
            size = Size(rows=40, columns=80)

            def get_size(self):
                return self.size

        async def exercise(editor, output):
            task = asyncio.create_task(editor.app.run_async())
            try:
                for rows, columns in ((40, 80), (18, 40), (30, 100)):
                    output.size = Size(rows=rows, columns=columns)
                    editor.buffer.text = "/sta"
                    editor.append("output\n" * 50)
                    await asyncio.sleep(0.03)
                    editor.app._on_resize()
                    screen = editor.app.renderer.last_rendered_screen
                    lines = [
                        "".join(screen.data_buffer[y][x].char for x in range(columns))
                        for y in range(rows)
                    ]
                    offset = 5 if os.environ.get("TERM") == "dumb" else 6
                    self.assertIn("/status", lines[rows - offset])
                    self.assertTrue(lines[rows - 3].startswith("> /sta"))
                    editor._menu_hidden = True
                    editor.busy = True
                    editor.app._redraw()
                    screen = editor.app.renderer.last_rendered_screen
                    self.assertEqual(
                        "".join(screen.data_buffer[rows - 3][x].char for x in range(2)),
                        "> ",
                    )
                    editor.busy = False
                    editor._menu_hidden = False
            finally:
                editor.app.exit(result="")
                await task

        with tempfile.TemporaryDirectory() as raw, create_pipe_input() as pipe:
            output = Output()
            editor = LiteraryInput(
                Path(raw) / "history",
                input=pipe,
                output=output,
                commands=command_catalog(build_parser()),
            )
            asyncio.run(exercise(editor, output))

    def test_catalog_tracks_real_commands_and_subcommands(self):
        catalog = command_catalog(build_parser())
        roots = {command.text for command in matching_commands("/", catalog)}
        self.assertTrue(
            {"/help", "/auth", "/model", "/run", "/chapter", "/clear", "/exit"} <= roots
        )
        self.assertEqual(
            [c.text for c in matching_commands("/sta", catalog)], ["/status"]
        )
        self.assertIn(
            "/run resume", [c.text for c in matching_commands("/run r", catalog)]
        )
        self.assertEqual(matching_commands("a /status", catalog), [])

    def test_tab_and_enter_complete_without_immediate_execution(self):
        for keys, expected in (
            ("/sta\t\r", "/status"),
            ("/sta\r\r", "/status"),
            ("/run r\t id-123\r", "/run resume id-123"),
            ("/ru\rli\t\r", "/run list"),
        ):
            with (
                self.subTest(keys=keys),
                tempfile.TemporaryDirectory() as raw,
                create_pipe_input() as pipe,
            ):
                editor = LiteraryInput(
                    Path(raw) / "history",
                    input=pipe,
                    output=DummyOutput(),
                    commands=command_catalog(build_parser()),
                )
                pipe.send_text(keys)
                value = editor.prompt("model", "provider")
                self.assertEqual(value, expected)

    def test_streamed_subprocess_and_cancellation(self):
        async def exercise(editor):
            task = asyncio.create_task(
                editor.run_command(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        "import time; print('ready', flush=True); time.sleep(30)",
                    ]
                )
            )
            for _ in range(200):
                if "ready" in editor.transcript:
                    break
                await asyncio.sleep(0.01)
            self.assertIn("ready", editor.transcript)
            await editor.cancel_process()
            self.assertNotEqual(await asyncio.wait_for(task, 5), 0)
            self.assertIsNone(editor.process)

        with tempfile.TemporaryDirectory() as raw, create_pipe_input() as pipe:
            editor = LiteraryInput(
                Path(raw) / "history", input=pipe, output=DummyOutput()
            )
            asyncio.run(exercise(editor))
