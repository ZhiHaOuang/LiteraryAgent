"""Measure actual CLI first paint and input readiness, without model calls."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time

from lg_cli.config import load_config
from lg_cli.home_cache import save_snapshot
from lg_cli.book_home import snapshot
from lg_cli.project_store import ProjectStore


def measure(command, workspace, env):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH',30,64,0,0))
    start = time.perf_counter()
    process = subprocess.Popen(command + ['--environment','ui-benchmark','-C',str(workspace)],
        stdin=slave,stdout=slave,stderr=slave,env=env)
    os.close(slave)
    received = b''
    painted = ready = None
    try:
        while time.perf_counter() - start < 10:
            if select.select([master], [], [], .05)[0]:
                try:
                    received += os.read(master,65536)
                except OSError:
                    break
            elapsed = (time.perf_counter() - start) * 1000
            if painted is None and '正在准备交互'.encode() in received:
                painted = elapsed
            if '/ 命令 · /browse 浏览'.encode() in received:
                ready = elapsed
                break
        cache_deadline = time.monotonic() + 3
        while not list((Path(env['XDG_CACHE_HOME']) / 'literarygiant/home').glob('*.screen.json')) and time.monotonic() < cache_deadline:
            if select.select([master], [], [], .02)[0]:
                os.read(master,65536)
        os.write(master,b'\x04')
        exit_deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < exit_deadline:
            if select.select([master], [], [], .02)[0]:
                try:
                    received += os.read(master,65536)
                except OSError:
                    break
        process.wait(timeout=1)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        os.close(master)
    if painted is None or ready is None or process.returncode != 0:
        raise RuntimeError(f'CLI did not reach the measured states: paint={painted}, ready={ready}, exit={process.returncode}\n{received[-2500:]!r}')
    return {'first_home_ms':round(painted,1),'input_ready_ms':round(ready,1)}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--command',default=None)
    parser.add_argument('--runs',type=int,default=5)
    parser.add_argument('--workspace',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    command=[args.command] if args.command else [sys.executable,'-m','lg_cli']
    with tempfile.TemporaryDirectory(prefix='lg-home-benchmark-') as raw:
        root=Path(raw)
        env={**os.environ,'TERM':'xterm-256color','XDG_CACHE_HOME':str(root/'cache'),
            'XDG_CONFIG_HOME':str(root/'prefs')}
        store=ProjectStore(args.workspace or root/'book')
        if args.workspace is None:
            store.initialize(name='长夜来信')
            for i in range(1,13):
                store.create_document(kind='chapter',slug=f'chapter-{i}',title=f'第{i}章',
                    content='用于性能测试的正文。'*300,sequence=i)
        results=[]
        for index in range(args.runs):
            result=measure(command,store.workspace,env)
            results.append(result)
            print(f'Run {index + 1}: {result}',flush=True)
        if args.output:
            args.output.write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(results,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
