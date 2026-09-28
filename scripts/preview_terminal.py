"""Export deterministic screenshots from the actual prompt-toolkit screen."""
import argparse
import asyncio
import io
import tempfile
import time
from pathlib import Path
from dataclasses import replace

from prompt_toolkit.data_structures import Size
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from rich.style import Style
from rich.text import Text
from rich.terminal_theme import TerminalTheme

from lg_cli.main import build_parser
from lg_cli.config import load_config
from lg_cli.dashboard import render_status_bar
from lg_cli.slash_commands import command_catalog
from lg_cli.terminal_input import LiteraryInput


class Output(DummyOutput):
    def __init__(self, width, height):
        self.size = Size(rows=height, columns=width)
    def get_size(self):
        return self.size


def capture(editor, width, height, path):
    screen = editor.app.renderer.last_rendered_screen
    rendered = Text()
    for y in range(height):
        for x in range(width):
            cell = screen.data_buffer[y][x]
            if not cell.char:
                continue
            attrs = editor.app.renderer._attrs_for_style[cell.style]
            def color(value):
                if not value or value == 'default':
                    return None
                return '#' + value if len(value) == 6 else value
            rendered.append(cell.char, Style(color=color(attrs.color), bgcolor=color(attrs.bgcolor),
                bold=attrs.bold, italic=attrs.italic, dim=attrs.dim, reverse=attrs.reverse))
        if y < height - 1:
            rendered.append('\n')
    console = Console(file=io.StringIO(), record=True, width=width, force_terminal=True)
    console.print(rendered, end='')
    colors=[(25,25,25),(190,100,70),(140,170,130),(232,184,102),(130,150,170),(165,135,175),(135,175,170),(225,221,211)]
    theme=TerminalTheme((25,25,25),(225,221,211),colors,colors)
    console.save_svg(str(path.with_suffix('.svg')),title='LiteraryGiant',theme=theme)
    path.with_suffix('.txt').write_text(rendered.plain,encoding='utf-8')


async def preview(root, width, height):
    with tempfile.TemporaryDirectory() as temporary, create_pipe_input() as pipe:
        config=replace(load_config(Path(temporary),environment='ui-preview'),workspace=Path('/books/长夜来信'),default_model='Writing model',provider='preview')
        editor=LiteraryInput(Path(temporary)/'history',input=pipe,output=Output(width,height),
            commands=command_catalog(build_parser()),usage_path=Path(temporary)/'usage.json',
            status_bar=lambda w: render_status_bar(config,w,compact=height<20,shelf_root=Path('/books')))
        editor.model='Writing model'
        editor.provider='LiteraryGiant'
        task=asyncio.create_task(editor.app.run_async())
        try:
            editor.append('帮我检查第三章与前文是否矛盾，再看看对白是否自然。',role='user')
            editor.append('我会先核对人物知道的信息和时间线，同时请另一位审查 Agent 检查对白。\n\n有冲突的地方会先与你讨论。',role='assistant')
            editor.tasks={'continuity':{'name':'连续性审查','status':'completed','step':'已核对人物信息与时间线','details':[]},
                'dialogue':{'name':'对白审查','status':'running','step':'正在检查第三章的两场对话','details':[]}}
            editor.busy=True
            editor.started=time.monotonic()
            editor._loading_word='simmering'
            await asyncio.sleep(.05)
            editor.app._redraw()
            capture(editor,width,height,root/f'conversation-{width}')
            editor.busy=False
            editor.clear()
            editor.buffer.text='/'
            await asyncio.sleep(.05)
            editor.app._redraw()
            capture(editor,width,height,root/f'commands-{width}')
            editor.buffer.text=''
            editor.append('已检查章节索引，以下改动将修复顺序。\n\n```python\nchapter_order = [1, 2, 3]\n```\n\n| 章节 | 结果 |\n| --- | --- |\n| 第三章 | 通过 |',role='assistant')
            await asyncio.sleep(.05)
            editor.app._redraw()
            capture(editor,width,height,root/f'details-{width}')
        finally:
            editor.app.exit()
            await task


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    for width,height in [(64,30),(80,36),(40,24)]:
        asyncio.run(preview(args.output,width,height))
    print(args.output)
