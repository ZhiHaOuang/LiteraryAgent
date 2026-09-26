import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from lg_cli.chapter_sync import ChapterSync
from lg_cli.novel_editor import browse_novel, read_chapter, save_part
from lg_cli.project_store import ProjectStore, ProjectStoreError
from lg_cli.terminal_input import LiteraryInput


class NovelEditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ProjectStore(self.root)
        self.store.initialize(name='Book')
        self.store.create_document(kind='chapter', slug='one', title='First chapter')
        self.store.create_scene(slug='scene', title='Opening scene', chapter='one')
        self.store.create_version('scene', content='Original prose', state='accepted', activate=True)

    def test_save_versions_main_text_and_syncs_chapter(self):
        draft = read_chapter(self.store, 'one')
        scene = self.store.get_document('scene')
        save_part(self.store, draft, scene.id, 'Author revision')
        changed = self.store.get_document('scene')
        self.assertEqual(changed.content, 'Author revision')
        self.assertEqual(changed.state, 'accepted')
        self.assertEqual(self.store.get_version('scene', scene.active_version_number).content, 'Original prose')
        self.assertEqual(self.store.get_version('scene', changed.active_version_number).metadata['origin'], 'novel-editor')
        self.assertEqual(ChapterSync(self.store).status('one').status, 'clean')
        self.assertFalse((self.store.root / 'conversations').exists())

    def test_entire_chapter_conflict_is_checked_and_other_books_rejected(self):
        draft = read_chapter(self.store, 'one')
        self.store.create_version('one', content='Concurrent intro', activate=True)
        with self.assertRaises(ProjectStoreError):
            save_part(self.store, draft, self.store.get_document('scene').id, 'Do not overwrite')
        self.assertEqual(self.store.get_document('scene').content, 'Original prose')
        other = ProjectStore(self.root / 'other')
        other.initialize()
        with self.assertRaises(ProjectStoreError):
            save_part(other, draft, draft.documents[0].id, 'Wrong book')

    def test_external_chapter_edits_are_preserved_after_database_save(self):
        sync = ChapterSync(self.store)
        path = sync.export('one').path
        path.write_text(path.read_text().replace('Original prose', 'External author edit'))
        message = save_part(self.store, read_chapter(self.store, 'one'), self.store.get_document('scene').id, 'TUI edit')
        self.assertIn('External author edit', path.read_text())
        self.assertEqual(self.store.get_document('scene').content, 'TUI edit')
        self.assertIn('未同步', message)

    def session(self, texts, choices):
        session = Mock()
        session.view_text = AsyncMock(side_effect=['edit', None, None])
        session.edit_text = AsyncMock(side_effect=texts)
        session.ask = AsyncMock(side_effect=choices)
        return session

    def test_return_prompts_and_save_is_explicit(self):
        session = self.session(['New prose'], ['save'])
        asyncio.run(browse_novel(session, self.store, 'one'))
        self.assertEqual(self.store.get_document('scene').content, 'New prose')
        self.assertIn('保存章节修改', session.ask.call_args.kwargs['title'])
        self.assertFalse(session.ask.call_args.kwargs['record'])
        session.run_command.assert_not_called()

    def test_multiple_scenes_are_edited_without_flattening_chapter_structure(self):
        self.store.create_scene(slug='second', title='Second scene', chapter='one')
        self.store.create_version('second', content='Second prose', activate=True)
        target = self.store.get_document('second')
        session = self.session(['Revised second'], [target.id, 'save'])
        asyncio.run(browse_novel(session, self.store, 'one'))
        self.assertEqual(self.store.get_document('scene').content, 'Original prose')
        self.assertEqual(self.store.get_document('second').content, 'Revised second')
        self.assertEqual(len(self.store.list_scenes(chapter='one')), 2)

    def test_cancel_confirmation_continues_editing_then_discard_keeps_version(self):
        original = self.store.get_document('scene').active_version_id
        session = self.session(['First edit', 'Second edit'], [None, 'discard'])
        asyncio.run(browse_novel(session, self.store, 'one'))
        self.assertEqual(session.edit_text.await_count, 2)
        self.assertEqual(session.edit_text.call_args.args[1], 'First edit')
        self.assertEqual(self.store.get_document('scene').active_version_id, original)

    def test_unchanged_return_does_not_prompt_or_create_version(self):
        original = self.store.get_document('scene').active_version_id
        session = self.session(['Original prose'], [])
        asyncio.run(browse_novel(session, self.store, 'one'))
        session.ask.assert_not_called()
        self.assertEqual(self.store.get_document('scene').active_version_id, original)

    def test_conflict_can_keep_edit_as_classified_draft(self):
        async def edit(*args):
            self.store.create_version('scene', content='Concurrent author text', activate=True)
            return 'My unsaved edit'
        session = self.session([], ['save', 'draft'])
        session.edit_text.side_effect = edit
        asyncio.run(browse_novel(session, self.store, 'one'))
        self.assertEqual(self.store.get_document('scene').content, 'Concurrent author text')
        files = list((self.root / 'ReferenceLibrary/drafts/candidates').glob('candidate-*.md'))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_text(), 'My unsaved edit\n')

    def test_terminal_edit_supports_typing_newlines_and_returns_draft_on_escape(self):
        async def exercise(editor, pipe):
            app = asyncio.create_task(editor.app.run_async())
            pending = None
            try:
                await asyncio.sleep(0.03)
                pending = asyncio.create_task(editor.edit_text('Edit', 'Original'))
                await asyncio.sleep(0.03)
                pipe.send_text('hello\rworld')
                await asyncio.sleep(0.05)
                pipe.send_text('\x1b')
                self.assertEqual(await asyncio.wait_for(pending, 2), 'hello\nworldOriginal')
                self.assertFalse(editor.viewer_editable)
            finally:
                if pending and not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                editor.app.exit()
                await app
        with create_pipe_input() as pipe:
            editor = LiteraryInput(self.root / 'history', input=pipe, output=DummyOutput())
            asyncio.run(exercise(editor, pipe))
