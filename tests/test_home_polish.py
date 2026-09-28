import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit.data_structures import Point, Size
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.utils import get_cwidth
from rich.text import Text

from lg_cli.book_home import PANELS, home_details, panel_lines, panel_entries, render_home, render_home_header, snapshot
from lg_cli.book_presentation import people, outline_sections
from lg_cli.config import load_config
from lg_cli.home_cache import quick_snapshot, save_snapshot, source_signature
from lg_cli.live_view import LiveView
from lg_cli.project_store import ProjectStore
from lg_cli.terminal_input import LiteraryInput
from lg_cli.terminal_view import render_conversation


class HomePolishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {'XDG_CACHE_HOME': str(self.root / 'cache'),
            'XDG_CONFIG_HOME': str(self.root / 'prefs'), 'TERM': 'xterm-256color'})
        env.start()
        self.addCleanup(env.stop)
        self.store = ProjectStore(self.root / 'book')
        self.store.initialize(name='长夜来信')

    def test_people_merge_preserves_sources_and_remaps_edges(self):
        rows = [{'id':'a', 'name':'林舟【分析】【分析】', 'role':'章节人物分析',
            'summary':'旧线索', 'evidence':{'state':'analysis','document':'第一章'}},
            {'id':'b', 'name':'林舟', 'role':'protagonist','summary':'已确认身份',
             'evidence':{'state':'canonical','document':'人物设定'}},
            {'id':'c','name':'许岚','role':'ally','summary':'档案员'}]
        characters, edges = people({'characters':rows,
            'relationships':[{'source':'b','target':'c','label':'共同调查'}]})
        self.assertEqual(len(characters), 2)
        self.assertEqual(characters[0]['name'], '林舟')
        self.assertEqual(characters[0]['role'], 'protagonist')
        self.assertEqual(len(characters[0]['records']), 2)
        self.assertEqual(edges[0]['source'], characters[0]['id'])

    def test_outline_is_readable_and_completed_events_do_not_become_plans(self):
        data = snapshot(load_config(self.store.workspace))
        data['events'] = [{'id':'one','label':'已经发生的事件','planned':False,'summary':'正文分析'}]
        data['outlines'] = [{'title':'主线大纲', 'content': json.dumps({'title':'长夜来信',
            'status':'author-approved-production-spec','premise':'一封旧信改变调查方向',
            'stages':[{'goal':'找到寄信人'}]}, ensure_ascii=False)}]
        text = '\n'.join(panel_lines(data, 'plan', full=True))
        self.assertNotIn('已经发生的事件', text)
        self.assertNotIn('author-approved', text)
        self.assertNotIn('"title"', text)
        self.assertIn('故事前提', text)
        self.assertIn('找到寄信人', text)

    def test_legacy_rule_tuples_and_character_annotations_are_readable(self):
        text = '\n'.join(body for _,body in outline_sections(json.dumps({'rules':[
            ['timeline','storm-clock','潮峰固定在下午两点。']], 'chapters':[
            {'number':1,'title':'雨夜','reveal':'发现线索','emotion':'犹疑'}]},ensure_ascii=False)))
        self.assertIn('潮峰固定',text)
        self.assertNotIn('storm-clock',text)
        self.assertNotIn('timeline',text)
        self.assertIn('揭示的信息',text)
        data = {'characters':[
            {'id':'one','name':'林舟（间接）','role':'章节人物分析','summary':'出场说明',
             'evidence':{'state':'analysis'}},
            {'id':'two','name':'lin-zhou','role':'已记录人物','summary':'林舟32岁，全书唯一第三人称限制视角。',
             'evidence':{'state':'canonical'}},
            {'id':'note','name':'专业边界意识','role':'章节人物分析','summary':'对主题的分析',
             'evidence':{'state':'analysis'}}], 'relationships':[]}
        persons,_ = people(data)
        self.assertEqual(len(persons),1)
        self.assertEqual(persons[0]['name'],'林舟')
        self.assertEqual(persons[0]['role'],'pov')
        self.assertEqual(data['character_notes'][0]['name'],'专业边界意识')

    def test_book_details_keep_review_state_and_empty_chapters_truthful(self):
        self.store.create_document(kind='chapter',slug='one',title='雨夜',sequence=1)
        data=snapshot(load_config(self.store.workspace))
        entries=panel_entries(data,'book')
        self.assertIn('审核状态',entries[0][0])
        self.assertIn('审核状态：草稿',entries[1][1])
        self.assertIn('尚未写入正文',entries[1][1])
        self.assertNotIn('保存在场景中',entries[1][1])

    def test_cache_is_book_scoped_and_live_counts_override_it(self):
        config = load_config(self.store.workspace)
        save_snapshot(config, snapshot(config))
        self.store.create_document(kind='chapter',slug='one',title='雨夜',content='正文',sequence=1)
        fast = quick_snapshot(config)
        self.assertIn('共 1 章 · 已写 1 章', '\n'.join(panel_lines(fast, 'book')))
        self.assertIn('cached_at', fast)
        other = ProjectStore(self.root / 'other')
        other.initialize(name='另一部书')
        self.assertEqual(quick_snapshot(load_config(other.workspace))['chapters'], [])

    def test_source_cache_reuses_unchanged_projection_and_invalidates_edits(self):
        config = load_config(self.store.workspace)
        self.store.create_document(kind='chapter',slug='one',title='雨夜',content='正文',sequence=1)
        first = snapshot(config)
        self.assertTrue(save_snapshot(config, first))
        with patch('lg_cli.book_home.build_index', side_effect=AssertionError('Unnecessary rebuild')):
            self.assertEqual(snapshot(config)['chapters'][0]['title'], '雨夜')
        self.store.create_document(kind='chapter',slug='two',title='来信',content='新正文',sequence=2)
        self.assertEqual(len(snapshot(config)['chapters']),2)

    def test_quick_snapshot_closes_database_before_background_validation(self):
        import gc
        config=load_config(self.store.workspace)
        before=source_signature(config.workspace)
        gc.disable()
        try:
            quick_snapshot(config)
            self.assertEqual(source_signature(config.workspace),before)
        finally:
            gc.enable()
            gc.collect()

    def test_first_paint_contains_real_panels_and_has_bounded_cell_width(self):
        from lg_cli.first_home import render_first_home
        self.store.create_document(kind='chapter',slug='one',title='雨夜',content='正文',sequence=1)
        data=quick_snapshot(load_config(self.store.workspace))
        for width,height in ((24,24),(39,24),(63,30),(99,36),(159,45)):
            plain=Text.from_ansi(render_first_home(data,width,height)).plain
            self.assertIn('书籍概况',plain)
            self.assertIn('已写 1 章',plain)
            self.assertLessEqual(len(plain.splitlines()),height-2)
            self.assertTrue(all(get_cwidth(line)<=width for line in plain.splitlines()),(width,plain))
            if width < 92:
                self.assertTrue(all(get_cwidth(line)==width for line in plain.splitlines() if line.startswith(('╭','│','╰'))))

    def test_empty_items_do_not_create_frames_and_one_request_has_one_reply(self):
        blocks = [('user','查看章节'), ('assistant',' '), ('progress','我会核对章节资料'),
            ('system','project_memory · 完成\n结果'), ('assistant','第一段说明'),
            ('system','命令 · 完成\necho test\ntest'), ('assistant','最终回复')]
        hotspots = {}
        view = Text.from_ansi(render_conversation(blocks, 64, hotspots=hotspots)).plain
        self.assertEqual(view.count('╭─ LiteraryGiant'), 1)
        self.assertIn('思考摘要', view)
        self.assertIn('执行记录', view)
        self.assertNotIn('echo test', view)
        self.assertNotIn('我会核对', view)
        for line, indices in hotspots.items():
            self.assertIn('▸', view.splitlines()[line])
            expanded = Text.from_ansi(render_conversation(blocks,64,expanded=indices)).plain
            self.assertTrue('我会核对' in expanded or 'echo test' in expanded)
        pending = Text.from_ansi(render_conversation([('assistant','')],64,loading='I am simmering')).plain
        self.assertNotIn('╭', pending)

    def test_command_confirmation_stays_visible_while_tool_output_is_folded(self):
        text = Text.from_ansi(render_conversation([('user','/rename'),
            ('system','对话名称已更新'), ('execution','project_memory · 完成\n结果')],64)).plain
        self.assertIn('对话名称已更新',text)
        self.assertIn('执行记录',text)
        self.assertNotIn('project_memory',text)
        error = Text.from_ansi(render_conversation([('system','章节读取失败\n检查权限\n具体堆栈')],64)).plain
        self.assertIn('章节读取失败',error)
        self.assertNotIn('具体堆栈',error)

    def test_shell_and_public_summaries_are_available_but_private_reasoning_is_not(self):
        with create_pipe_input() as pipe:
            session = LiteraryInput(self.root/'history',input=pipe,output=DummyOutput())
            events = LiveView(session)
            events('item/reasoning/textDelta', {'itemId':'private','delta':'PRIVATE'})
            events('item/reasoning/summaryTextDelta', {'itemId':'public','delta':'检查书籍记录'})
            events('item/completed', {'item': {'type':'commandExecution','id':'cmd',
                'command':'echo example','aggregatedOutput':'example','exitCode':0}})
            values = '\n'.join(text for _,text in session._blocks)
            self.assertNotIn('PRIVATE', values)
            self.assertIn('检查书籍记录', values)
            self.assertIn('echo example', values)

    def test_f1_navigation_details_escape_and_mouse_release(self):
        class Output(DummyOutput):
            def get_size(self):
                return Size(rows=30, columns=64)
        self.store.create_document(kind='character',slug='lin',title='林舟',content='人物详细资料')
        data = snapshot(load_config(self.store.workspace))
        async def exercise(session, pipe):
            session.home_renderer = lambda width: render_home(data,width,heading=False,
                selected=list(PANELS)[session.home_selection] if session.browse_mode else None,
                positions=session._home_positions)
            session.home_header_renderer = lambda width: render_home_header(data,width,.5)
            session.home_details_handler = lambda selected=None: home_details(session,data,
                list(PANELS)[selected] if selected is not None else None)
            session.show_home()
            task = asyncio.create_task(session.app.run_async())
            async def send(text):
                pipe.send_text(text)
                await asyncio.sleep(.08)
            try:
                await asyncio.sleep(.05)
                self.assertFalse(session.app.mouse_support())
                await send('\x1bOP')
                self.assertTrue(session.browse_mode)
                self.assertTrue(session.app.mouse_support())
                await send('\x1b[B')
                self.assertEqual(session.home_selection,1)
                await send('\r')
                self.assertEqual(session.dialog['title'], '人物')
                await send('\r')
                self.assertIn('人物详细资料',session.viewer_buffer.text)
                self.assertFalse(session.app.mouse_support())
                # Standalone Escape needs the terminal key-prefix timeout to elapse.
                await send('\x1b')
                await asyncio.sleep(.6)
                self.assertIsNone(session.viewer)
                self.assertIsNotNone(session.dialog)
                await send('\x1b')
                await asyncio.sleep(.6)
                self.assertIsNone(session.dialog)
                self.assertTrue(session.browse_mode)
                await send('\x1b')
                await asyncio.sleep(.6)
                self.assertFalse(session.browse_mode)
                self.assertFalse(session.app.mouse_support())
            finally:
                session.app.exit()
                await task
        with create_pipe_input() as pipe:
            session = LiteraryInput(self.root/'history',input=pipe,output=Output())
            asyncio.run(exercise(session,pipe))

    def test_header_uses_animated_original_and_fits_half_terminal(self):
        data = snapshot(load_config(self.store.workspace))
        for width in (24,40,64,100,160):
            first = Text.from_ansi(render_home_header(data,width,0)).plain
            second = Text.from_ansi(render_home_header(data,width,.8)).plain
            self.assertNotEqual(first,second)
            self.assertTrue(all(get_cwidth(line)<=width for line in first.splitlines()))
            self.assertIn('长夜',first)

    def test_f1_reply_folds_expand_by_keyboard_and_click(self):
        async def exercise(session,pipe):
            session.append('检查章节',role='user')
            session.append('正在核对记录',role='progress')
            session.append('命令执行\necho example\nexample',role='system')
            session.append('章节检查完成',role='assistant')
            task=asyncio.create_task(session.app.run_async())
            try:
                await asyncio.sleep(.05)
                pipe.send_text('\x1bOP\r')
                await asyncio.sleep(.08)
                self.assertIn(1,session.expanded)
                pipe.send_text('\x1b[B\r')
                await asyncio.sleep(.08)
                self.assertIn(2,session.expanded)
                session._conversation()
                row=next(row for row,indices in session._detail_hotspots.items() if 2 in indices)
                session._click_history(Point(0,row))
                self.assertNotIn(2,session.expanded)
                self.assertNotIn('echo example',Text.from_ansi(session._conversation()).plain)
            finally:
                session.app.exit()
                await task
        with create_pipe_input() as pipe:
            session=LiteraryInput(self.root/'history',input=pipe,output=DummyOutput())
            asyncio.run(exercise(session,pipe))
