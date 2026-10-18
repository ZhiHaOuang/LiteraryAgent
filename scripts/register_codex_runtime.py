"""Register a locally built derivative; never build, download or replace a binary."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lg-cli'))
from lg_cli.runtime_manifest import file_sha256, read_runtime_identity


def source_identity(source: Path, pin: str) -> tuple[str, str]:
    def git(*args: str) -> str:
        result = subprocess.run(['git', *args], cwd=source, text=True, capture_output=True, check=False)
        if result.returncode:
            raise ValueError('Cannot verify derivative source repository')
        return result.stdout.strip()

    if Path(git('rev-parse', '--show-toplevel')).resolve() != source.resolve():
        raise ValueError('Source must be the root of an isolated Codex source repository')
    if git('status', '--porcelain', '--untracked-files=all'):
        raise ValueError('Commit derivative source changes before registering the runtime')
    git('merge-base', '--is-ancestor', pin, 'HEAD')
    return git('rev-parse', 'HEAD'), git('rev-parse', 'HEAD^{tree}')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True, help='Clean Git repository derived from the pinned Codex commit')
    parser.add_argument('--tag', required=True, help='Upstream baseline tag')
    parser.add_argument('--build-profile', required=True)
    parser.add_argument('--version', required=True, help='Expected --version output from the build')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    pin = (ROOT / 'CODEX_CORE_COMMIT').read_text().strip()
    source = args.source.expanduser().resolve()
    commit, tree = source_identity(source, pin)
    tag_commit = subprocess.run(['git', 'rev-parse', f'refs/tags/{args.tag}^{{commit}}'], cwd=source,
        capture_output=True, text=True, check=False)
    if tag_commit.returncode or tag_commit.stdout.strip() != pin:
        raise ValueError('Upstream tag must resolve locally to CODEX_CORE_COMMIT')
    binary = args.binary.expanduser().resolve(strict=True)
    digest = file_sha256(binary)
    probe = subprocess.run([str(binary), '--version'], capture_output=True, text=True, timeout=15, check=False)
    if probe.returncode or probe.stdout.strip() != args.version:
        raise ValueError('Runtime version probe does not match the expected build version')
    if file_sha256(binary) != digest:
        raise ValueError('Runtime binary changed during verification')
    manifest = {
        'schema_version': 2, 'kind': 'lg-derived', 'commit': pin, 'tag': args.tag,
        'source_commit': commit, 'source_tree': tree, 'build_profile': args.build_profile,
        'protocol': 'codex-app-server-v2', 'version': args.version,
        'binary': str(binary), 'binary_sha256': digest,
    }
    # Validate before publishing; an existing manifest must never be overwritten.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='lg-runtime-register-') as temporary:
        candidate = Path(temporary) / 'runtime.json'
        candidate.write_text(json.dumps(manifest))
        read_runtime_identity(candidate, pin=pin)
    with args.output.expanduser().open('x', encoding='utf-8') as output:
        output.write(json.dumps(manifest, indent=2) + '\n')
    print(f'Registered derivative runtime: {args.output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
