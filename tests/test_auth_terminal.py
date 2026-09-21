from __future__ import annotations

import fcntl
import io
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
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from lg_cli.auth_ui import radiolist_dialog


class AuthTerminalTests(unittest.TestCase):
    def test_invalid_number_does_not_select_a_profile(self):
        output = io.StringIO()
        with (
            patch("builtins.input", side_effect=["wrong", "9", "2"]),
            redirect_stdout(output),
        ):
            selected = radiolist_dialog(
                title="Profiles", text="", values=[("one", "First"), ("two", "Second")]
            ).run()
        self.assertEqual(selected, "two")
        self.assertEqual(output.getvalue().count("Invalid number."), 2)
        self.assertNotIn("\x1b", output.getvalue())

    def test_key_is_saved_without_terminal_echo(self):
        with tempfile.TemporaryDirectory() as home:
            master, slave = pty.openpty()
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
                (b"> ", b"1\n"),
                (b"> ", b"2\n"),
                (b"Profile name [deepseek]: ", b"\n"),
                (b"Model ID available to your account [deepseek-flash]: ", b"\n"),
                (b"Base URL [https://api.deepseek.com/anthropic]: ", b"\n"),
                (b"API key: ", secret + b"\n"),
                (b"> ", b"0\n"),
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
                self.assertEqual(index, len(steps))
                self.assertEqual(process.wait(timeout=2), 0)
                self.assertNotIn(secret, output)
                self.assertNotIn(b"\x1b", output)
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
                            os.write(master, b"0\n")
                            sent_cancel = True
                    self.assertIn(b"Add provider", output)
                    self.assertIn(b"LiteraryGiant", output)
                    self.assertNotIn(b"\x1b", output)
                    self.assertTrue(output.isascii())
                    self.assertEqual(process.wait(timeout=2), 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    os.close(master)
