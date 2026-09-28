"""Responsive book dashboard rendered entirely from local records."""
from __future__ import annotations

import io
import json
import os

from .preferences import effective_preferences, DETAIL_LEVELS
from .project_store import ProjectStore
from .story_index import build_index
from .usage_ledger import UsageLedger
from .book_presentation import people, clean_label, outline_sections, source_detail, outline_chapters, chapter_label

PANELS = {'book': '书籍概况', 'characters': '人物', 'relationships': '人物关系',
    'progress': '目前剧情进展', 'plan': '完整规划', 'personal': '个人与偏好', 'usage': '本书模型用量'}
ROLES = {'protagonist': '主角', 'antagonist': '对手', 'ally': '同伴', 'supporting': '配角', 'pov':'视角人物'}


def header_content(data, width, seconds=0, *, height=30):
    from rich.text import Text
    from .slime_animation import slime_mark, compact_slime_mark
    width = max(12, width)
    inner = width - 4
    mark = compact_slime_mark(seconds) if inner < 30 or height < 26 else slime_mark(seconds)
    rows = [Text('欢迎回家', style='bold #e8b866'), *mark.split('\n'),
        Text(data['name'], style='bold #dc795f'),
        Text(data['preferences']['name'] + ' · ' + data['model'], style='dim')]
    content = Text(no_wrap=True)
    title = ' LiteraryGiant '
    title = title if len(title) < width - 2 else ''
    content.append('╭' + title + '─' * (width - 2 - len(title)) + '╮', style='#e8b866')
    for row in rows:
        row = row.copy()
        row.truncate(inner, overflow='ellipsis')
        padding = inner - row.cell_len
        content.append('\n│ ', style='#e8b866')
        content.append(' ' * (padding // 2))
        content.append_text(row)
        content.append(' ' * (padding - padding // 2))
        content.append(' │', style='#e8b866')
    content.append('\n╰' + '─' * (width - 2) + '╯', style='#e8b866')
    return content


def render_home_header(data, width, seconds=0, *, height=30):
    from rich.console import Console
    stream = io.StringIO()
    color = 'NO_COLOR' not in os.environ and os.environ.get('TERM') != 'dumb'
    Console(file=stream, width=max(12, width), force_terminal=color,
        color_system='truecolor' if color else None).print(header_content(data,width,seconds,height=height))
    return stream.getvalue().rstrip('\n')


def snapshot(config) -> dict:
    from .home_cache import read_snapshot, source_signature
    store = ProjectStore(config.workspace)
    if store.initialized:
        before = source_signature(config.workspace)
        cached = read_snapshot(config)
        data = cached if cached and cached.get('source_signature') == before else build_index(store)
        data['source_signature'] = before
        for key in ('refreshing', 'refresh_error', 'usage_pending'):
            data.pop(key, None)
    else:
        data = {'name': '尚未选择书籍', 'chapters': [], 'characters': [], 'events': [],
            'relationships': [], 'links': [], 'outlines': [], 'missing': 0}
    if not all('records' in person for person in data['characters']):
        data['characters'], data['relationships'] = people(data)
    from .book_overview import read_overview
    return dict(data, overview=read_overview(config.workspace, data.get('outlines', [])), preferences=effective_preferences(config.workspace),
        usage=UsageLedger(config.workspace).summary(), model=config.model_label, provider=config.provider)


def graph_lines(nodes, edges, *, limit=None):
    names = {n['id']: n.get('name', n.get('label', '')) for n in nodes}
    lines = []
    visible = nodes if limit is None else nodes[:limit]
    ids = {n['id'] for n in visible}
    for node in visible:
        lines.append('● ' + clean_label(names[node['id']]))
        outgoing = [e for e in edges if e['source'] == node['id']]
        for index, edge in enumerate(outgoing):
            target = names.get(edge['target'], edge['target'])
            lines.append(('  └─ ' if index == len(outgoing) - 1 else '  ├─ ') + edge['label'] + ' → ' + target)
        if not outgoing:
            summary = ' '.join(node.get('summary', '').split()) if limit is not None else node.get('summary', '')
            lines.append('  ' + (summary if limit is None else summary[:90]))
        if limit is None and node.get('evidence'):
            lines.append(source_detail(node))
    if limit is not None and len(nodes) > len(visible):
        lines.append(f'另有 {len(nodes) - len(visible)} 项')
    if nodes and not edges:
        lines.append('节点之间的明确关系尚未记录。')
    return lines or ['尚未记录']


def panel_lines(data, key, *, full=False):
    limit = None if full else 4
    if key == 'book':
        chapters = data['chapters']
        written = [d for d in chapters if d.get('has_prose', bool(d['content'].strip()))]
        latest = max(chapters, key=lambda d: (d['sequence'] if d['sequence'] is not None else -1, d['id']), default=None)
        protagonists = [c['name'] for c in data['characters'] if c['role'] in {'主角','protagonist'}]
        viewpoints = [c['name'] for c in data['characters'] if c['role'] == 'pov']
        lines = [data['name'], data.get('overview') or '尚未设置简介', f"共 {len(chapters)} 章 · 已写 {len(written)} 章",
            '最新章节：' + (chapter_label(latest) if latest else '尚未记录'),
            ('主角：' + '、'.join(protagonists)) if protagonists else
            ('视角人物：' + '、'.join(viewpoints)) if viewpoints else '主角：尚未明确标注']
        if full:
            lines += [f"已采用或定稿：{sum(d['state'] in {'accepted','final'} for d in chapters)} 章",
                f"尚未建立最新关系索引的资料：{data['missing']} 条", '已写表示章节或其场景已有正文。']
        return lines
    if key == 'characters':
        characters = data['characters'] if full else data['characters'][:4]
        lines = []
        for c in characters:
            role = ROLES.get(c.get('role'), c.get('role') if c.get('role') == '主角' else '')
            lines.append('● ' + c['name'] + (' · ' + role if role else '') + '\n  '
                + (source_detail(c) if full else ' '.join(c.get('summary', '').split())[:90]))
        if not full and len(data['characters']) > 4:
            lines.append(f"另有 {len(data['characters']) - 4} 项人物资料")
        return lines or ['尚未记录']
    if key == 'relationships':
        return graph_lines(data['characters'], data['relationships'], limit=limit)
    if key in {'progress', 'plan'}:
        nodes = [e for e in data['events'] if e.get('evidence', {}).get('kind') == 'outline'] if key == 'plan' else [e for e in data['events'] if not e['planned']]
        if not full and key == 'progress':
            nodes = nodes[-4:]
        ids = {n['id'] for n in nodes}
        edges = [e for e in data['links'] if e['source'] in ids and e['target'] in ids]
        lines = graph_lines(nodes, edges, limit=limit)
        if key == 'plan':
            outlines = data['outlines'] if full else data['outlines'][:2]
            if outlines and not nodes:
                lines = []
            for document in outlines:
                sections = outline_sections(document['content'])
                lines.append('● ' + clean_label(document['title']))
                chosen = sections if full else [section for section in sections if section[0] in {'故事前提','故事梗概'}][:1]
                if not chosen and not full:
                    chosen = sections[:2]
                for label, body in chosen:
                    body = body if full else body[:100]
                    lines.append('  ' + label + '：' + body.replace('\n', '\n    '))
                if not full:
                    planned = outline_chapters(document['content'])
                    for i, chapter in enumerate(planned[:3]):
                        lines.append(('  │\n' if i else '') + '  ○ ' + chapter_label(chapter))
                        if isinstance(chapter.get('goal'), str):
                            lines.append('    ' + chapter['goal'][:75])
                    if len(planned) > 3:
                        lines.append(f'  另有 {len(planned) - 3} 章规划')
            if not outlines and not nodes:
                lines = ['尚未记录大纲']
        return lines
    if key == 'personal':
        prefs = data['preferences']
        return [prefs['name'], '默认模型：' + data['model'], '提供方：' + data['provider'],
            '回复：' + DETAIL_LEVELS.get(prefs['detail'], '适中'), '写作偏好 · ' + prefs['writing_scope'],
            prefs['writing'] if full else (prefs['writing'][:160] or '尚未设置'), '/profile 修改']
    usage = data['usage']
    if data.get('usage_pending'):
        return ['正在汇总本书用量…', '历史记录读取完成后自动更新']
    lines = [f"累计 {usage['input'] + usage['output']:,} tokens",
        f"输入 {usage['input']:,} · 输出 {usage['output']:,}"] if usage['requests'] else ['尚无实际用量记录']
    rows = usage['models'] if full else usage['models'][:3]
    for row in rows:
        model = row['provider'] + ' · 历史模型未记录' if row['model'] == '历史模型（名称未记录）' else row['model']
        lines.append(f"● {model} · {row['input'] + row['output']:,}\n  输入 {row['input']:,} · 输出 {row['output']:,}" + (f"\n  提供方：{row['provider']}" if full else ''))
    if full:
        lines += ['统计起点：' + (usage['since'][:16].replace('T', ' ') + ' UTC' if usage['since'] else '尚未开始'),
        f"覆盖 {usage['requests']} 次请求 · {usage['missing']} 次用量缺失 · {usage['partial']} 次不完整",
        f"含 {usage.get('historical', 0)} 条可核实历史记录；其余历史不计入。",
            '新统计包含主 Agent 与子 Agent。']
    else:
        lines.append('可展开查看全部模型与统计说明')
    return lines


def render_home(data, width, *, selected=None, positions=None, heading=True):
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    width = max(12, width)
    stream = io.StringIO()
    color = 'NO_COLOR' not in os.environ and os.environ.get('TERM') != 'dumb'
    console = Console(file=stream, width=width, force_terminal=color,
        color_system='truecolor' if color else None, highlight=False)
    if heading:
        console.print(Text('LiteraryGiant  /  ' + data['preferences']['name'], style='bold #e8b866' if color else 'bold'))
        console.print(Text(data['name'] + ' · ' + data['model'], style='#dc795f' if color else ''))
    if heading:
        console.print(Text('方向键选择 · Enter 查看 · Esc 返回' if selected else '输入开始交流 · /browse 浏览', style='dim'))
    if data.get('refreshing'):
        stamp = data.get('cached_at', '')[:16].replace('T', ' ')
        console.print(Text(('上次更新 ' + stamp + ' UTC · 正在核对最新资料') if stamp else '正在核对人物、关系与历史用量…', style='dim'))
    if data.get('refresh_error'):
        console.print(Text('资料更新未完成 · 再次 /home 重试', style='#dc795f'))
    columns = 3 if width >= 150 else 2 if width >= 92 else 1
    keys = list(PANELS)
    for start in range(0, len(keys), columns):
        table = Table.grid(expand=True, padding=(0, 1))
        for _ in range(columns):
            table.add_column(ratio=1)
        panels = []
        for key in keys[start:start + columns]:
            lines = panel_lines(data, key)
            # Overview remains bounded; complete source text is available in details.
            overview = Text()
            for i, line in enumerate('\n'.join(line[:320] for line in lines).splitlines()):
                if i:
                    overview.append('\n')
                style = 'dim' if line.startswith('  ') or line.startswith(('F1', '另有', '节点')) else ''
                if line.lstrip().startswith(('● ', '○ ')):
                    style = 'bold #dc795f' if color else 'bold'
                if key == 'usage' and i == 0:
                    style = 'bold #e8b866' if color else 'bold'
                overview.append(line, style=style)
            if positions is not None:
                positions[key] = stream.getvalue().count('\n')
            panels.append(Panel(overview, title=('▸ ' if selected == key else '') + PANELS[key], title_align='left',
                border_style='bold #e8b866' if selected == key else '#e8b866' if key in {'book','personal'} else '#b96e49',
                box=box.ROUNDED, padding=(0, 1)))
        table.add_row(*panels, *[''] * (columns - len(panels)))
        console.print(table)
    return stream.getvalue().rstrip('\n')


def panel_entries(data, key):
    if key == 'relationships':
        names = {c['id']:c['name'] for c in data['characters']}
        entries = []
        for character in data['characters']:
            edges = [e for e in data['relationships'] if character['id'] in {e['source'],e['target']}]
            lines = [names[e['source']] + ' ─ ' + e['label'] + ' → ' + names[e['target']] for e in edges]
            entries.append((character['name'], '\n'.join(lines or ['尚未记录明确关系'])
                + '\n\n' + source_detail(character)))
        return entries
    if key == 'characters':
        return [(c['name'], source_detail(c)) for c in data['characters']] + [
            ('人物分析补充 · ' + clean_label(c['name']), source_detail(c)) for c in data.get('character_notes', [])]
    if key == 'book':
        states = {'draft':'草稿','candidate':'候选稿','accepted':'已采用','final':'已定稿',
            'archived':'已归档','rejected':'未采用'}
        entries = [('章节统计与审核状态', '\n'.join(panel_lines(data,'book',full=True)))]
        for chapter in data['chapters']:
            state = states.get(chapter['state'],'尚未确认')
            body = chapter['content'] or ('本章正文保存在场景中。' if chapter.get('has_prose') else '尚未写入正文。')
            entries.append((chapter_label(chapter), '审核状态：' + state + '\n\n' + body))
        return entries
    if key == 'progress':
        return [(clean_label(e['label']), source_detail(e)) for e in data['events'] if not e['planned']]
    if key == 'plan':
        entries = []
        for document in data['outlines']:
            entries.append((clean_label(document['title']), '\n\n'.join('## ' + label + '\n' + body
                for label, body in outline_sections(document['content']))))
            for chapter in outline_chapters(document['content']):
                entries.append((chapter_label(chapter), '\n\n'.join('## ' + label + '\n' + body
                    for label, body in outline_sections(json.dumps(chapter,ensure_ascii=False),chapter=True))))
        return entries
    if key == 'usage':
        entries = [(r['model'], f"# {r['model']}\n\n提供方：{r['provider']}\n累计 {r['input'] + r['output']:,} tokens\n输入 {r['input']:,}\n输出 {r['output']:,}") for r in data['usage']['models']]
        return entries + [('统计说明', '\n'.join(panel_lines(data, key, full=True)))]
    return [('个人与写作偏好', '\n'.join(panel_lines(data, key, full=True)))]


async def home_details(session, data, selected=None):
    while True:
        try:
            key = selected or await session.ask(kind='choice', title='工作台面板', text='查看完整资料与来源',
                values=list(PANELS.items()), record=False)
        finally:
            session.close_dialog()
        if key is None:
            return
        entries = panel_entries(data, key)
        if not entries:
            await session.view_text(PANELS[key], '\n'.join(panel_lines(data, key, full=True)))
        else:
            while True:
                try:
                    choice = await session.ask(kind='choice', title=PANELS[key],
                        text='Enter 查看详情 · Esc 返回面板',
                        values=[(str(i), title) for i, (title, _) in enumerate(entries)], record=False)
                finally:
                    session.close_dialog()
                if choice is None:
                    break
                title, body = entries[int(choice)]
                await session.view_text(title, body)
        if selected:
            return
