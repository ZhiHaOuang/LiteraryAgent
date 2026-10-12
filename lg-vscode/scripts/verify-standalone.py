"""Build a new isolated copy without runtime, Qwen tree, node_modules or accounts."""
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

source = Path(__file__).resolve().parent.parent
if '/LitIsLand/' not in str(source):
    raise SystemExit('Run this dependency experiment in LitIsLand')
target = Path(tempfile.mkdtemp(prefix='lg-standalone-', dir=source.parent))
for name in ('src', 'shared', 'webview', 'media', 'tests', 'docs', 'scripts', '.vscode', 'fixtures'):
    shutil.copytree(source / name, target / name)
for name in ('package.json', 'package-lock.json', 'tsconfig.json', '.vscodeignore', '.gitignore', 'README.md'):
    shutil.copy2(source / name, target / name)
evidence = source / '.test-output' / 'standalone.json'
report = {'directory': str(target), 'steps': []}
evidence.parent.mkdir(parents=True, exist_ok=True)
evidence.write_text(json.dumps(report, indent=2))
commands = [
    ['npm', 'ci', '--offline', '--ignore-scripts', '--no-audit', '--no-fund'],
    ['npm', 'run', 'typecheck'], ['npm', 'test'], ['npm', 'run', 'build'], ['npm', 'run', 'package'],
]
for command in commands:
    result = subprocess.run(command, cwd=target, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    # Keep deterministic status/count evidence; never store npm/provider diagnostics verbatim.
    counts = re.findall(r'^# (?:tests|pass|fail|skipped|duration_ms) [0-9.]+$', result.stdout.decode(errors='replace'), re.M)
    report['steps'].append({'command': command, 'exitCode': result.returncode,
        'outputSHA256': hashlib.sha256(result.stdout).hexdigest(), 'testSummary': counts})
    evidence.write_text(json.dumps(report, indent=2))
    if result.returncode:
        raise SystemExit(result.returncode)
report['vsixSHA256'] = hashlib.sha256((target / 'literarygiant-vscode-0.1.0.vsix').read_bytes()).hexdigest()
evidence.write_text(json.dumps(report, indent=2))
