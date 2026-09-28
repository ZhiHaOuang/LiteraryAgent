"""Book-scoped presentation cache outside the checkout; never a source of story truth."""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

def cache_path(workspace):
    root = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'literarygiant' / 'home'
    key = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()
    return root / (key + '.json')


def source_signature(workspace):
    """Cheap source stamps, including the targets of source-bound artifact indices."""
    paths = {workspace / '.literarygiant' / name for name in
        ('story.sqlite3', 'story.sqlite3-wal', 'project.json', 'assets.json')}
    from .book_overview import RELATIVE_PATH
    paths.add(workspace / RELATIVE_PATH)
    for relative in ('ReferenceLibrary/analyses', 'ReferenceLibrary/indices', '.literarygiant/analysis'):
        root = workspace / relative
        if root.resolve().is_relative_to(workspace.resolve()):
            paths.update(path for path in root.rglob('*') if path.is_file()
                and path.resolve().is_relative_to(workspace.resolve()))
    for index in (workspace / 'ReferenceLibrary/indices').glob('artifact-*.json'):
        if not index.resolve().is_relative_to(workspace.resolve()):
            continue
        try:
            source = (workspace / json.loads(index.read_text(encoding='utf-8'))['path']).resolve()
            if source.is_relative_to(workspace.resolve()):
                paths.add(source)
        except (OSError, ValueError, TypeError, KeyError):
            pass
    stamps = []
    for path in sorted(paths):
        try:
            stat = path.stat()
            if path.name == 'story.sqlite3-wal' and stat.st_size == 0:
                # Read-only SQLite opens can leave an empty WAL; it carries no edits.
                stamps.append((str(path.relative_to(workspace)), None, None))
            else:
                stamps.append((str(path.relative_to(workspace)), stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            stamps.append((str(path.relative_to(workspace)), None, None))
    return hashlib.sha256(json.dumps(stamps).encode()).hexdigest()


def read_snapshot(config):
    from .project_store import ProjectStore
    try:
        data = json.loads(cache_path(config.workspace).read_text(encoding='utf-8'))
        if data.get('schema') == 2 and data.get('project_id') == ProjectStore(config.workspace).project_info().project_id:
            return data
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def save_snapshot(config, data):
    from .output_writer import _atomic_text
    from .project_store import ProjectStore
    signature = source_signature(config.workspace)
    if data.get('source_signature', signature) != signature:
        return False
    payload = dict(data, schema=2, source_signature=signature, cached_at=datetime.now(timezone.utc).isoformat(),
        project_id=ProjectStore(config.workspace).project_info().project_id)
    # Personal settings are read afresh; no credentials or config objects enter this cache.
    for key in ('preferences', 'model', 'provider'):
        payload.pop(key, None)
    path = cache_path(config.workspace)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_text(path, json.dumps(payload, ensure_ascii=False) + '\n')
    path.chmod(0o600)
    return True


def quick_snapshot(config):
    from .book_presentation import people
    from .preferences import effective_preferences
    from .project_store import ProjectStore
    store = ProjectStore(config.workspace)
    data = {'name': '尚未选择书籍', 'chapters': [], 'characters': [], 'events': [],
        'relationships': [], 'links': [], 'outlines': [], 'missing': 0,
        'usage': {'models': [], 'input': 0, 'output': 0, 'requests': 0,
            'missing': 0, 'partial': 0, 'since': None}, 'refreshing': True}
    if store.initialized:
        info = store.project_info()
        path = cache_path(config.workspace)
        try:
            cached = json.loads(path.read_text(encoding='utf-8'))
            if cached.get('schema') == 2 and cached.get('project_id') == info.project_id:
                data.update(cached)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        data['name'] = info.name
        # Counts and latest chapter are always read from the live DB, even with a cache.
        with closing(sqlite3.connect(store.database_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.05)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("""SELECT d.id,d.kind,d.title,d.sequence,d.state,
                COALESCE(v.content,'') AS content, v.metadata_json, v.version_number
                FROM documents d LEFT JOIN document_versions v ON v.id=d.active_version_id
                WHERE d.state NOT IN ('archived','rejected') ORDER BY d.sequence,d.id""").fetchall()
            written_parents = {row[0] for row in db.execute("""SELECT s.chapter_id
                FROM scene_cards s JOIN documents d ON d.id=s.document_id
                JOIN document_versions v ON v.id=d.active_version_id
                WHERE s.status<>'archived' AND trim(v.content)<>''""")}
            facts = [dict(row) for row in db.execute("SELECT id,category,fact_key,value,tags_json FROM story_facts WHERE state='canonical'")]
            timeline = [dict(row) for row in db.execute("SELECT id,label,event,sort_key,state FROM timeline_entries WHERE state='canonical' ORDER BY sort_key,id")]
        data['chapters'] = [dict(row, has_prose=bool(row['content'].strip()) or row['id'] in written_parents)
            for row in rows if row['kind'] == 'chapter']
        data['outlines'] = [dict(row) for row in rows if row['kind'] == 'outline']
        if not data.get('cached_at'):
            for document in rows:
                raw = json.loads(document['metadata_json'] or '{}').get('story_index', {})
                evidence = {'document': document['title'], 'kind': document['kind'],
                    'state': document['state'], 'version': document['version_number']}
                for group in ('characters', 'events', 'relationships', 'links'):
                    for entry in raw.get(group, []):
                        node = dict(entry, evidence=evidence)
                        for field in ('id', 'source', 'target'):
                            if field in node:
                                node[field] = str(document['id']) + ':' + node[field]
                        data[group].append(node)
                if document['kind'] == 'character' and not raw.get('characters'):
                    data['characters'].append({'id': str(document['id']), 'name': document['title'],
                        'role': '', 'summary': document['content'], 'evidence': evidence})
            for fact in facts:
                category = (fact['category'] + ' ' + fact['tags_json']).lower()
                if any(term in category for term in ('character','protagonist','人物','角色','主角')):
                    data['characters'].append({'id':f"fact:{fact['id']}", 'name':fact['fact_key'],
                        'role':'主角' if any(term in category for term in ('protagonist','主角')) else '',
                        'summary':fact['value'], 'evidence':{'state':'canonical','document':'已确认设定'}})
            data['events'].extend({'id':f"timeline:{entry['id']}", 'label':entry['label'],
                'summary':entry['event'], 'planned':False, 'evidence':{'state':'canonical',
                'document':'时间线','order':entry['sort_key']}} for entry in timeline)
            data['characters'], data['relationships'] = people(data)
            data['usage_pending'] = True
    from .book_overview import read_overview
    data['overview'] = read_overview(config.workspace, data.get('outlines', []))
    return dict(data, refreshing=True, preferences=effective_preferences(config.workspace),
        model=config.model_label, provider=config.provider)
