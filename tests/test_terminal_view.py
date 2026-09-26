import asyncio
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from lg_cli.dashboard import render_dashboard, render_status_bar
from lg_cli.main import build_parser
from lg_cli.slash_commands import command_catalog
from lg_cli.terminal_input import LiteraryInput
from lg_cli.terminal_view import choice_rows, render_conversation
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.utils import get_cwidth
from rich.text import Text

from tests.helpers import make_config


class TerminalViewTests(unittest.TestCase):
    def test_welcome_long_labels_resize_with_menu_and_bottom_input(self):
        class Output(DummyOutput):
            size = Size(rows=40, columns=120)

            def get_size(self):
                return self.size

        async def exercise(editor, output):
            task = asyncio.create_task(editor.app.run_async())
            try:
                for rows, columns in (
                    (40, 120),
                    (50, 180),
                    (24, 40),
                    (50, 180),
                    (32, 80),
                    (24, 40),
                    (18, 40),
                    (12, 24),
                    (9, 18),
                    (40, 120),
                ):
                    output.size = Size(rows=rows, columns=columns)
                    for text in ("", "/"):
                        editor.buffer.text = text
                        editor.app._on_resize()
                        await asyncio.sleep(0.03)
                        screen = editor.app.renderer.last_rendered_screen
                        lines = [
                            "".join(
                                screen.data_buffer[y][x].char for x in range(columns)
                            )
                            for y in range(rows)
                        ]
                        self.assertTrue(lines[rows - 3].startswith("> " + text), lines)
                        if rows >= 12:
                            self.assertTrue(lines[0].startswith("╭"), lines)
                            self.assertEqual(lines[0][columns - 2], "╮", lines)
                            self.assertTrue(
                                any(line.startswith("╰") for line in lines[:-4]), lines
                            )
                        if text:
                            self.assertTrue(
                                any("/" in line for line in lines[:-4]), lines
                            )
            finally:
                editor.app.exit(result=0)
                await task

        with (
            tempfile.TemporaryDirectory() as raw,
            create_pipe_input() as pipe,
            patch.dict(os.environ, {"TERM": "xterm-256color", "NO_COLOR": "1"}),
        ):
            config = replace(
                make_config(Path(raw) / ("long-path-" * 20)),
                default_model="long-model-" * 20,
            )
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
                compact_welcome=lambda w: welcome(w, True),
                minimal_welcome=lambda w: render_status_bar(config, w, compact=True),
                commands=command_catalog(build_parser()),
            )
            asyncio.run(exercise(editor, output))

    def test_menu_columns_and_compact_selection(self):
        with patch.dict(os.environ, {"TERM": "xterm-256color"}):
            for width in (18, 40, 80):
                fragments = choice_rows(
                    [("/auth", "Models"), ("/world", "世界观设定")], 0, width
                )
                lines = "".join(t for _, t in fragments).splitlines()
                self.assertEqual([get_cwidth(t) for t in lines], [width] * 4)
                self.assertTrue(lines[1].startswith(" │/auth"))
                self.assertTrue(lines[1].endswith("Models│ "))
                if width >= 40:
                    self.assertTrue(lines[3].endswith("世界观设定  "))
                else:
                    self.assertTrue(lines[3].endswith("…  "))

    def test_selection_insets_border_without_moving_fill_or_neighbor_rows(self):
        with patch.dict(os.environ, {"TERM": "xterm-256color"}):
            for width in (4, 8, 24, 80):
                labels = ["Previous", "中文选项", "Next"]
                fragments = choice_rows(labels, 1, width)
                rows = "".join(text for _, text in fragments).splitlines()
                self.assertEqual(len(rows), 5)
                self.assertEqual([get_cwidth(row) for row in rows], [width] * 5)
                sides = [text for style, text in fragments if style == "class:command.side"]
                self.assertEqual(sides, [" │", "│ "])
                self.assertEqual(rows[1], " ╭" + "─" * (width - 4) + "╮ ")
                self.assertEqual(rows[3], " ╰" + "─" * (width - 4) + "╯ ")
                fill = [text for style, text in fragments if style == "class:command.selected"]
                self.assertEqual(get_cwidth(fill[0]), width - 4)
                plain = "".join(text for _, text in choice_rows(labels, -1, width)).splitlines()
                self.assertEqual(rows[0], plain[0])
                self.assertEqual(rows[4], plain[2])
                self.assertEqual(rows[2][2:-2], plain[1][2:-2])

    def test_conversation_roles_markdown_and_reflow(self):
        with patch.dict(os.environ, {"TERM": "xterm-256color"}):
            for width in (24, 80):
                rendered = render_conversation(
                    [
                        ("user", "帮我写一个小说开头"),
                        ("assistant", "你好！\n\n**The opening**\n\n- A bell rings."),
                    ],
                    width,
                )
                text = Text.from_ansi(rendered).plain
                self.assertIn("> 帮我", text)
                self.assertIn("● 你好！", text)
                self.assertNotIn("**", text)
                self.assertTrue(
                    all(get_cwidth(line) <= width for line in text.splitlines())
                )

    def test_long_header_keeps_icon_and_frame_during_resize(self):
        class Output(DummyOutput):
            size = Size(rows=32, columns=80)

            def get_size(self):
                return self.size

        async def exercise(editor, output):
            task = asyncio.create_task(editor.app.run_async())
            try:
                for rows, columns in (
                    (32, 80),
                    (24, 40),
                    (12, 24),
                    (40, 120),
                    (24, 40),
                ):
                    output.size = Size(rows=rows, columns=columns)
                    editor.app._on_resize()
                    await asyncio.sleep(0.03)
                    screen = editor.app.renderer.last_rendered_screen
                    lines = [
                        "".join(screen.data_buffer[y][x].char for x in range(columns))
                        for y in range(rows)
                    ]
                    self.assertTrue(lines[0].startswith("╭"))
                    bottom = 4 if rows < 20 else 6
                    self.assertTrue(
                        lines[bottom].startswith("╰"), (rows, columns, lines)
                    )
                    if rows >= 20:
                        self.assertIn("▄███████▄", lines[3])
                        self.assertIn("▀██▄████▀", lines[5])
                    self.assertTrue(lines[rows - 3].startswith("> "))
            finally:
                editor.app.exit(result=0)
                await task

        with (
            tempfile.TemporaryDirectory() as raw,
            create_pipe_input() as pipe,
            patch.dict(os.environ, {"TERM": "xterm-256color", "NO_COLOR": "1"}),
        ):
            config = replace(
                make_config(Path(raw) / ("long-path-" * 20)),
                default_model="long-model-" * 20,
            )
            output = Output()
            editor = LiteraryInput(
                Path(raw) / "history",
                input=pipe,
                output=output,
                status_bar=lambda w: render_status_bar(
                    config, w, compact=output.size.rows < 20
                ),
            )
            editor.append("Old output\n" * 100)
            asyncio.run(exercise(editor, output))

    def test_structured_subprocess_chunking_and_clean_answer(self):
        records = [
            {"type": "context.started"},
            {"type": "stage.started", "stage_id": "answer"},
            {"type": "model.event", "data": {"engine_event": "private reasoning"}},
            {"type": "artifact.written", "message": "/private/path"},
            {"type": "run.result", "text": "你好，钟声响了。", "exit_code": 0},
        ]
        payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in records)
        code = (
            "import os,time\n"
            f"data={payload.encode()!r}\n"
            "for i in range(0,len(data),7):\n"
            " os.write(1,data[i:i+7])\n"
            " time.sleep(0.001)\n"
        )
        with tempfile.TemporaryDirectory() as raw, create_pipe_input() as pipe:
            editor = LiteraryInput(
                Path(raw) / "history", input=pipe, output=DummyOutput()
            )
            result = asyncio.run(
                editor.run_command([sys.executable, "-c", code], structured=True)
            )
            self.assertEqual(result, 0)
            self.assertIn(("assistant", "你好，钟声响了。"), editor._blocks)
            self.assertNotIn("private reasoning", editor.transcript)
            self.assertNotIn("/private/path", editor.transcript)
            self.assertIn("Completed in", editor.transcript)
            self.assertIsNone(editor.process)

    def test_missing_result_and_process_failure_are_visible(self):
        with tempfile.TemporaryDirectory() as raw, create_pipe_input() as pipe:
            editor = LiteraryInput(
                Path(raw) / "history", input=pipe, output=DummyOutput()
            )
            asyncio.run(
                editor.run_command([sys.executable, "-c", "pass"], structured=True)
            )
            self.assertIn("No workflow result received", editor.transcript)
            editor.clear()
            result = asyncio.run(
                editor.run_command(
                    [
                        sys.executable,
                        "-c",
                        "import sys;sys.stderr.write('broken');sys.exit(2)",
                    ],
                    structured=True,
                )
            )
            self.assertEqual(result, 2)
            self.assertIn("broken", editor.transcript)
            self.assertIn("Failed in", editor.transcript)

    def test_progress_animation_changes_without_transcript_growth(self):
        with tempfile.TemporaryDirectory() as raw, create_pipe_input() as pipe:
            editor = LiteraryInput(
                Path(raw) / "history", input=pipe, output=DummyOutput()
            )
            editor.started = 10
            with patch("lg_cli.terminal_input.time.monotonic", return_value=11):
                first = editor._progress_text()
            with patch("lg_cli.terminal_input.time.monotonic", return_value=11.125):
                second = editor._progress_text()
            self.assertNotEqual(first[0], second[0])
            self.assertEqual(editor.transcript, "")
