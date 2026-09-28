import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.text import Text
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from lg_cli.book_home import snapshot, render_home, panel_lines
from lg_cli.config import load_config
from lg_cli.project_store import ProjectStore
from lg_cli.preferences import save_preferences, effective_preferences, book_path
from lg_cli.conversation_store import ConversationStore
from lg_cli.terminal_input import LiteraryInput
from lg_cli.usage_ledger import UsageLedger


class BookHomeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / 'personal')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.book = self.root / 'book'
        self.store = ProjectStore(self.book)
        self.store.initialize(name='长夜来信')

    def test_home_local_sources_and_responsive_widths(self):
        self.store.create_document(kind='chapter', slug='one', title='雨夜', content='正文', sequence=1)
        data = snapshot(load_config(self.book))
        with patch('lg_cli.provider_transport.provider_bridge', side_effect=AssertionError('No model calls')):
            for width in (24, 40, 64, 100, 160):
                plain = Text.from_ansi(render_home(data, width)).plain
                self.assertIn('雨夜', plain)
                self.assertIn('人物关系', plain)
                self.assertTrue(all(get_cwidth(row) <= width for row in plain.splitlines()), width)
        self.assertIn('尚未明确标注', '\n'.join(panel_lines(data, 'book')))
        self.assertIn('尚无实际用量记录', '\n'.join(panel_lines(data, 'usage')))

    def test_preferences_cross_book_and_local_override_reach_config(self):
        save_preferences({'name': '林', 'detail': 'concise', 'writing': '悬疑'})
        other = self.root / 'other'
        save_preferences({'writing': '温暖'}, book_path(self.book))
        self.assertEqual(effective_preferences(other)['writing'], '悬疑')
        self.assertEqual(effective_preferences(self.book)['writing'], '温暖')
        instructions = load_config(self.book).project_instructions
        self.assertIn('温暖', instructions)
        self.assertNotIn('悬疑', instructions)
        self.assertIn('concise', instructions)
        self.assertFalse((self.book / 'preferences.json').exists())

    def test_conversation_rename_preserves_history_and_hides_identifier(self):
        store = ConversationStore(self.book)
        key = store.create()
        store.append(key, 'user', '检查第三章人物动机')
        original = store.path(key).read_bytes()
        store.rename(key, '第三章 · 动机修订')
        self.assertEqual(store.path(key).read_bytes(), original)
        label = store.choices()[0][1]
        self.assertIn('第三章 · 动机修订', label)
        self.assertNotIn(key, label)
        self.assertEqual(ConversationStore(self.book).list()[0][1], '第三章 · 动机修订')

    def test_usage_is_per_book_deduplicated_and_marks_missing(self):
        ledger = UsageLedger(self.book)
        ledger.record('one', 'provider', 'model-a', {'input_tokens': 10, 'output_tokens': 5})
        ledger.record('one', 'provider', 'model-a', {'input_tokens': 10, 'output_tokens': 5})
        ledger.record('two', 'provider', 'model-b', {}, partial=True)
        data = ledger.summary()
        self.assertEqual((data['input'], data['output'], data['requests'], data['missing']), (10,5,2,1))
        self.assertEqual(UsageLedger(self.root/'other').summary()['requests'], 0)

    def test_home_scroll_and_dialogue_are_separate(self):
        with create_pipe_input() as pipe:
            session = LiteraryInput(self.root/'history', input=pipe, output=DummyOutput(), usage_path=self.root/'usage.json')
            session.append('旧对话', role='assistant')
            session.home_renderer = lambda width: '\n'.join(f'首页第{i}行' for i in range(80))
            session.show_home()
            self.assertEqual(session.home_scroll, 0)
            session.scroll_history(-10)
            self.assertEqual(session.home_scroll, 10)
            self.assertIn('首页', session._conversation())
            session.home_visible = False
            self.assertIn('旧对话', Text.from_ansi(session._conversation()).plain)

    def test_large_book_counts_and_latest_are_not_clipped_by_browser_limit(self):
        with self.store._connection() as db:
            db.executemany("INSERT INTO documents(kind,slug,title,state,sequence,created_at,updated_at) VALUES ('chapter',?,?,'draft',?,'2020','2020')",
                [(f'c{i}',f'第{i}章',i) for i in range(1,1006)])
        data = snapshot(load_config(self.book))
        self.assertEqual(len(data['chapters']),1005)
        self.assertIn('第1005章', '\n'.join(panel_lines(data,'book')))

    def test_chapter_with_scene_prose_is_counted_as_written(self):
        chapter = self.store.create_document(kind='chapter',slug='one',title='第一章')
        scene = self.store.create_scene(slug='scene',title='场景',chapter=chapter.id)
        self.store.create_version(scene.document.id,content='场景中的实际正文。',activate=True)
        data = snapshot(load_config(self.book))
        self.assertIn('共 1 章 · 已写 1 章', '\n'.join(panel_lines(data,'book')))
