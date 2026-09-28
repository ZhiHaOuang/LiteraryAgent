"""Capture the actual home screen using a fictional, local-only book."""
import argparse
import asyncio
import tempfile
from pathlib import Path

from prompt_toolkit.input.defaults import create_pipe_input
from preview_terminal import Output, capture
from lg_cli.book_home import snapshot, render_home, render_home_header, PANELS, home_details
from lg_cli.config import load_config
from lg_cli.project_store import ProjectStore
from lg_cli.terminal_input import LiteraryInput
from lg_cli.main import build_parser
from lg_cli.slash_commands import command_catalog
from lg_cli.usage_ledger import UsageLedger

GRAPH = {'characters': [{'id':'lin','name':'林舟','role':'protagonist','summary':'追查失踪者；刚收到一封来自旧码头的信。'},
    {'id':'xu','name':'许岚','role':'ally','summary':'档案员，掌握旧案的关键记录。'}],
    'events':[{'id':'letter','label':'收到来信','summary':'信封上的日期与失踪案吻合。','planned':False},
        {'id':'archive','label':'核对旧案','summary':'林舟与许岚确认线索指向码头。','planned':False},
        {'id':'dock','label':'夜访码头','summary':'第四章：追查寄信人的身份。','planned':True}],
    'relationships':[{'source':'lin','target':'xu','label':'共同调查'}],
    'links':[{'source':'letter','target':'archive','label':'促使调查'}, {'source':'archive','target':'dock','label':'下一步计划'}]}


async def preview(output, width, height):
    with tempfile.TemporaryDirectory() as raw, create_pipe_input() as pipe:
        root=Path(raw)
        store=ProjectStore(root);store.initialize(name='长夜来信')
        for index,title in enumerate(['雨夜的访客','空白档案','码头来信'],1):
            store.create_document(kind='chapter',slug=f'chapter-{index}',title=f'第{index}章 · {title}',
                sequence=index,content='虚构测试正文',state='accepted',
                metadata={'story_index':GRAPH} if index==3 else {})
        UsageLedger(root).record('sample-a','写作模型','Writer',{'input_tokens':23400,'output_tokens':8100})
        UsageLedger(root).record('sample-b','审查模型','Reviewer',{'input_tokens':9700,'output_tokens':2100})
        store.create_document(kind='outline',slug='main-outline',title='主线大纲',
            content='{"title":"长夜来信","premise":"一封旧信让林舟重新追查失踪案。","stages":[{"goal":"核对来信与旧案"},{"goal":"夜访码头，寻找寄信人"}]}')
        data=snapshot(load_config(root,environment='ui-preview'))
        data['preferences']={'name':'林间','detail':'balanced','writing':'克制叙述，悬念逐章推进。','writing_scope':'本书'}
        data['model']='Writer';data['provider']='预览模型'
        editor=LiteraryInput(root/'history',input=pipe,output=Output(width,height),
            commands=command_catalog(build_parser()),usage_path=root/'command-usage.json')
        editor.model='Writer';editor.provider='预览模型'
        editor.home_renderer=lambda width:render_home(data,width,heading=False,
            selected=list(PANELS)[editor.home_selection] if editor.browse_mode else None,
            positions=editor._home_positions)
        editor.home_header_renderer=lambda width:render_home_header(data,width,.8,height=height)
        editor.home_details_handler=lambda selected=None: home_details(editor,data,
            list(PANELS)[selected] if selected is not None else None)
        editor.show_home()
        task=asyncio.create_task(editor.app.run_async())
        try:
            await asyncio.sleep(.05);editor.app._redraw()
            capture(editor,width,height,output/f'home-{width}')
            editor.scroll_history(-1000)
            await asyncio.sleep(.05);editor.app._redraw()
            capture(editor,width,height,output/f'home-bottom-{width}')
            editor.browse_mode=True
            editor.home_selection=4
            editor._home_cache=None
            editor._focus_home_panel()
            await asyncio.sleep(.05);editor.app._redraw()
            capture(editor,width,height,output/f'home-browse-{width}')
            editor.show_home();editor.buffer.text='/'
            await asyncio.sleep(.05);editor.app._redraw()
            capture(editor,width,height,output/f'commands-{width}')
        finally:
            editor.app.exit();await task


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    for width,height in [(40,24),(64,30),(100,36),(160,45)]:
        asyncio.run(preview(args.output,width,height))
    print(args.output)
