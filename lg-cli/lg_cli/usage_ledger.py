"""Book-owned accounting of reported usage, never token estimates."""
from __future__ import annotations

import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path


class UsageLedger:
    def __init__(self, workspace: Path):
        self.path = workspace / '.literarygiant' / 'usage.sqlite3'

    def start_capture(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=15) as db:
            db.execute('CREATE TABLE IF NOT EXISTS ledger_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO ledger_meta VALUES (?,?)',
                ('capture_started', datetime.now(timezone.utc).isoformat()))
            db.execute("""CREATE TABLE IF NOT EXISTS usage (
                request_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL,
                provider TEXT NOT NULL, model TEXT NOT NULL,
                input_tokens INTEGER, output_tokens INTEGER, source TEXT NOT NULL, partial INTEGER NOT NULL)""")

    def record(self, request_id: str, provider: str, model: str, usage: dict,
               *, source: str = 'provider', partial: bool = False) -> None:
        def count(key):
            value = usage.get(key)
            return value if type(value) is int and value >= 0 else None
        self.start_capture()
        with sqlite3.connect(self.path, timeout=15) as db:
            db.execute('INSERT OR IGNORE INTO usage VALUES (?,?,?,?,?,?,?,?)',
                (request_id, datetime.now(timezone.utc).isoformat(), provider, model,
                 count('input_tokens'), count('output_tokens'), source, int(partial)))

    def summary(self) -> dict:
        empty = {'models': [], 'input': 0, 'output': 0, 'requests': 0, 'missing': 0, 'partial': 0, 'since': None}
        rows, cutoff = [], None
        if self.path.exists():
            with sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True) as db:
                db.row_factory = sqlite3.Row
                rows = [dict(row) for row in db.execute("""SELECT provider, model, SUM(COALESCE(input_tokens,0)) AS input,
                    SUM(COALESCE(output_tokens,0)) AS output, COUNT(*) AS requests,
                    SUM(input_tokens IS NULL OR output_tokens IS NULL) AS missing,
                    SUM(partial) AS partial, MIN(timestamp) AS since
                    FROM usage GROUP BY provider, model""")]
                marker = db.execute("SELECT value FROM ledger_meta WHERE key='capture_started'").fetchone()
                cutoff = marker[0] if marker else None
        historical = self._historical(cutoff)
        rows += historical
        merged = {}
        for row in rows:
            key = (row['provider'], row['model'])
            if key not in merged:
                merged[key] = dict(row)
            else:
                for field in ('input', 'output', 'requests', 'missing', 'partial'):
                    merged[key][field] += row[field]
                merged[key]['since'] = min(merged[key]['since'], row['since'])
        rows = sorted(merged.values(), key=lambda row: row['input'] + row['output'], reverse=True)
        result = dict(empty, models=rows, historical=sum(row['requests'] for row in historical))
        for key in ('input', 'output', 'requests', 'missing', 'partial'):
            result[key] = sum(row[key] for row in rows)
        result['since'] = min((row['since'] for row in rows), default=None)
        return result

    def label_thread(self, thread: str, model: str) -> None:
        if not self.path.exists():
            return
        prefix = f'subscription:{thread}:'
        with sqlite3.connect(self.path, timeout=15) as db:
            db.execute('UPDATE usage SET model=? WHERE substr(request_id,1,?)=? AND model=?',
                (model, len(prefix), prefix, '模型名未记录'))

    def _historical(self, cutoff):
        import hashlib
        from .home_cache import cache_path
        from .output_writer import _atomic_text
        rows, seen = [], set()
        runs = self.path.parent / 'runs'
        paths = sorted(runs.glob('*/events.jsonl'))
        stamps = []
        for path in paths:
            for source in (path, path.with_name('run.json')):
                try:
                    stat = source.stat()
                    stamps.append((str(source), stat.st_mtime_ns, stat.st_size))
                except FileNotFoundError:
                    stamps.append((str(source), None, None))
        signature = hashlib.sha256(json.dumps([cutoff, stamps]).encode()).hexdigest()
        cache = cache_path(self.path.parent.parent).with_suffix('.usage.json')
        try:
            previous = json.loads(cache.read_text(encoding='utf-8'))
            if previous.get('signature') == signature:
                return previous['rows']
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        for path in paths:
            if not path.resolve().is_relative_to(runs.resolve()):
                continue
            try:
                manifest = json.loads(path.with_name('run.json').read_text(encoding='utf-8'))
                with path.open(encoding='utf-8') as stream:
                    for line in stream:
                        try:
                            event = json.loads(line)
                            stamp = event['timestamp']
                            datetime.fromisoformat(stamp)
                            if cutoff and stamp >= cutoff:
                                continue
                            payload = (event.get('data') or {}).get('engine_event') or {}
                            if payload.get('type') != 'turn.completed':
                                continue
                            usage = payload.get('usage') or {}
                            counts = [usage.get(k) for k in ('input_tokens','output_tokens')]
                            if any(type(v) is not int or v < 0 for v in counts):
                                continue
                            key = (path.parent.name, event['sequence'])
                            if key in seen:
                                continue
                            seen.add(key)
                            rows.append({'provider':manifest.get('provider','历史提供方未记录'),
                                'model':payload.get('model') or '历史模型（名称未记录）',
                                'input':counts[0], 'output':counts[1], 'requests':1,
                                'missing':0,'partial':0,'since':stamp})
                        except (ValueError, KeyError, TypeError, AttributeError):
                            continue
            except (OSError, ValueError, TypeError):
                continue
        try:
            cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _atomic_text(cache, json.dumps({'signature': signature, 'rows': rows}, ensure_ascii=False))
            cache.chmod(0o600)
        except OSError:
            pass
        return rows
