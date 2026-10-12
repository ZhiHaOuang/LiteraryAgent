"""Small desktop control surface over the same bookshelf and auth stores as CLI."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from .bookshelf import Bookshelf, remember_location, restore_location
from .config import load_config
from .credentials import environment_dir, read_profiles, save_profile
from .project_store import ProjectRegistry, ProjectStore
from .providers import PROVIDERS


class DesktopError(ValueError):
    pass


def inventory(workspace: Path, environment: str | None = None) -> dict:
    root = workspace.expanduser().resolve()
    restored = restore_location(root, environment)
    shelf = Bookshelf.discover(root)
    if shelf is None and Bookshelf(root / 'Projects').marker.is_file():
        shelf = Bookshelf(root / 'Projects')
    books = shelf.books() if shelf else [p for p in ProjectRegistry().list() if p['initialized'] and p['workspace_exists']]
    current = restored if ProjectStore(restored).initialized else None
    if current and shelf and current.parent != shelf.root:
        current = None
    if current and not any(Path(p['workspace']).resolve() == current for p in books):
        info = ProjectStore(current).project_info()
        books.insert(0, {'name': info.name, 'project_id': info.project_id, 'workspace': str(current)})
    config = load_config(current or root, environment=environment)
    space = config.environment or environment or 'sandbox'
    saved = read_profiles(space)
    return {
        'workspace': str(current) if current else '',
        'shelf': str(shelf.root) if shelf else '',
        'books': [{k: p[k] for k in ('name', 'project_id', 'workspace')} for p in books],
        'environment': space,
        'active': saved['active'] or '',
        'model': config.default_model,
        'provider': config.provider,
        'runtimeManifest': str(config.runtime_manifest) if config.runtime_manifest else '',
        'credentialsPath': str(environment_dir(space) / 'credentials.json'),
        'profiles': [{'name': name, **{k: profile[k] for k in ('provider', 'protocol', 'model', 'base_url', 'auth_mode')}}
                     for name, profile in sorted(saved['profiles'].items())],
        'providers': [{'id': key, 'name': p.name, 'protocol': p.protocol, 'base_url': p.base_url, 'model': p.default_model}
                      for key, p in PROVIDERS.items()],
    }


def control(workspace: Path, environment: str | None, request: dict) -> dict:
    allowed = {
        'inventory': {'action'}, 'remember': {'action', 'workspace'},
        'use': {'action', 'name'}, 'model': {'action', 'name', 'model'},
        'add': {'action', 'name', 'provider', 'model', 'base_url', 'key_env', 'replace'},
    }
    action = request.get('action') if isinstance(request, dict) else None
    if action not in allowed or set(request) - allowed[action]:
        raise DesktopError('INVALID_REQUEST')
    for key, value in request.items():
        if key != 'replace' and (not isinstance(value, str) or len(value) > 4096):
            raise DesktopError('INVALID_REQUEST')
    if 'replace' in request and not isinstance(request['replace'], bool):
        raise DesktopError('INVALID_REQUEST')
    data = inventory(workspace, environment)
    space = data['environment']
    if action == 'remember':
        target = Path(request['workspace']).expanduser().resolve()
        if not ProjectStore(target).initialized:
            raise DesktopError('NOT_A_BOOK')
        if data['shelf']:
            Bookshelf(Path(data['shelf'])).check_book(target)
        remember_location(target, space)
        return inventory(target, space)
    if action in {'use', 'model'}:
        save_profile(space, request['name'], model=request.get('model'))
    elif action == 'add':
        variable = request.get('key_env', '')
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', variable):
            raise DesktopError('INVALID_KEY_ENV')
        key = os.environ.get(variable, '')
        if not key:
            raise DesktopError('KEY_ENV_MISSING')
        save_profile(space, request['name'], key, provider=request['provider'], model=request['model'],
                     base_url=request['base_url'], replace=request.get('replace', False))
    return inventory(workspace, space)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True, type=Path)
    parser.add_argument('--environment')
    args = parser.parse_args()
    try:
        raw = sys.stdin.read(20001)
        if len(raw) > 20000:
            raise DesktopError('INVALID_REQUEST')
        result = control(args.workspace, args.environment, json.loads(raw))
        print(json.dumps({'ok': True, 'data': result}, ensure_ascii=False))
    except (ValueError, OSError, KeyError, TypeError):
        # Never forward provider bodies, credentials, or arbitrary exception text.
        error = sys.exc_info()[1]
        print(json.dumps({'ok': False, 'error': str(error) if isinstance(error, DesktopError) else 'CONFIG_INVALID'}))


if __name__ == '__main__':
    main()
