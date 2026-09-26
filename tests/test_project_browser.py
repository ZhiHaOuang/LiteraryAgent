from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from lg_cli.project_browser import browse_project, project_entries
from lg_cli.project_store import ProjectStore
from lg_cli.terminal_input import LiteraryInput
from lg_cli.main import interactive_loop
from tests.helpers import make_config
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput


class ProjectBrowserTests(unittest.TestCase):
    def test_list_aliases_open_reader_without_creating_conversations(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ProjectStore(root).initialize()
            session = Mock()
            async def exercise(handler):
                for command in ('/chapter list', '/analysis list', '/bible list', '/timeline list', '/foreshadowing list'):
                    self.assertTrue(await handler(command))
                return 0
            session.run.side_effect = lambda handler, *_args, **_kwargs: asyncio.run(exercise(handler))
            with patch('lg_cli.main.remember_location'), patch('lg_cli.main.LiteraryInput', return_value=session), patch('lg_cli.project_browser.browse_project', new_callable=AsyncMock) as browse:
                self.assertEqual(interactive_loop(make_config(root)), 0)
                self.assertEqual([call.args[2] for call in browse.call_args_list],
                                 ['chapters', 'analyses', 'bible', 'timeline', 'storylines'])
            session.run_command.assert_not_called()
            self.assertEqual(list((root / '.literarygiant/conversations').glob('*.jsonl')), [])

    def test_read_queries_are_project_local_and_do_not_create_conversations(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = ProjectStore(root / "one")
            store.initialize(name="One")
            store.add_fact(category="character", key="Alice", value="Navigator", state="canonical")
            store.add_fact(category="character", key="Unconfirmed", value="Draft", state="idea")
            store.add_timeline_entry(label="Arrival", event="Ship arrives", state="canonical")
            store.add_foreshadowing(title="Missing letter", setup_note="A sealed envelope")
            other = ProjectStore(root / "two")
            other.initialize(name="Two")
            self.assertEqual([entry.title for entry in project_entries(store, "characters")], ["Alice"])
            self.assertEqual([entry.title for entry in project_entries(store, "events")], ["Arrival"])
            self.assertEqual([entry.title for entry in project_entries(store, "storylines")], ["Missing letter"])
            self.assertEqual(project_entries(other, "characters"), [])
            self.assertFalse((store.root / "conversations").exists())

    def test_detail_returns_to_list_then_back_exits(self):
        with tempfile.TemporaryDirectory() as raw:
            store = ProjectStore(Path(raw))
            store.initialize(name="Browse")
            fact = store.add_fact(category="character", key="Alice", value="Navigator", state="canonical")
            session = Mock()
            session.ask = AsyncMock(side_effect=[f"fact:{fact.id}", "back"])
            session.view_text = AsyncMock()
            with patch('lg_cli.project_browser.project_entries', wraps=project_entries) as read:
                asyncio.run(browse_project(session, store, "characters"))
                read.assert_called_once()
            self.assertEqual(session.ask.await_count, 2)
            self.assertTrue(all(call.kwargs["record"] is False for call in session.ask.call_args_list))
            session.view_text.assert_awaited_once()
            session.append.assert_not_called()

    def test_timeline_distinguishes_scene_schedule_from_canonical_events(self):
        with tempfile.TemporaryDirectory() as raw:
            store = ProjectStore(Path(raw))
            store.initialize(name="Schedule")
            chapter = store.create_document(kind="chapter", slug="first", title="First")
            store.create_scene(slug="arrival", title="Arrival", chapter=chapter.id,
                               time_label="Day 1 08:00", goal="Reach the harbor")
            entries = project_entries(store, "timeline")
            self.assertEqual(len(entries), 1)
            self.assertIn("[planned]", entries[0].title)
            self.assertIn("not an inferred Canonical event", entries[0].text)
            self.assertEqual(store.list_timeline(), [])

    def test_read_only_view_scrolls_and_escape_restores_transcript(self):
        async def exercise(editor, pipe):
            editor.append("Original conversation\n")
            editor.scroll = 2
            app = asyncio.create_task(editor.app.run_async())
            viewer = None
            try:
                await asyncio.sleep(0.03)
                viewer = asyncio.create_task(editor.view_text("Characters", "\n".join(f"row {i}" for i in range(100))))
                await asyncio.sleep(0.03)
                pipe.send_text("\x1b[B")
                await asyncio.sleep(0.03)
                self.assertGreater(editor.viewer_buffer.cursor_position, 0)
                original = editor.viewer_buffer.text
                pipe.send_text("should not edit")
                await asyncio.sleep(0.03)
                self.assertEqual(editor.viewer_buffer.text, original)
                pipe.send_text("\x1b")
                await asyncio.wait_for(viewer, timeout=2)
                self.assertIsNone(editor.viewer)
                self.assertEqual(editor.transcript, "Original conversation\n")
                self.assertEqual(editor.scroll, 2)
            finally:
                if viewer and not viewer.done():
                    viewer.cancel()
                    await asyncio.gather(viewer, return_exceptions=True)
                editor.app.exit()
                await app

        with tempfile.TemporaryDirectory() as raw, create_pipe_input() as pipe:
            editor = LiteraryInput(Path(raw) / "history", input=pipe, output=DummyOutput())
            asyncio.run(exercise(editor, pipe))
