"""Paint the book before importing workflow engines and terminal input machinery."""
from __future__ import annotations

import os
import sys
import json
import sqlite3

initial_home = None
painted = False


def _watch(paths):
    result = []
    for path in paths:
        try:
            stat = os.stat(path)
            result.append([str(path), None, None] if str(path).endswith('-wal') and stat.st_size == 0
                else [str(path), stat.st_mtime_ns, stat.st_size])
        except FileNotFoundError:
            result.append([str(path), None, None])
    return result


def _screen_path(root, environment, width, height):
    from .home_cache import cache_path
    import hashlib
    identity = json.dumps([environment, width, height, os.environ.get('NO_COLOR'),
        os.environ.get('TERM'), os.environ.get('LG_MODEL'), os.environ.get('LITERARYGIANT_MODEL'),
        os.environ.get('LG_PROVIDER'), os.environ.get('LITERARYGIANT_PROVIDER')])
    key = hashlib.sha256(identity.encode()).hexdigest()[:20]
    return cache_path(root).with_suffix('.' + key + '.screen.json')


def save_frame(config, data, width, height):
    from .book_home import render_home, render_home_header
    from datetime import datetime, timezone
    from .config import load_config
    if load_config(config.workspace, environment=config.environment).model_label != config.model_label:
        return
    screen_data = dict(data, refreshing=True, cached_at=datetime.now(timezone.utc).isoformat())
    rows = (render_home_header(screen_data, width, height=height) + '\n'
        + render_home(screen_data, width, heading=False)).splitlines()[:max(1, height - 2)]
    _write_frame(config, '\r\n'.join(rows), width, height)


def _write_frame(config, frame, width, height):
    from .credentials import environment_dir
    from .preferences import personal_path, book_path
    from .output_writer import _atomic_text
    from .project_store import ProjectStore
    store = ProjectStore(config.workspace)
    if not store.initialized:
        return
    paths = [*config.loaded_files, personal_path(), book_path(config.workspace),
        environment_dir(config.environment or 'sandbox') / 'credentials.json',
        config.workspace / '.literarygiant' / 'project.json',
        config.workspace / '.literarygiant' / 'story.sqlite3',
        config.workspace / '.literarygiant' / 'story.sqlite3-wal',
        config.workspace / 'ReferenceLibrary/bible/book-overview.json']
    payload = {'schema': 4, 'watch': _watch(paths), 'frame': frame,
        'project_id': store.project_info().project_id}
    path = _screen_path(config.workspace, config.environment, width, height)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_text(path, json.dumps(payload, ensure_ascii=False))
    path.chmod(0o600)


def _paint(frame):
    global painted
    sys.stdout.write('\x1b[?1049h\x1b[H\x1b[2J' + frame
        + '\r\n\x1b[90m正在准备交互…\x1b[0m')
    sys.stdout.flush()
    painted = True


def first_frame(argv):
    global initial_home
    if not (sys.stdin.isatty() and sys.stdout.isatty() and os.isatty(0) and os.isatty(1)):
        return
    options = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in {'-C', '--cwd', '--shelf', '--environment', '--model', '--conversation'}:
            if index + 1 >= len(argv):
                return
            options[token] = argv[index + 1]
            index += 2
        elif token == '--debug':
            index += 1
        else:
            return
    try:
        from pathlib import Path
        root = options.get('-C') or options.get('--cwd') or options.get('--shelf')
        environment = options.get('--environment')
        if root:
            root = Path(root).expanduser().resolve()
        else:
            from .bookshelf import restore_location
            root = restore_location(Path.cwd(), environment)
        size = os.get_terminal_size(1)
        width = max(12, size.columns - 1)
        cache_environment = environment
        if environment is None:
            # load_config resolves an active default profile into the sandbox
            # environment; use the same cache identity without loading config.
            from .credentials import read_profiles
            profiles = read_profiles('sandbox')
            if profiles['profiles'].get(profiles.get('active')):
                cache_environment = 'sandbox'
        if '--model' not in options:
            try:
                cached = json.loads(_screen_path(root, cache_environment, width, size.lines).read_text(encoding='utf-8'))
                if cached.get('schema') == 4 and cached['watch'] == _watch([row[0] for row in cached['watch']]):
                    _paint(cached['frame'])
                    return
            except (OSError, ValueError, KeyError, TypeError):
                pass
        from .first_home import render_first_home
        from .config import load_config
        from .home_cache import quick_snapshot
        config = load_config(root, environment=environment)
        if options.get('--model'):
            from dataclasses import replace
            config = replace(config, default_model=options['--model'])
        data = quick_snapshot(config)
        frame = render_first_home(data,width,size.lines)
        _paint(frame)
        initial_home = (root, data)
        if '--model' not in options:
            try:
                _write_frame(config, frame, width, size.lines)
            except OSError:
                pass
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        # The normal entry point owns diagnostics; first paint must not hide errors.
        return


def restore_terminal():
    global painted
    if painted:
        sys.stdout.write('\x1b[?1049l')
        sys.stdout.flush()
        painted = False
