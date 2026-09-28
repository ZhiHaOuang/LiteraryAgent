"""Small first-paint renderer sharing the real dashboard's content and slime.

The interactive renderer takes over after input machinery has loaded. Keeping
Console/Table/Markdown imports out of this path also makes an uncached launch fast.
"""
from __future__ import annotations

import os
import re
import unicodedata


def _width(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in text)


def _fit(text, width):
    text = ''.join(c for c in text if c.isprintable())
    if _width(text) <= width:
        return text + ' ' * (width - _width(text))
    while text and _width(text) > max(0,width - 1):
        text = text[:-1]
    text += '…' if width else ''
    return text + ' ' * max(0,width-_width(text))


def _style(style, color):
    if not color:
        return ''
    codes = ['1'] if 'bold' in style else ['2'] if 'dim' in style else []
    for index, value in enumerate(re.findall(r'#[0-9a-fA-F]{6}', style)):
        codes.append(('48' if index else '38') + ';2;' + ';'.join(str(int(value[i:i+2],16)) for i in (1,3,5)))
    return '\x1b[' + ';'.join(codes or ['0']) + 'm'


def _rich_text(text, color):
    # Text spans preserve the existing pixel animation's foreground/background halves.
    result, previous = [], None
    for index, char in enumerate(text.plain):
        style = ' '.join(str(span.style) for span in text.spans if span.start <= index < span.end)
        if style != previous:
            result.append(_style(style,color))
            previous = style
        result.append(char)
    return ''.join(result) + ('\x1b[0m' if color else '')


def render_first_home(data, width, height):
    from .book_home import header_content, panel_lines, PANELS
    color = 'NO_COLOR' not in os.environ and os.environ.get('TERM') != 'dumb'
    reset = '\x1b[0m' if color else ''
    rows = [_rich_text(line,color) for line in header_content(data,width,height=height).split('\n')]
    if data.get('refreshing'):
        stamp = data.get('cached_at','')[:16].replace('T',' ')
        caption = '上次更新 ' + stamp + ' UTC · 正在核对最新资料' if stamp else '正在核对人物、关系与历史用量…'
        rows.append(_style('dim',color) + _fit(caption,width) + reset)
    columns = 3 if width >= 150 else 2 if width >= 92 else 1
    gap = 1
    available = width - (columns - 1) * gap
    sizes = [available // columns + (i < available % columns) for i in range(columns)]
    keys = list(PANELS)
    for start in range(0,len(keys),columns):
        panels = []
        for column,key in enumerate(keys[start:start+columns]):
            size = sizes[column]
            border = _style('#e8b866' if key in {'book','personal'} else '#b96e49',color)
            title = '─ ' + PANELS[key] + ' '
            title = _fit(title,max(1,size-2)).rstrip()
            panel = [border + '╭' + title + '─' * max(0,size-2-_width(title)) + '╮' + reset]
            for i,line in enumerate('\n'.join(panel_lines(data,key)).splitlines()):
                style = 'dim' if line.startswith('  ') or line.startswith(('F1','另有','节点')) else ''
                if line.lstrip().startswith(('● ','○ ')):
                    style = 'bold #dc795f'
                if key == 'usage' and i == 0:
                    style = 'bold #e8b866'
                panel.append(border + '│ ' + reset + _style(style,color)
                    + _fit(line,max(1,size-4)) + reset + border + ' │' + reset)
            panel.append(border + '╰' + '─' * max(0,size-2) + '╯' + reset)
            panels.append(panel)
        for index in range(max(len(panel) for panel in panels)):
            rows.append(' '.join(panel[index] if index < len(panel) else ' ' * sizes[column]
                for column,panel in enumerate(panels)))
            if len(rows) >= height - 2:
                return '\r\n'.join(rows[:max(1,height-2)])
    return '\r\n'.join(rows[:max(1,height-2)])
