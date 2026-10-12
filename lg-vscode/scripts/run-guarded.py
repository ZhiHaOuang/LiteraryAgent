"""Linux test runner: one heavy job at a time, bounded Node heap and cgroup headroom."""
import argparse
import fcntl
import os
import signal
import subprocess
import time
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--lock', required=True, type=Path)
parser.add_argument('--budget-mib', type=int, default=384)
parser.add_argument('--reserve-mib', type=int, default=192)
parser.add_argument('--node-heap-mib', type=int, default=256)
parser.add_argument('command', nargs=argparse.REMAINDER)
args = parser.parse_args()
command = args.command[1:] if args.command[:1] == ['--'] else args.command
if not command or min(args.budget_mib, args.reserve_mib, args.node_heap_mib) <= 0:
    parser.error('Specify a command and positive memory limits')

def headroom():
    limit = (Path('/sys/fs/cgroup') / 'memory.max').read_text().strip()
    used = int((Path('/sys/fs/cgroup') / 'memory.current').read_text())
    if limit == 'max':
        raise SystemExit('No finite cgroup limit; configure an explicit bounded test environment')
    return (int(limit) - used) // (1024 * 1024)

args.lock.parent.mkdir(parents=True, exist_ok=True)
lock = args.lock.open('a')
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit('Another guarded check is already running')
available = headroom()
if available < args.budget_mib + args.reserve_mib:
    raise SystemExit(f'Not started: {available} MiB headroom; need {args.budget_mib + args.reserve_mib} MiB')
env = dict(os.environ, NODE_OPTIONS=f'--max-old-space-size={args.node_heap_mib}')
child = subprocess.Popen(command, env=env, start_new_session=True)

def stop():
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()

def interrupted(_signum, _frame):
    raise KeyboardInterrupt

signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGINT, interrupted)
try:
    while child.poll() is None:
        if headroom() < args.reserve_mib:
            print('Memory reserve reached; stopping this check before container exhaustion', flush=True)
            stop()
            raise SystemExit(75)
        time.sleep(0.2)
    raise SystemExit(child.returncode)
finally:
    if child.poll() is None:
        stop()
