from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from lg_cli.auth_ui import choice_application
from lg_cli.slime_animation import BODY, CROWN
from lg_cli.terminal_input import input_style
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput


class AuthTerminalTests(unittest.TestCase):
    def test_selection_is_orange_and_scrolls_in_small_terminal(self):
        class Output(DummyOutput):
            def get_size(self):
                return Size(rows=18, columns=40)

        with patch.dict(os.environ, {"TERM": "xterm-256color"}):
            os.environ.pop("NO_COLOR", None)
            style = input_style().get_attrs_for_style_str("class:command.selected")
            self.assertEqual(style.bgcolor, BODY.lstrip("#"))
            self.assertFalse(style.reverse)
            for selector in ("command.edge", "command.side"):
                border = input_style().get_attrs_for_style_str(f"class:{selector}")
                self.assertEqual(border.color, CROWN.lstrip("#"))
                self.assertEqual(border.bgcolor, "default")
                self.assertFalse(border.reverse)

        async def exercise(app, pipe):
            task = asyncio.create_task(app.run_async())
            try:
                pipe.send_text("\x1b[B" * 15)
                await asyncio.sleep(0.05)
                app._redraw()
                screen = app.renderer.last_rendered_screen
                lines = [
                    "".join(screen.data_buffer[y][x].char for x in range(40))
                    for y in range(screen.height)
                ]
                self.assertTrue(any("Profile 15" in line for line in lines))
                pipe.send_text("\r")
                self.assertEqual(await asyncio.wait_for(task, 2), 15)
            finally:
                if not task.done():
                    app.exit(result=None)
                    await task

        with create_pipe_input() as pipe:
            app = choice_application(
                title="Profiles",
                text="",
                values=[(i, f"Profile {i}") for i in range(30)],
                input=pipe,
                output=Output(),
            )
            asyncio.run(exercise(app, pipe))

    def test_arrow_selection_and_cancel(self):
        for keys, expected in (
            ("\x1b[B\r", "two"),
            ("\x1b[A\r", "two"),
            ("\x03", None),
            ("2\r", "one"),
        ):
            with self.subTest(keys=keys), create_pipe_input() as pipe:
                app = choice_application(
                    title="Profiles",
                    text="",
                    values=[("one", "First"), ("two", "Second")],
                    input=pipe,
                    output=DummyOutput(),
                )
                pipe.send_text(keys)
                self.assertEqual(app.run(), expected)

    def test_key_is_saved_without_terminal_echo(self):
        with tempfile.TemporaryDirectory() as home:
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 100, 0, 0))
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "lg_cli",
                    "--environment",
                    "terminal-test",
                    "auth",
                ],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env={**os.environ, "HOME": home, "TERM": "xterm-256color"},
                cwd=home,
            )
            os.close(slave)
            secret = b"fake-terminal-only-secret"
            steps = [
                (b"Add provider", b"\r"),
                (b"Connection type", b"\x1b[B\r"),
                (b"Profile name", b"\r"),
                (b"account", b"\r"),
                (b"/anthropic", b"\r"),
                (b"API key", secret + b"\r"),
                (b"Model profiles", b"\x03"),
            ]
            output = b""
            cursor = 0
            index = 0
            deadline = time.monotonic() + 10
            try:
                while time.monotonic() < deadline and process.poll() is None:
                    if select.select([master], [], [], 0.1)[0]:
                        try:
                            output += os.read(master, 65536)
                        except OSError:
                            break
                    if index < len(steps) and steps[index][0] in output[cursor:]:
                        cursor = len(output)
                        os.write(master, steps[index][1])
                        index += 1
                self.assertEqual(
                    index,
                    len(steps),
                    output[-2500:]
                    .replace(secret, b"[hidden]")
                    .decode(errors="replace"),
                )
                self.assertEqual(process.wait(timeout=2), 0)
                self.assertNotIn(secret, output)
                saved = json.loads(
                    (
                        Path(home)
                        / ".literarygiant/environments/terminal-test/credentials.json"
                    ).read_text()
                )
                self.assertEqual(saved["profiles"]["deepseek"]["key"], secret.decode())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                os.close(master)

    def test_auth_screen_renders_and_cancels_in_wide_and_narrow_terminal(self):
        for columns in (100, 40):
            with self.subTest(columns=columns), tempfile.TemporaryDirectory() as home:
                master, slave = pty.openpty()
                fcntl.ioctl(
                    slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, columns, 0, 0)
                )
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "lg_cli",
                        "--environment",
                        "terminal-test",
                        "auth",
                    ],
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    env={**os.environ, "HOME": home, "TERM": "xterm-256color"},
                    cwd=home,
                )
                os.close(slave)
                output = b""
                sent_cancel = False
                deadline = time.monotonic() + 10
                try:
                    while time.monotonic() < deadline and process.poll() is None:
                        if select.select([master], [], [], 0.1)[0]:
                            try:
                                output += os.read(master, 65536)
                            except OSError:
                                break
                        if b"Add provider" in output and not sent_cancel:
                            os.write(master, b"\x03")
                            sent_cancel = True
                    self.assertIn(b"Add provider", output)
                    self.assertIn(b"LiteraryGiant", output)
                    self.assertNotIn(b"0. Back", output)
                    self.assertEqual(output.count(b"\x1b[?1049h"), 1)
                    self.assertEqual(output.count(b"\x1b[?1049l"), 1)
                    self.assertEqual(process.wait(timeout=2), 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    os.close(master)
