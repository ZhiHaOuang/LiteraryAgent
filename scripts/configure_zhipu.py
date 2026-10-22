#!/usr/bin/env python3
"""One hidden local input for the two explicitly selected Zhipu API profiles."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

from lg_cli.credentials import environment_dir, read_profiles, save_profile

PROFILES = (
    ('zhipu-vision', 'glm-4.6v', 'responses-compatible', 'https://open.bigmodel.cn/api/v1'),
    ('zhipu-air', 'glm-4.5-air', 'glm', 'https://open.bigmodel.cn/api/anthropic'),
)


def preflight(environment: str) -> Path:
    saved = read_profiles(environment)
    if any(name in saved['profiles'] for name, *_ in PROFILES):
        raise ValueError('A selected profile already exists; no key was requested or replaced.')
    path = environment_dir(environment) / 'config.toml'
    if path.exists():
        if path.is_symlink() or tomllib.loads(path.read_text()).get('runtime', {}).get('engine') != 'native':
            raise ValueError('Existing runtime configuration needs an explicit merge before setup.')
    return path


def hidden_key(dialog: bool) -> str:
    if not dialog:
        return getpass.getpass('智谱 API Key（隐藏输入）：')
    script = '''with timeout of 3600 seconds
set resultDialog to display dialog "输入智谱 API Key。将配置 GLM-4.5-Air 与 GLM-4.6V；密钥仅保存到本机 LG 凭据库，随后由当前任务开始5元预算内的实验。" default answer "" with hidden answer with title "LiteraryGiant · 智谱配置" buttons {"取消", "保存"} default button "保存" cancel button "取消"
return text returned of resultDialog
end timeout'''
    result = subprocess.run(['/usr/bin/osascript', '-e', script], capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValueError('Key entry was cancelled or the local dialog is unavailable; no key saved.')
    return result.stdout.strip()


def configure(key: str, environment: str) -> dict:
    if not key or any(c.isspace() or ord(c) < 32 for c in key):
        raise ValueError('API key must be nonempty and contain no whitespace.')
    config_path = preflight(environment)
    for name, model, provider, endpoint in PROFILES:
        save_profile(environment, name, key, provider=provider, model=model, base_url=endpoint)
    if not config_path.exists():
        fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write('[runtime]\nengine = "native"\n')
            stream.flush()
            os.fsync(stream.fileno())
    return {'status': 'configured_not_probed', 'environment': environment,
            'active': 'zhipu-air', 'runtime': 'native',
            'profiles': [{'name': name, 'provider': provider, 'model': model,
                          'protocol': 'responses' if provider == 'responses-compatible' else 'anthropic',
                          'base_url': endpoint} for name, model, provider, endpoint in PROFILES],
            'credentialMode': oct((environment_dir(environment) / 'credentials.json').stat().st_mode & 0o777),
            'modelCalls': 0}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', default='sandbox')
    parser.add_argument('--dialog', action='store_true', help='Use a native Mac hidden-input dialog.')
    args = parser.parse_args(argv)
    try:
        preflight(args.environment)
        key = hidden_key(args.dialog).strip()
        result = configure(key, args.environment)
        key = None
    except Exception as exc:
        # Never expose an exception repr, subprocess output, key, or existing store.
        allowed = str(exc) if type(exc) is ValueError else type(exc).__name__
        print('Setup stopped: ' + allowed, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
