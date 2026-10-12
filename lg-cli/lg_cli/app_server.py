"""LG-owned launcher: app-server inherits stdio without a protocol translation layer."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .core_adapter import _adapter_env, _locked_runtime
from .native import writing_args
from .paths import core_codex_root
from .provider_transport import provider_bridge
from .runtime_context import writing_instructions


def launch(config, manifest: Path, *, describe: bool = False) -> int:
    if not manifest.is_absolute():
        raise ConfigError('Runtime manifest must be an absolute path')
    candidate = _locked_runtime(manifest, core_codex_root(), probe=True)
    if not candidate.available:
        raise ConfigError(candidate.reason)
    if not (config.uses_anthropic or config.uses_responses or config.auth_mode == 'chatgpt'):
        raise ConfigError('The extension requires an explicitly configured Messages, Responses or ChatGPT profile')
    if not config.credentials_configured:
        raise ConfigError('Configure an LG provider profile before connecting the extension')
    if describe:
        print(json.dumps({'schema': 1, 'workspace': str(config.workspace),
            'model': config.default_model, 'runtimeVersion': candidate.version,
            'baseInstructions': writing_instructions(config)}, ensure_ascii=False))
        return 0
    provider = []
    if config.auth_mode == 'chatgpt':
        from .subscription_auth import subscription_args, subscription_status
        ok, message = subscription_status(config, command=list(candidate.command))
        if not ok:
            raise ConfigError(message)
        provider = subscription_args()
    with provider_bridge(config) as bridge:
        env = _adapter_env(config)
        env.pop('CODEX_API_KEY', None)
        if bridge:
            env['LG_BRIDGE_TOKEN'] = bridge.token
            provider = bridge.codex_args()
        env['NO_PROXY'] = env['no_proxy'] = ','.join(filter(None, [env.get('NO_PROXY', ''), '127.0.0.1,localhost,::1']))
        command = [*candidate.command, *provider, *writing_args(config, require_elicitation=True),
            '-c', 'features.multi_agent=false', '-c', 'features.multi_agent_v2=false',
            'app-server', '--listen', 'stdio://']
        process = subprocess.Popen(command, cwd=config.workspace, env=env)
        # Keep the credential bridge alive until the inherited-stdio child exits.
        def terminate(signum, _frame):
            if process.poll() is None:
                process.send_signal(signum)
        previous = {s: signal.signal(s, terminate) for s in (signal.SIGTERM, signal.SIGINT)}
        try:
            return process.wait()
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True, type=Path)
    parser.add_argument('--runtime-manifest', required=True, type=Path)
    parser.add_argument('--describe', action='store_true')
    parser.add_argument('--environment')
    args = parser.parse_args()
    try:
        code = launch(load_config(args.workspace, environment=args.environment), args.runtime_manifest, describe=args.describe)
    except (ConfigError, ValueError, OSError):
        # Never copy upstream bodies, command environments or credentials to the client.
        print('LG launcher failed: verify the LG profile, absolute runtime manifest and binary checksum.', file=sys.stderr)
        code = 1
    raise SystemExit(code)


if __name__ == '__main__':
    main()
