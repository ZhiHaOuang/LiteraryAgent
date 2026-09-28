import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from rich.text import Text
from prompt_toolkit.utils import get_cwidth
from lg_cli.outline_editor import browse_outlines
from lg_cli.project_store import ProjectStore
from lg_cli.terminal_view import render_conversation
from lg_cli.working_animation import loading_text, WORDS
from lg_cli.anthropic_bridge import translate_request, ProtocolError


class RedesignTests(unittest.TestCase):
    def test_outline_edit_keeps_old_version_and_unchanged_edit_is_noop(self):
        with tempfile.TemporaryDirectory() as raw:
            store=ProjectStore(Path(raw))
            store.initialize(name='Book')
            store.create_document(kind='outline',slug='outline',title='已有大纲')
            original=store.create_version('outline',content='Original outline',activate=True)
            doc=store.get_document('outline')
            session=Mock()
            session.ask=AsyncMock(side_effect=[doc.id,'save',None])
            session.view_text=AsyncMock(side_effect=['edit',None])
            session.edit_text=AsyncMock(return_value='Revised outline')
            asyncio.run(browse_outlines(session,store))
            self.assertEqual(store.get_document('outline').content,'Revised outline')
            self.assertEqual(store.get_version('outline',original.version_number).content,'Original outline')
            count=len(store.list_versions('outline'))
            session.ask=AsyncMock(side_effect=[doc.id,None])
            session.view_text=AsyncMock(side_effect=['edit',None])
            session.edit_text=AsyncMock(return_value='Revised outline')
            asyncio.run(browse_outlines(session,store))
            self.assertEqual(len(store.list_versions('outline')),count)

    def test_collapsed_payloads_keep_explanation_and_expand_losslessly(self):
        value='修改配置以启用章节索引。\n\n```python\nprivate_example = 123\n```\n\n| 名称 | 状态 |\n| --- | --- |\n| 索引 | 就绪 |'
        for width in [24,40,64,80]:
            compact=Text.from_ansi(render_conversation([('assistant',value)],width)).plain
            self.assertIn('修改配置',compact)
            self.assertNotIn('private_example',compact)
            self.assertIn('展开回复全文',compact)
            self.assertNotIn('Ctrl+O', compact)
            self.assertTrue(all(get_cwidth(line)<=width for line in compact.splitlines()))
        full=Text.from_ansi(render_conversation([('assistant',value)],80,expanded={0})).plain
        self.assertIn('private_example',full)
        partial=Text.from_ansi(render_conversation([('assistant','```python\nprivate_example = 123')],80)).plain
        self.assertNotIn('private_example',partial)

    def test_loading_moves_with_reply_and_does_not_claim_actual_work(self):
        frame=loading_text(0,'simmering')
        view=Text.from_ansi(render_conversation([('assistant','最新回复')],64,loading=frame)).plain
        self.assertGreater(view.index('I am simmering'),view.index('最新回复'))
        self.assertNotEqual(frame,loading_text(.125,'simmering'))
        self.assertNotIn('I am',Text.from_ansi(render_conversation([('assistant','最新回复')],64)).plain)
        self.assertGreaterEqual(len(set(WORDS)),100)

    def test_agent_messages_are_context_not_system_instructions(self):
        payload,_=translate_request({'model':'test','instructions':'trusted', 'input':[
            {'type':'agent_message','author':'reviewer','recipient':'director',
             'content':[{'type':'input_text','text':'Review findings'}]}]},100)
        self.assertEqual(payload['system'],[{'type':'text','text':'trusted'}])
        self.assertEqual(payload['messages'][0]['role'],'user')
        self.assertEqual(payload['messages'][0]['content'][-1]['text'],'Review findings')
        with self.assertRaises(ProtocolError):
            translate_request({'model':'test','input':[{'type':'agent_message','content':[{'type':'encrypted_content','encrypted_content':'opaque'}]}]},100)

    def test_file_outline_keeps_original_and_rejects_stale_edit(self):
        from lg_cli.outline_editor import outline_files, save_outline_file
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            path=root/'ReferenceLibrary/plans/outlines/outline.latest.md'
            path.parent.mkdir(parents=True)
            path.write_text('original',encoding='utf-8')
            self.assertIn(path,outline_files(root))
            save_outline_file(root,path,'original','author edit')
            self.assertEqual(path.read_text(),'author edit')
            backups=list((root/'.literarygiant/editor-history/outlines').glob('*.md'))
            self.assertEqual(len(backups),1)
            self.assertEqual(backups[0].read_text(),'original')
            with self.assertRaises(ValueError):
                save_outline_file(root,path,'original','stale edit')
            self.assertEqual(path.read_text(),'author edit')

    def test_working_input_delivers_to_steer_handler(self):
        from lg_cli.terminal_input import LiteraryInput
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        async def exercise(editor,pipe):
            received=[]
            async def steer(value):
                received.append(value)
            editor.steer_handler=steer
            editor.busy=True
            app=asyncio.create_task(editor.app.run_async())
            try:
                pipe.send_text('请停止对白审查\r')
                for _ in range(50):
                    if received: break
                    await asyncio.sleep(.01)
                self.assertEqual(received,['请停止对白审查'])
                self.assertEqual(editor.buffer.text,'')
                self.assertIn(('user','请停止对白审查'),editor._blocks)
            finally:
                editor.app.exit()
                await app
        with tempfile.TemporaryDirectory() as raw,create_pipe_input() as pipe:
            editor=LiteraryInput(Path(raw)/'history',input=pipe,output=DummyOutput(),usage_path=Path(raw)/'usage.json')
            asyncio.run(exercise(editor,pipe))
