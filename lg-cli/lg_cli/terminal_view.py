"""Cell-aware presentation for the interactive terminal, independent of workflows."""

import io
import os
import re

from prompt_toolkit.utils import get_cwidth
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.align import Align
from rich import box
from rich.text import Text


def clip(text, width):
    text = " ".join(text.split())
    if get_cwidth(text) <= width:
        return text
    while text and get_cwidth(text) > max(0, width - 1):
        text = text[:-1]
    return text + ("…" if width else "")


def command_columns(name, description, width, *, name_limit=None):
    width = max(0, width)
    name = clip(name, min(width, name_limit if name_limit is not None else max(1, width // 2)))
    description = clip(description, max(0, width - get_cwidth(name) - 2))
    return name + ' ' * (width - get_cwidth(name) - get_cwidth(description)) + description


def choice_rows(labels, selected, width):
    width = max(4, width)
    result = []
    available = width - 4
    name_width = min(max((get_cwidth(label[0]) for label in labels if isinstance(label, tuple)), default=0),
        max(1, available // 2))
    for index, label in enumerate(labels):
        if index:
            result.append(("", "\n"))
        available = width - 4
        if isinstance(label, tuple):
            name, description = label
            label = command_columns(name, description, available, name_limit=name_width)
        else:
            label = clip(label, available)
        body = "  " + label + " " * (width - 2 - get_cwidth(label))
        if index == selected:
            if os.environ.get("TERM") != "dumb":
                result.append(("class:command.edge", " ╭" + "─" * (width - 4) + "╮ \n"))
            if os.environ.get("TERM") != "dumb":
                result.extend(
                    [
                        ("class:command.side", " │"),
                        ("class:command.selected", body[2:-2]),
                        ("class:command.side", "│ "),
                    ]
                )
            else:
                result.append(("class:command.selected", body))
            if os.environ.get("TERM") != "dumb":
                result.append(("class:command.edge", "\n ╰" + "─" * (width - 4) + "╯ "))
        else:
            result.append(("", body))
    return result


def compact_content(value):
    """Hide bulky payloads while retaining surrounding explanatory prose."""
    hidden = False
    def code(match):
        nonlocal hidden
        hidden = True
        language = match.group(1).strip() or '文本'
        count = len(match.group(2).splitlines())
        return f'\n▸ {language} 代码 · {count} 行\n'
    text = re.sub(r'```([^\n]*)\n(.*?)(?:```|\Z)', code, value, flags=re.S)
    lines = text.splitlines()
    result = []
    index = 0
    while index < len(lines):
        if index + 1 < len(lines) and '|' in lines[index] and re.match(r'^\s*\|?\s*:?-{3,}', lines[index + 1]):
            start = index
            index += 2
            while index < len(lines) and '|' in lines[index]:
                index += 1
            result.append(f'▸ 表格 · {max(0, index - start - 2)} 行数据')
            hidden = True
        else:
            result.append(lines[index])
            index += 1
    return '\n'.join(result), hidden


def _render_bubbles(blocks, width, *, expanded=(), tasks=(), loading='', offset=0):
    stream = io.StringIO()
    color = 'NO_COLOR' not in os.environ and os.environ.get('TERM') != 'dumb'
    console = Console(file=stream, width=max(4, width), force_terminal=color,
        color_system='truecolor' if color else None, highlight=False)
    last_user = max((i for i, (role, _) in enumerate(blocks) if role == 'user'), default=-1)
    current_reply = max((i for i, (role, _) in enumerate(blocks)
        if role in {'assistant', 'progress'} and i > last_user), default=-1)
    if current_reply >= 0:
        current_reply += offset
    for index, (role, value) in enumerate(blocks, start=offset):
        if not value.strip():
            continue
        if role in {'user', 'assistant', 'progress'}:
            content, hidden = compact_content(value) if role != 'user' and index not in expanded else (value, False)
            label = '你' if role == 'user' else 'LiteraryGiant' if role == 'assistant' else '进展'
            border = '#e8b866' if role == 'user' else '#b96e49'
            # A narrow gutter carries alignment without sacrificing a half-screen's text width.
            available = max(4, width - (2 if width < 60 else 6))
            if role == 'user':
                longest = max((get_cwidth(line) for line in value.splitlines()), default=0)
                available = min(available, max(12, longest + 4))
            renderable = Text(content) if role == 'user' else Markdown(content)
            if loading and index == current_reply:
                rows = console.render_lines(renderable,
                    console.options.update(width=max(1, available - 4)), pad=False)
                texts = [Text.assemble(*[(segment.text, segment.style) for segment in row if not segment.control]) for row in rows]
                ornaments = [Text(line, style='#dc795f' if color else '') for line in loading.splitlines()]
                if texts and get_cwidth(texts[-1].plain) + 2 + max(map(lambda t: t.cell_len, ornaments)) <= available - 4:
                    indent = texts[-1].cell_len + 2
                    texts[-1].append('  ')
                    texts[-1].append_text(ornaments[0])
                    texts.extend(Text(' ' * indent) + line for line in ornaments[1:])
                else:
                    texts.extend(ornaments)
                renderable = Group(*texts)
            panel = Panel(renderable, title=label, title_align='right' if role == 'user' else 'left',
                width=available, border_style=border if color else '', box=box.ROUNDED, padding=(0, 1))
            console.print(Align.right(panel) if role == 'user' else panel)
            console.print()
        else:
            lines = value.strip().splitlines()
            if index in expanded or len(lines) <= 2:
                console.print(Text(value.rstrip(), style='dim'))
            else:
                summary = clip(lines[0], max(4, width - 4))
                console.print(Text(f'▸ {summary}\n  {len(lines)} 行执行记录 · 详情 #{index + 1}', style='dim'))
    if loading and current_reply < 0:
        console.print(Text(loading, style='#dc795f' if color else ''))
    if tasks:
        console.print(Text('协作任务', style='#b96e49' if color else ''))
        for task in tasks:
            symbol = {'completed': '✓', 'failed': '!', 'running': '◌', 'cancelled': '–'}.get(task['status'], '·')
            console.print(Text(clip(f"  {symbol} {task['name']} · {task['step']}", width), style='dim'))
        console.print(Text('  协作任务可展开查看', style='dim'))
    return stream.getvalue().rstrip('\n')


def render_conversation(blocks, width, *, expanded=(), tasks=(), loading='', offset=0, hotspots=None, selection=None):
    """One reply per user request, with public summaries and execution folded below."""
    output = []
    groups, current = [], []
    for index, (role, value) in enumerate(blocks, offset):
        if role == 'user':
            if current:
                groups.append(current)
            current = []
            groups.append([(index, role, value)])
        else:
            current.append((index, role, value))
    if current:
        groups.append(current)
    for number, group in enumerate(groups):
        if group[0][1] == 'user':
            output.append(_render_bubbles([('user', group[0][2])], width))
            continue
        prose = [(i, text) for i, role, text in group if role == 'assistant' and text.strip()]
        ornament = loading if number == len(groups) - 1 else ''
        if prose:
            text = '\n\n'.join(text.strip() for _, text in prose)
            first = prose[0][0]
            output.append(_render_bubbles([('assistant', text)], width, offset=first,
                expanded=[first] if any(i in expanded for i, _ in prose) else (), loading=ornament))
            if compact_content(text)[1]:
                if hotspots is not None:
                    hotspots[sum(part.count('\n') + 2 for part in output)] = tuple(i for i, _ in prose)
                output.append(('› ' if selection == len(hotspots or {}) - 1 else '') +
                    ('▾ 收起回复全文' if any(i in expanded for i, _ in prose) else '▸ 展开回复全文'))
        elif ornament:
            output.append(_render_bubbles([], width, loading=ornament))
        for i, role, text in group:
            if role == 'system' and text.strip():
                lines = text.strip().splitlines()
                output.append(_render_bubbles([('system',text if len(lines) <= 2 else lines[0])],width))
        for role, label in [('progress', '思考摘要'), ('system', '执行记录')]:
            entries = [(i, text) for i, r, text in group if text.strip() and
                (r == 'progress' if role == 'progress' else r == 'execution' or
                 r == 'system' and len(text.strip().splitlines()) > 2)]
            if not entries:
                continue
            opened = any(i in expanded for i, _ in entries)
            row = ('▾ ' if opened else '▸ ') + label + f' · {len(entries)} 项'
            focused = hotspots is not None and selection == len(hotspots)
            if focused:
                row = '› ' + row
            if hotspots is not None:
                hotspots[sum(part.count('\n') + 2 for part in output)] = tuple(i for i, _ in entries)
            row = clip(row, width)
            color = 'NO_COLOR' not in os.environ and os.environ.get('TERM') != 'dumb'
            style = '\x1b[38;2;232;184;102m' if focused else '\x1b[38;2;185;110;73m' if role == 'progress' else '\x1b[90m'
            output.append(style + row + '\x1b[0m' if color else row)
            if opened:
                output.append(_render_bubbles([(role, text) for _, text in entries], width,
                    expanded=range(len(entries))))
    if loading and (not groups or groups[-1][0][1] == 'user'):
        output.append(_render_bubbles([], width, loading=loading))
    if tasks:
        output.append('协作任务')
        for index, task in enumerate(tasks):
            focused = hotspots is not None and selection == len(hotspots)
            if hotspots is not None:
                hotspots[sum(part.count('\n') + 2 for part in output)] = (f'task:{index}',)
            symbol = {'completed': '✓', 'failed': '!', 'running': '◌', 'cancelled': '–'}.get(task['status'], '·')
            row = clip(('› ' if focused else '▸ ') + symbol + ' ' + task['name'] + ' · ' + task['step'], width)
            color = 'NO_COLOR' not in os.environ and os.environ.get('TERM') != 'dumb'
            output.append(('\x1b[38;2;232;184;102m' if focused else '\x1b[90m') + row + '\x1b[0m' if color else row)
    return '\n\n'.join(part for part in output if part)
