import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from prompt_toolkit.data_structures import Size
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.utils import get_cwidth
from rich.text import Text

from lg_cli.book_home import header_content, panel_lines, snapshot
from lg_cli.book_overview import read_overview, save_overview
from lg_cli.config import load_config
from lg_cli.home_cache import quick_snapshot, source_signature
from lg_cli.main import build_parser, interactive_loop
from lg_cli.project_store import ProjectStore
from lg_cli.run_store import RunStore
from lg_cli.slash_commands import command_catalog
from lg_cli.terminal_input import LiteraryInput
from lg_cli.terminal_view import choice_rows, render_conversation
from lg_cli.thinking_orb import working_dots, terminal_frame


class DetailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root/'prefs'),
            'XDG_CACHE_HOME': str(self.root/'cache'), 'LITERARYGIANT_REGISTRY_HOME': str(self.root/'registry'),
            'TERM': 'xterm-256color'})
        env.start()
        self.addCleanup(env.stop)
        self.store = ProjectStore(self.root/'book')
        self.store.initialize(name='长夜来信')

    def test_fixed_header_has_centered_original_mark_and_bounded_width(self):
        data = snapshot(load_config(self.store.workspace))
        for width in (24, 40, 64, 100, 160):
            lines = header_content(data, width).plain.splitlines()
            self.assertTrue(lines[0].startswith('╭'))
            self.assertTrue(lines[-1].endswith('╯'))
            self.assertTrue(all(get_cwidth(line) == width for line in lines))
            line = next(line for line in lines if '长夜来信' in line)
            left, right = line[2:-2].split('长夜来信')
            self.assertLessEqual(abs(len(left)-len(right)), 1)

    def test_chinese_descriptions_share_right_edge(self):
        for width in (20, 40, 64, 120):
            rows = ''.join(t for _, t in choice_rows([('/x', '短'), ('/模型', '较长的中文解释')], -1, width)).splitlines()
            self.assertEqual(get_cwidth(rows[0].rstrip()), get_cwidth(rows[1].rstrip()))
            self.assertTrue(all(get_cwidth(row) == width for row in rows))
            self.assertTrue(all(row.startswith('  /') for row in rows))

    def test_overview_refreshes_both_snapshot_paths_and_stays_book_scoped(self):
        config = load_config(self.store.workspace)
        self.assertIn('尚未设置简介', panel_lines(quick_snapshot(config), 'book'))
        before = source_signature(self.store.workspace)
        save_overview(self.store.workspace, '林舟追寻旧信，揭开故乡隐瞒的往事。', source={'version': 1})
        self.assertNotEqual(before, source_signature(self.store.workspace))
        for data in (snapshot(config), quick_snapshot(config)):
            self.assertIn('林舟追寻旧信', '\n'.join(panel_lines(data, 'book')))
        save_overview(self.store.workspace, '林舟循着旧信跨越长夜，与故乡和解。', source={'version': 2})
        self.assertIn('与故乡和解', quick_snapshot(config)['overview'])
        self.assertEqual(read_overview(self.root/'other'), '')
        with self.assertRaises(ValueError):
            save_overview(self.store.workspace, {}, source={})
        self.assertIn('与故乡和解', read_overview(self.store.workspace))

    def test_outline_premise_fallback_does_not_expose_json(self):
        outlines = [{'content': json.dumps({'title':'长夜来信', 'premise':'旧信带来一场寻人之旅。'})}]
        self.assertEqual(read_overview(self.store.workspace, outlines), '旧信带来一场寻人之旅。')

    def test_first_paint_is_cached_before_background_index_and_invalidates_overview(self):
        from lg_cli import startup
        with patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True), \
             patch('os.isatty', return_value=True), patch('os.get_terminal_size', return_value=os.terminal_size((64, 30))), \
             patch('lg_cli.startup._paint') as paint, patch.object(startup, 'initial_home', None):
            args = ['-C', str(self.store.workspace)]
            startup.first_frame(args)
            self.assertIn('尚未设置简介', paint.call_args.args[0])
            with patch('lg_cli.home_cache.quick_snapshot', side_effect=AssertionError('Cache should bypass DB projection')):
                startup.first_frame(args)
            save_overview(self.store.workspace, '林舟重读旧信，寻找故乡的真相。', source={})
            startup.first_frame(args)
            self.assertIn('林舟重读旧信', paint.call_args.args[0])

    def test_browse_is_local_preserves_home_and_releases_mouse_on_escape(self):
        class Output(DummyOutput):
            def get_size(self):
                return Size(rows=30, columns=64)
        async def exercise(session, pipe):
            session._handler = AsyncMock(return_value=True)
            session.show_home()
            task = asyncio.create_task(session.app.run_async())
            try:
                await asyncio.sleep(.05)
                pipe.send_text('/browse\r')
                await asyncio.sleep(.15)
                self.assertTrue(session.home_visible)
                self.assertTrue(session.browse_mode)
                self.assertTrue(session.app.mouse_support())
                session._handler.assert_not_called()
                self.assertEqual(session._blocks, [])
                pipe.send_text('\x1b')
                await asyncio.sleep(.6)
                self.assertFalse(session.browse_mode)
                self.assertFalse(session.app.mouse_support())
            finally:
                session.app.exit()
                await task
        with create_pipe_input() as pipe:
            session = LiteraryInput(self.root/'history', input=pipe, output=Output(), commands=command_catalog(build_parser()))
            asyncio.run(exercise(session, pipe))

    def test_run_list_and_profile_route_without_an_agent_or_subprocess(self):
        session = Mock()
        session.app.is_running = False
        session.ask = AsyncMock(return_value=None)
        session.view_text = AsyncMock(return_value=None)
        session.run_command = AsyncMock(side_effect=AssertionError('Unexpected subprocess'))
        def run(handler, model, provider):
            async def exercise():
                await handler('/run list')
                await handler('/profile')
                await handler('/preferences')
                await handler('/browse')
                return 0
            return asyncio.run(exercise())
        session.run.side_effect = run
        with patch('lg_cli.main.LiteraryInput', return_value=session), \
             patch('lg_cli.live_agent.LiveAgent', side_effect=AssertionError('Unexpected model')):
            self.assertEqual(interactive_loop(load_config(self.store.workspace)), 0)
        session.view_text.assert_any_await('运行记录', '本书尚无运行记录。')
        session.enter_browse.assert_called_once()
        session.run_command.assert_not_called()

    def test_browse_can_expand_code_without_ctrl_shortcuts(self):
        blocks = [('assistant', '说明\n```python\nprint("detail")\n```')]
        hotspots = {}
        compact = render_conversation(blocks, 64, hotspots=hotspots)
        self.assertNotIn('print(', compact)
        self.assertNotIn('Ctrl+', compact)
        self.assertEqual(list(hotspots.values()), [(0,)])
        self.assertIn('print', render_conversation(blocks, 64, expanded={0}))

    def test_management_does_not_echo_as_chat_but_writing_starts_a_new_request(self):
        async def exercise(session):
            session._handler = AsyncMock(return_value=True)
            session._submit_value('/write 续写第一章')
            await session._task
            self.assertEqual(session._blocks, [('user', '/write 续写第一章')])
            session.clear()
            session._submit_value('/run list')
            await session._task
            self.assertEqual(session._blocks, [])
            session.show_home()
            session._handler = AsyncMock(side_effect=ValueError('未找到运行记录'))
            await session._dispatch('/run show missing')
            self.assertFalse(session.home_visible)
            self.assertIn('未找到运行记录', session.transcript)
        with create_pipe_input() as pipe:
            session = LiteraryInput(self.root/'history', input=pipe, output=DummyOutput())
            asyncio.run(exercise(session))

    def test_original_orb_uses_moving_geometry_without_emoticons(self):
        dots = working_dots(0)
        self.assertEqual(len(dots), 39)
        self.assertEqual(dots, sorted(dots, key=lambda d: d[2]))
        frames = [terminal_frame(t) for t in range(16)]
        self.assertGreater(len(set(frames)), 8)
        for frame in frames:
            self.assertEqual([get_cwidth(line) for line in frame.splitlines()], [4, 4])
            self.assertTrue(all(c in ' \n' or 0x2800 <= ord(c) <= 0x28ff for c in frame))

    def test_orb_geometry_matches_original_javascript_golden_vectors(self):
        fixture = json.loads((Path(__file__).parent/'fixtures/thinking-orbs-working.json').read_text())
        for frame in fixture['frames']:
            for actual, original in zip(working_dots(frame['seconds']), frame['dots'], strict=True):
                expected = [original.get(key, 1) for key in ('x', 'y', 'z', 'r', 'white', 'a')]
                for value, golden in zip(actual, expected, strict=True):
                    self.assertAlmostEqual(value, golden, places=10)

    def test_missing_overview_is_rejected_before_saving_generated_revision(self):
        from tests.helpers import make_config
        from tests.test_story_workflow import StoryAdapter
        from lg_cli.story_workflow import StoryWorkflowService
        class Adapter(StoryAdapter):
            def run(self, **kwargs):
                result = super().run(**kwargs)
                value = json.loads(result.output_text)
                value.pop('book_overview')
                from dataclasses import replace
                return replace(result, output_text=json.dumps(value))
        self.store.create_document(kind='chapter', slug='one', title='来信', content='旧正文')
        result = StoryWorkflowService(make_config(self.store.workspace), project=self.store,
            adapter=Adapter()).revise_document('one', mode='light')
        self.assertFalse(result.ok)
        self.assertEqual(len(self.store.list_versions('one')), 1)
        self.assertEqual(read_overview(self.store.workspace), '')

    def test_writing_workflow_saves_overview_in_same_model_response(self):
        from tests.helpers import make_config
        from tests.test_story_workflow import StoryAdapter
        from lg_cli.story_workflow import StoryWorkflowService
        class Adapter(StoryAdapter):
            def run(self, **kwargs):
                result = super().run(**kwargs)
                value = json.loads(result.output_text)
                value['book_overview'] = '林舟追寻旧信，在封闭的城门前寻找真相。'
                from dataclasses import replace
                return replace(result, output_text=json.dumps(value))
        self.store.create_document(kind='chapter', slug='one', title='来信', content='旧正文')
        adapter = Adapter()
        service = StoryWorkflowService(make_config(self.store.workspace), project=self.store, adapter=adapter)
        result = service.revise_document('one', mode='light')
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(adapter.calls), 1)
        self.assertIn('WHOLE book', adapter.prompts[0])
        self.assertIn('寻找真相', read_overview(self.store.workspace))
        self.assertEqual(self.store.get_document('one').content, '旧正文')

    def test_planning_stages_can_omit_overview_but_final_prose_updates_it(self):
        from tests.helpers import FakeAdapter, make_config
        from lg_cli.workflow_runner import WorkflowRunner
        from dataclasses import replace
        import re
        class Adapter(FakeAdapter):
            def run(self, **kwargs):
                result = super().run(**kwargs)
                value = json.loads(result.output_text)
                stage, total = map(int, re.search(r'## Stage\n(\d+)/(\d+)', kwargs['prompt']).groups())
                if stage < total:
                    value['book_overview'] = ''
                return replace(result, output_text=json.dumps(value))
        adapter = Adapter()
        result = WorkflowRunner(make_config(self.store.workspace), adapter=adapter).run('write', '续写第一章')
        self.assertTrue(result.ok, result.error)
        self.assertGreater(len(adapter.calls), 1)
        self.assertIn('揭开故乡的秘密', read_overview(self.store.workspace))
