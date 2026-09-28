"""Readable local book records; presentation never invents story facts."""
from __future__ import annotations

import json
import re


def clean_label(value):
    text = re.sub(r'^(?:\s*[\[【](?:分析|章节分析)[\]】])+\s*', '', str(value))
    return re.sub(r'(?:\s*[\[【](?:分析|章节分析)[\]】])+\s*$', '', text).strip()


def people(data):
    """Merge exact names, retaining every source for the detail view."""
    merged, aliases = {}, {}
    priority = {'canonical': 4, 'accepted': 3, 'final': 3, 'draft': 2, 'analysis': 1}
    candidates = {re.split(r'[（(]', clean_label(c['name']), maxsplit=1)[0]
        for c in data.get('characters', [])}
    notes = []
    for original in data.get('characters', []):
        source = dict(original)
        name = clean_label(source['name'])
        base = re.split(r'[（(]', name, maxsplit=1)[0]
        if re.search(r'[（(].*(?:间接|出场|队员|负责人|组长|联络|居民)', name):
            name = base
        if re.fullmatch(r'[a-z][a-z_-]+', name) and source.get('evidence', {}).get('state') == 'canonical':
            summary = source.get('summary', '')
            matches = [candidate for candidate in candidates if candidate and summary.startswith(candidate)
                and re.search(r'[\u4e00-\u9fff]', candidate)]
            if matches:
                name = max(matches, key=len)
            elif match := re.match(r'^([\u4e00-\u9fff]{2,8})(?=\d+岁|[：:])', summary):
                name = match[1]
            if re.search(r'全书唯一[^。；\n]{0,20}视角', summary) and source.get('role') not in {'protagonist','主角'}:
                source['role'] = 'pov'
        if source.get('evidence', {}).get('state') == 'analysis' and re.search(r'(?:流程|意识|机制|主题|原则)$', name):
            notes.append(source)
            continue
        if source.get('evidence', {}).get('state') == 'analysis' and '与' in name and all(part in candidates for part in name.split('与')):
            notes.append(source)
            continue
        if not name:
            continue
        old = merged.get(name)
        if old is None:
            old = dict(source, name=name, records=[])
            merged[name] = old
        old['records'].append(original)
        aliases[source['id']] = old['id']
        rank = priority.get(source.get('evidence', {}).get('state'), 0)
        old_rank = priority.get(old.get('evidence', {}).get('state'), 0)
        if rank >= old_rank:
            old.update(summary=source.get('summary', ''), evidence=source.get('evidence', {}))
        if source.get('role') in {'protagonist', '主角'} or old.get('role') not in {'protagonist', '主角'} and rank >= old_rank:
            old['role'] = source.get('role', '')
    edges, seen = [], set()
    for edge in data.get('relationships', []):
        start, end = aliases.get(edge['source']), aliases.get(edge['target'])
        key = (start, end, edge['label'])
        if start and end and start != end and key not in seen:
            seen.add(key)
            edges.append(dict(edge, source=start, target=end))
    data['character_notes'] = notes
    return list(merged.values()), edges


FIELD_NAMES = {
    'title': '书名', 'name': '名称', 'premise': '故事前提', 'logline': '故事梗概',
    'synopsis': '故事梗概', 'summary': '概要', 'goal': '目标', 'goals': '阶段目标',
    'objective': '目标', 'objectives': '阶段目标', 'stages': '阶段规划', 'phases': '阶段规划',
    'acts': '分幕规划', 'arcs': '故事线', 'chapters': '章节规划', 'outline': '大纲',
    'plot': '主线', 'main_plot': '主线', 'rules': '写作约定', 'constraints': '写作约定',
    'characters': '人物', 'setting': '背景', 'ending': '结局', 'theme': '主题',
    'description': '说明', 'events': '情节', 'beats': '情节节点', 'conflict': '冲突',
    'stakes': '代价', 'resolution': '解决方式', 'content': '内容', 'notes': '补充说明',
    'number': '章节序号', 'time': '时间', 'location': '地点', 'reveal': '揭示的信息',
    'emotion': '情绪变化', 'end': '结束状态',
}
INTERNAL_FIELDS = {'status', 'id', 'schema', 'schema_version', 'version', 'created_at',
    'updated_at', 'metadata', 'source', 'source_path', 'story_index', 'type'}


def outline_sections(content, *, chapter=False):
    """Translate structured outlines, leaving normal prose/Markdown intact."""
    text = content.strip()
    if text.startswith('```'):
        text = re.sub(r'^```[^\n]*\n|\n```\s*$', '', text)
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return [('大纲正文', text)] if text else []

    def describe(item, depth=0):
        if isinstance(item, str):
            return item.strip()
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            return str(item)
        if isinstance(item, list):
            return '\n'.join(f'• {line}' for entry in item if (line := describe(entry, depth + 1)))
        if isinstance(item, dict):
            lines = []
            for key, entry in item.items():
                if key in INTERNAL_FIELDS:
                    continue
                body = describe(entry, depth + 1)
                if body:
                    label = '标题' if key == 'title' else FIELD_NAMES.get(key, key if re.search(r'[\u4e00-\u9fff]', key) else '补充说明')
                    lines.append(label + '：' + body)
            return '\n'.join(lines)
        return ''

    if not isinstance(value, dict):
        body = describe(value)
        return [('大纲正文', body)] if body else []
    sections = []
    for key, entry in value.items():
        if key in INTERNAL_FIELDS:
            continue
        if key == 'rules' and isinstance(entry, list):
            entry = [row[2] if isinstance(row, list) and len(row) == 3
                and all(isinstance(v, str) and re.fullmatch(r'[a-z_-]+',v) for v in row[:2]) else row for row in entry]
        body = describe(entry)
        if body:
            label = '章节标题' if key == 'title' and chapter else FIELD_NAMES.get(key, key if re.search(r'[\u4e00-\u9fff]', key) else '补充说明')
            sections.append((label, body))
    return sections


def outline_chapters(content):
    try:
        value = json.loads(content)
    except (ValueError, TypeError):
        return []
    chapters = value.get('chapters', []) if isinstance(value, dict) else []
    if not isinstance(chapters, list):
        return []
    return [chapter for chapter in chapters if isinstance(chapter, dict)
        and isinstance(chapter.get('title'), str)]


def chapter_label(chapter):
    number = chapter.get('sequence', chapter.get('number'))
    title = chapter['title']
    prefix = f'第{number}章'
    return prefix + ' · ' + title if number is not None and not title.startswith(prefix) else title


def source_detail(node):
    records = node.get('records', [node])
    lines = []
    states = {'canonical': '已确认设定', 'accepted': '已采用', 'final': '已定稿',
        'draft': '草稿', 'candidate': '候选资料', 'analysis': '章节分析'}
    for record in records:
        evidence = record.get('evidence', {})
        source = evidence.get('document', '本地资料')
        version = evidence.get('version')
        state = states.get(evidence.get('state'), '本地记录')
        lines.append(f"## {source}" + (f' · 第 {version} 版' if version else '') + f' · {state}')
        lines.append(record.get('summary', '') or '尚无说明')
    return '\n\n'.join(lines)
