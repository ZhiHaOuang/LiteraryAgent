#!/usr/bin/env python3
"""Replay product tests with synthetic user state and no inherited credentials.

Run in a LitIsLand checkout. This is an engineering check, not a quality eval.
No dependency installation, model selection, Runtime discovery or data cleanup.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


SMOKE = [
    'tests.test_candidate_bases', 'tests.test_direction_candidate_guard',
    'tests.test_literary_tasks', 'tests.test_review_delivery',
    'tests.test_operation_receipts', 'tests.test_chapter_sync',
    'tests.test_narrative_execution', 'tests.test_source_revalidation',
]
BOOTSTRAP = '''import os,pathlib,sys
root=os.environ.get("LG_REPLAY_USER_DIRECTORY")
if root:
    original=pathlib.Path.home
    pathlib.Path.home=classmethod(lambda cls: original() if os.environ.get("HOME") else cls(root))
    def audit(event,args):
        if event=="socket.connect" and isinstance(args[1],tuple) and args[1][0] not in {"127.0.0.1","::1","localhost"}:
            raise PermissionError("Replay permits only local synthetic model connections")
    sys.addaudithook(audit)
'''


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--suite',choices=['smoke','full'],default='smoke')
    parser.add_argument('--jobs',type=int,choices=range(1,5),default=1)
    parser.add_argument('--output',type=Path,required=True,help='New external evidence directory; never a book.')
    args=parser.parse_args(argv)
    root=args.source.resolve()
    if not (root/'lg-cli/lg_cli').is_dir() or not (root/'tests/helpers.py').is_file():
        parser.error('--source must identify the product source root')
    output=args.output.resolve()
    if output.exists() or output.is_relative_to(root):
        parser.error('--output must be a new directory outside product source')
    for parent in (output,*output.parents):
        if (parent/'.literarygiant/project.json').exists() or (parent/'.literarygiant/story.sqlite3').exists():
            parser.error('--output must not be inside a work of fiction')
    output.mkdir(parents=True)
    bootstrap=output/'bootstrap'
    bootstrap.mkdir()
    (bootstrap/'sitecustomize.py').write_text(BOOTSTRAP)
    temporary=output/'tmp'
    temporary.mkdir()
    modules=SMOKE if args.suite=='smoke' else [
        'tests.'+path.stem for path in sorted((root/'tests').glob('test_*.py'))]
    groups=[modules[index::args.jobs] for index in range(args.jobs)]
    groups=[group for group in groups if group]
    # Deliberately no HOME, provider variables, API keys or user Python settings.
    # Tests may set their own temporary HOME. No system variable is repurposed.
    env={'PATH':'/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1','TMPDIR':str(temporary),
         'PYTHONPATH':os.pathsep.join(map(str,(bootstrap,root/'lg-cli',root)))}

    def run(index,group):
        private=output/('synthetic-user-'+str(index))
        private.mkdir()
        current={**env,'LG_REPLAY_USER_DIRECTORY':str(private)}
        command=[sys.executable,'-B','-m','unittest',*group,'-v']
        started=time.monotonic()
        log=output/('group-'+str(index)+'.log')
        with log.open('w') as stream:
            process=subprocess.run(command,cwd=root,env=current,stdout=stream,stderr=subprocess.STDOUT)
        text=log.read_text()
        count=re.search(r'Ran (\d+) tests?',text)
        skip=re.search(r'OK \(skipped=(\d+)\)',text)
        result={'group':index,'modules':group,'exitCode':process.returncode,
                'tests':int(count[1]) if count else None,'skipped':int(skip[1]) if skip else 0,
                'seconds':round(time.monotonic()-started,3),'log':log.name,'command':command}
        print(json.dumps(result,ensure_ascii=False),flush=True)
        return result

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures=[pool.submit(run,index,group) for index,group in enumerate(groups)]
        results=[future.result() for future in futures]
    inputs={str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest()
            for folder in ('lg-cli/lg_cli','tests') for path in sorted((root/folder).rglob('*'))
            if path.is_file() and path.suffix in {'.py','.json','.txt'}}
    report={'schema':'lg.review-replay.v1','suite':args.suite,'source':str(root),
            'python':sys.version.split()[0],'platform':sys.platform,'modules':len(modules),
            'total':sum(result['tests'] or 0 for result in results),
            'skipped':sum(result['skipped'] for result in results),'groups':results,
            'inputs':inputs,'credentialsInherited':False,'paidModelCalls':False,
            'limitations':['Core tests remain conditional without a pinned artifact.',
                          'Network guard covers the test process; workers use synthetic provider fixtures.',
                          'Engineering tests do not measure literary quality.']}
    (output/'validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return int(any(result['exitCode'] for result in results))


if __name__=='__main__':
    raise SystemExit(main())
