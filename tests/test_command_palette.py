import asyncio
import tempfile
import unittest
from pathlib import Path
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from lg_cli.main import build_parser
from lg_cli.slash_commands import CommandUsage, command_catalog, matching_commands
from lg_cli.terminal_input import LiteraryInput


class PaletteTests(unittest.TestCase):
    def test_usage_shared_and_private(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'personal' / 'usage.json'
            commands = command_catalog(build_parser())
            usage = CommandUsage(path)
            self.assertEqual([c.text for c in usage.favorites(commands)], ['/resume', '/novel', '/outlines'])
            usage.record('/doctor', commands)
            self.assertEqual(CommandUsage(path).favorites(commands)[0].text, '/doctor')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_search_description_and_nested_commands(self):
        catalog = command_catalog(build_parser())
        self.assertEqual([c.text for c in matching_commands('/已有大纲', catalog)], ['/outlines'])
        self.assertIn('/run resume', [c.text for c in matching_commands('/resume', catalog)] + [c.text for c in matching_commands('/run r', catalog)])
        self.assertEqual(matching_commands('/run resume abc', catalog), [])
        command = next(c for c in catalog if c.text == '/run resume')
        self.assertEqual(command.arguments[0].name, 'run_id')

    def test_enter_executes_and_tab_only_completes(self):
        async def exercise(editor, pipe):
            task = asyncio.create_task(editor.app.run_async())
            try:
                pipe.send_text('/sta\t')
                await asyncio.sleep(.08)
                self.assertFalse(task.done())
                self.assertEqual(editor.buffer.text, '/status')
                pipe.send_text('\r')
                self.assertEqual(await asyncio.wait_for(task, 2), '/status')
            finally:
                if not task.done():
                    editor.app.exit()
                    await task
        with tempfile.TemporaryDirectory() as raw, create_pipe_input() as pipe:
            editor = LiteraryInput(Path(raw)/'history', input=pipe, output=DummyOutput(),
                commands=command_catalog(build_parser()), usage_path=Path(raw)/'usage.json')
            asyncio.run(exercise(editor, pipe))

    def test_single_enter_and_parameter_guide(self):
        async def exercise(editor, pipe, keys, expected):
            task = asyncio.create_task(editor.app.run_async())
            try:
                first, *rest = keys.split('\r', 1)
                pipe.send_text(first + '\r')
                await asyncio.sleep(.06)
                if rest and rest[0]:
                    pipe.send_text(rest[0])
                self.assertEqual(await asyncio.wait_for(task, 3), expected)
            finally:
                if not task.done():
                    editor.app.exit()
                    await task
        for keys, expected in [('/sta\r', '/status'), ('/run resume\rbook-123\r', '/run resume book-123')]:
            with self.subTest(keys=keys), tempfile.TemporaryDirectory() as raw, create_pipe_input() as pipe:
                editor = LiteraryInput(Path(raw)/'history', input=pipe, output=DummyOutput(),
                    commands=command_catalog(build_parser()), usage_path=Path(raw)/'usage.json')
                asyncio.run(exercise(editor, pipe, keys, expected))
