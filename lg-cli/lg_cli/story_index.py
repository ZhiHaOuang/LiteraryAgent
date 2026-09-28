"""Deterministic, source-bound book graph projection. No model calls."""
from __future__ import annotations

from dataclasses import asdict
import json

from .output_writer import _atomic_text


def _index_path(workspace, name):
    path = workspace / 'ReferenceLibrary' / 'indices' / name
    if not path.resolve().is_relative_to(workspace.resolve()):
        raise ValueError('Story index must remain inside this book.')
    return path


def validate_graph(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - {'characters', 'events', 'relationships', 'links'}:
        raise ValueError('Story index must contain characters, events, relationships and links only.')
    fields = {'characters': {'id', 'name', 'role', 'summary'},
        'events': {'id', 'label', 'summary', 'planned'},
        'relationships': {'source', 'target', 'label'}, 'links': {'source', 'target', 'label'}}
    result = {}
    for group, keys in fields.items():
        rows = value.get(group, [])
        if not isinstance(rows, list) or len(rows) > 200:
            raise ValueError('Story index groups must contain at most 200 entries.')
        for row in rows:
            if not isinstance(row, dict) or set(row) != keys:
                raise ValueError(f'Invalid {group} fields: expected {sorted(keys)}')
            for key, item in row.items():
                if key == 'planned':
                    if type(item) is not bool:
                        raise ValueError('planned must be boolean')
                elif not isinstance(item, str) or len(item) > 1200 or not item.strip():
                    raise ValueError(f'Invalid graph field: {key}')
        result[group] = rows
    for nodes, edges in [('characters', 'relationships'), ('events', 'links')]:
        ids = {node['id'] for node in result[nodes]}
        if len(ids) != len(result[nodes]):
            raise ValueError('Duplicate graph node ID')
        if any(edge['source'] not in ids or edge['target'] not in ids for edge in result[edges]):
            raise ValueError('Graph links must reference existing nodes')
    return result


def build_index(store) -> dict:
    import hashlib
    documents = [d for d in store.list_documents(limit=None) if d.state not in {'archived', 'rejected'}]
    result = {'characters': [], 'events': [], 'relationships': [], 'links': [], 'missing': 0}
    for path in sorted((store.workspace / 'ReferenceLibrary' / 'indices').glob('artifact-*.json')):
        if not path.resolve().is_relative_to(store.workspace):
            continue
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
            artifact = (store.workspace / record['path']).resolve()
            if not artifact.is_relative_to(store.workspace) or not artifact.is_file():
                continue
            if hashlib.sha256(artifact.read_bytes()).hexdigest() != record['sha256']:
                result['missing'] += 1
                continue
            if record.get('display') is False:
                continue
            graph = validate_graph(record['graph'])
            for group, entries in graph.items():
                for entry in entries:
                    row = dict(entry, evidence={'state': record['state'], 'document': record['path'],
                        'kind': 'outline' if record['path'].startswith('ReferenceLibrary/plans/outlines/') else 'artifact'})
                    for field in ('id', 'source', 'target'):
                        if field in row:
                            row[field] = path.stem + ':' + row[field]
                    result[group].append(row)
        except (OSError, ValueError, KeyError, TypeError):
            result['missing'] += 1
    for document in documents:
        if document.active_version_number is None:
            continue
        version = store.get_version(document.id, document.active_version_number)
        raw = version.metadata.get('story_index')
        if raw is None:
            if document.kind == 'character':
                result['characters'].append({'id': f'document:{document.id}', 'name': document.title,
                    'role': '主角' if any(tag in {'protagonist','主角'} for tag in document.tags) else '人物资料',
                    'summary': document.content,
                    'evidence': {'state': document.state, 'document': document.title, 'version': version.version_number}})
            if document.kind in {'chapter', 'scene'} and document.content.strip():
                result['missing'] += 1
            continue
        graph = validate_graph(raw)
        prefix = f'{document.id}:'
        source = {'document': document.title, 'document_id': document.id, 'kind': document.kind, 'version': version.version_number,
            'state': document.state}
        for group, entries in graph.items():
            for entry in entries:
                row = dict(entry, evidence=source)
                for field in ('id', 'source', 'target'):
                    if field in row:
                        row[field] = prefix + row[field]
                result[group].append(row)
    from .book_analysis import BookAnalysisStore
    analyses = BookAnalysisStore(store)
    for chapter in (d for d in documents if d.kind == 'chapter'):
        if not any((root / chapter.slug / (category + '.json')).exists()
                   for root in (analyses.root, analyses.legacy_root) for category in ('CharacterArc','EventsLibrary')):
            continue
        try:
            records = analyses.list(chapter.slug)
        except (OSError, ValueError):
            result['missing'] += 1
            continue
        for record in records:
            if record['library'] not in {'CharacterArc','EventsLibrary'} or record['status'] != 'current':
                continue
            for entry in record['report']['entries']:
                row = {'id': f"analysis:{chapter.id}:{record['library']}:{entry['key']}",
                    'summary': entry['analysis'], 'evidence': {'state': 'analysis',
                        'document': chapter.title + ' · 章节分析', 'version': chapter.active_version_number}}
                if record['library'] == 'CharacterArc':
                    result['characters'].append(dict(row, name=entry['label'], role='章节人物分析'))
                else:
                    result['events'].append(dict(row, label=entry['label'], planned=False))
    # Canonical entries and explicit scene-card plans remain useful for older books.
    for fact in store.list_facts(state='canonical', limit=None):
        category = (fact.category + ' ' + ' '.join(fact.tags)).lower()
        if any(term in category for term in ('character', 'protagonist', '人物', '角色', '主角')):
            result['characters'].append({'id': f'fact:{fact.id}', 'name': fact.key,
                'role': '主角' if any(term in category for term in ('protagonist', '主角')) else '已记录人物',
                'summary': fact.value, 'evidence': {'state': 'canonical', 'document': '已确认设定'}})
    for entry in store.list_timeline(limit=None):
        if entry.state in {'archived', 'rejected'}:
            continue
        result['events'].append({'id': f'timeline:{entry.id}', 'label': entry.label,
            'summary': entry.event, 'planned': entry.state != 'canonical',
            'evidence': {'state': entry.state, 'document': '时间线', 'order': entry.sort_key}})
    scenes = store.list_scenes(limit=None)
    for scene in scenes:
        if scene.status == 'archived':
            continue
        if scene.goal or scene.end_state:
            result['events'].append({'id': f'scene:{scene.document.id}', 'label': scene.document.title,
                'summary': '目标：' + scene.goal + '；终态：' + scene.end_state, 'planned': True,
                'evidence': {'state': scene.plan_state, 'document': '场景规划'}})
    written_parents = {s.chapter_id for s in scenes if s.document.content.strip() and s.status != 'archived'}
    result['chapters'] = [dict(asdict(d), has_prose=bool(d.content.strip()) or d.id in written_parents)
        for d in documents if d.kind == 'chapter']
    result['outlines'] = [asdict(d) for d in documents if d.kind == 'outline']
    result['name'] = store.project_info().name
    return result


def refresh_index(store) -> None:
    path = _index_path(store.workspace, 'story.json')
    _atomic_text(path, json.dumps(build_index(store), ensure_ascii=False, indent=2) + '\n')


GRAPH_INSTRUCTIONS = '''
Include story_index alongside structured prose output, or pass it to create_chapter/edit_manuscript.
It is a source-bound reading index, not Canonical promotion. Use only facts explicit in this exact
returned document; empty arrays for unknowns. Distinguish future plans with planned=true.
Schema: characters [{id,name,role,summary}], events [{id,label,summary,planned:boolean}],
relationships [{source,target,label}] between character IDs, links [{source,target,label}] between
event IDs. IDs must be unique within each node group; every endpoint must exist in that group.
Use role='protagonist' only for explicitly identified protagonists. Label links accurately (e.g.
chronological order, cause, alternative); chronological succession does not prove causation.
For a partial prose edit, do not claim that the index describes the whole document.
'''


def save_artifact_index(workspace, path, graph, *, state='candidate', display=True):
    import hashlib
    path = path.resolve()
    if not path.is_relative_to(workspace.resolve()):
        raise ValueError('Indexed artifact must belong to the book.')
    relative = str(path.relative_to(workspace.resolve()))
    key = hashlib.sha256(relative.encode()).hexdigest()
    record = {'path': relative, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'state': state, 'display': display, 'graph': validate_graph(graph)}
    _atomic_text(_index_path(workspace, f'artifact-{key}.json'),
        json.dumps(record, ensure_ascii=False, indent=2) + '\n')


def artifact_graph(workspace, path):
    import hashlib
    path = path.resolve()
    relative = str(path.relative_to(workspace.resolve()))
    key = hashlib.sha256(relative.encode()).hexdigest()
    index = _index_path(workspace, f'artifact-{key}.json')
    if not index.exists():
        return None
    record = json.loads(index.read_text(encoding='utf-8'))
    if record['sha256'] != hashlib.sha256(path.read_bytes()).hexdigest():
        return None
    return validate_graph(record['graph'])
