#!/usr/bin/env python3
"""Compare two fixed LG sources on the same bounded polish tasks; plan by default."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
from decimal import Decimal

from scripts.compare_literary_models import (
    Budget, CASES, MAX_OUTPUT, MODELS, execute_case, known_cost,
    selected_config, worst_cost, write_json,
)


def source_identity(source):
    source = Path(source).resolve()
    if not (source / 'lg-cli/lg_cli/literary_tasks.py').is_file():
        raise ValueError('A complete LG product source directory is required.')
    files = {}
    for prefix in ('lg-cli/lg_cli', 'lg-subagents', 'lg-skills', 'lg-prompts', 'lg-context'):
        for path in sorted((source / prefix).rglob('*')):
            if path.is_file() and path.suffix in {'.py', '.json', '.txt', '.md', '.yaml', '.yml', '.toml'}:
                files[str(path.relative_to(source))] = hashlib.sha256(path.read_bytes()).hexdigest()
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return {'root': str(source), 'sha256': digest, 'files': files}


def run_worker(job_path, *, config_factory=None):
    """The worker imports application modules from the explicitly bound source."""
    from lg_cli import literary_tasks, story_workflow
    job = json.loads(Path(job_path).read_text())
    source = Path(job['source']['root'])
    for module in (literary_tasks, story_workflow):
        if not Path(module.__file__).resolve().is_relative_to(source / 'lg-cli/lg_cli'):
            raise ValueError('Worker imported a different application source.')
    if source_identity(source) != job['source']:
        raise ValueError('Application source changed before execution.')
    workspace = Path(job['workspace'])
    config = (config_factory or selected_config)(workspace, job['environment'], job['profile'])
    budget = Budget(Path(job['ledger']), Decimal(job['limitCNY']), Decimal(job['carriedCNY']))
    result = execute_case(job['case'], 'lg', Path(job['folder']), config, budget)
    if source_identity(source) != job['source']:
        raise ValueError('Application source changed during execution.')
    return result


def execute_source(case, arm, profile, source, output, environment, budget, *, runner=None):
    model = MODELS[profile]
    if source_identity(source['root']) != source:
        raise ValueError('Application source changed after planning.')
    folder = output / model / case['id'] / arm
    ledger = output / 'workers' / model / case['id'] / arm
    ledger.mkdir(mode=0o700, parents=True, exist_ok=False)
    carried = str(budget.held())
    # Persist a conservative parent reservation before a child can send anything.
    record = budget.begin(model + '/' + case['id'] + '/' + arm, model, {})
    record['sourceSha256'] = source['sha256']
    budget.save()
    job = {'source': source, 'case': case, 'profile': profile, 'environment': environment,
           'folder': str(folder), 'ledger': str(ledger), 'limitCNY': str(budget.limit),
           'carriedCNY': carried,
           'workspace': str(output / 'synthetic-books' / model / case['id'] / arm)}
    job_path = ledger / 'job.json'
    write_json(job_path, job)
    tool_root = Path(__file__).resolve().parents[1]
    command = [sys.executable, '-B', str(Path(__file__).resolve()), '--worker-job', str(job_path), '--run']
    env = {**os.environ, 'PYTHONPATH': str(Path(source['root']) / 'lg-cli') + os.pathsep + str(tool_root),
           'PYTHONDONTWRITEBYTECODE': '1'}
    completed = (runner or subprocess.run)(command, cwd=source['root'], env=env,
        capture_output=True, text=True, timeout=210)
    # A missing/failed worker remains uncertain and fully reserved. Never replay it.
    if completed.returncode != 0:
        raise ValueError('Worker outcome is uncertain; stop without replay.')
    child = json.loads((ledger / 'budget.json').read_text())
    rows = child['requests']
    if (len(rows) != 1 or child['initialProbeReserveCNY'] != carried
            or rows[0]['model'] != model or rows[0]['label'] != model + '/' + case['id'] + '/lg'):
        raise ValueError('Worker accounting does not match its reserved request.')
    receipt = rows[0]
    budget.finish(record, receipt.get('usage'), transport_complete=receipt['state'] == 'completed',
        host_schema_rejected=receipt['state'] == 'host_schema_rejected')
    for key in ('seconds', 'returnedModel', 'returnedModelStatus', 'providerResponseId'):
        record[key] = receipt.get(key)
    record['workerLedger'] = str(ledger / 'budget.json')
    budget.save()
    result = json.loads((folder / 'result.json').read_text())
    if not result['activeManuscriptUnchanged'] or source_identity(source['root']) != source:
        raise ValueError('Source or manuscript invariant changed; stop.')
    return {**result, 'arm': arm, 'sourceSha256': source['sha256'], 'folder': str(folder)}


def write_review(output, results, cases):
    rng, mapping = random.Random(20261022), {}
    lines = ['# 润色优化前后匿名材料', '', '相同模型内配对，失败保留；作者评分与返修时间待填。', '']
    with (output / 'author-review.csv').open('w', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['pair', 'side', 'task_1to5', 'voice_1to5', 'manual_edit_chars', 'author_minutes', 'evidence'])
        for index, model in enumerate(MODELS.values(), 1):
            for case in cases:
                pair = f'M{index}-{case["id"]}'
                arms = ['before', 'after']; rng.shuffle(arms)
                lines.extend([f'## {pair}：{case["title"]}', '', case['request'], ''])
                for side, arm in zip(('A', 'B'), arms):
                    row = next((r for r in results if r['model'] == model and r['case'] == case['id'] and r['arm'] == arm), None)
                    text = (Path(row['folder']) / 'candidate.txt').read_text() if row else ''
                    lines.extend([f'### {side}', '', text or '[未执行或失败，无候选]', ''])
                    mapping[pair + '-' + side] = {'model': model, 'arm': arm, 'ok': bool(row and row['ok'])}
                    writer.writerow([pair, side, '', '', '', '', ''])
    (output / 'blind-review.md').write_text('\n'.join(lines) + '\n')
    write_json(output / 'blind-key.json', mapping)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before-source', type=Path)
    parser.add_argument('--after-source', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--environment', default='sandbox')
    parser.add_argument('--budget-cny', default='5')
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--worker-job', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker_job:
        if not args.run:
            raise ValueError('A worker also requires explicit --run.')
        run_worker(args.worker_job)
        return 0
    if not all((args.before_source, args.after_source, args.output)):
        raise ValueError('Both fixed sources and a new external output directory are required.')
    sources = {'before': source_identity(args.before_source), 'after': source_identity(args.after_source)}
    if sources['before']['sha256'] == sources['after']['sha256']:
        raise ValueError('Before and after application sources must differ.')
    output = args.output.expanduser().resolve()
    if output.exists() or any(output.is_relative_to(Path(s['root'])) for s in sources.values()):
        raise ValueError('Use a new external directory; automatic resume and replay are unavailable.')
    if any((p / '.literarygiant/story.sqlite3').exists() for p in output.parents):
        raise ValueError('Output cannot be inside a formal book.')
    budget = Budget(output, Decimal(args.budget_cny), Decimal(0))
    cases = [c for c in json.loads(CASES.read_text()) if c['kind'] == 'polish']
    jobs = [(profile, case, arm) for profile in MODELS for index, case in enumerate(cases)
            for arm in (('before', 'after') if index % 2 == 0 else ('after', 'before'))]
    plan = {'schema': 'lg.polish-source-comparison.v1', 'sources': sources, 'models': MODELS,
            'cases': cases, 'requests': len(jobs), 'maxOutputTokens': MAX_OUTPUT,
            'worstCatalogueReserveCNY': str(sum((worst_cost(MODELS[p]) for p, _, _ in jobs), Decimal(0))),
            'scriptSha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'comparison': 'Actual R5 in both arms; only explicitly fingerprinted product sources differ.',
            'transportDefaults': 'Same per-model profile/protocol; temperature omitted; no retry or paid judge.',
            'authorReview': 'pending', 'cashDebitVerified': False, 'noAdoption': True}
    write_json(output / 'plan.json', plan)
    if not args.run:
        print(json.dumps({'status': 'plan_only', 'callsSent': 0, 'output': str(output)}))
        return 0
    results, stopped = [], None
    try:
        for profile, case, arm in jobs:
            result = execute_source(case, arm, profile, sources[arm], output, args.environment, budget)
            results.append(result)
            write_json(output / 'results.json', results)
            print(json.dumps({'model': MODELS[profile], 'case': case['id'], 'arm': arm,
                'ok': result['ok'], 'budgetHeldCNY': str(budget.held())}), flush=True)
            if budget.pending:
                raise ValueError('Unknown request cost or transport; no further requests.')
    except Exception as exc:
        stopped = type(exc).__name__
    finally:
        write_review(output, results, cases)
        estimate = sum((known_cost(r['model'], r['usage']['input_tokens'], r['usage']['output_tokens'])
                        for r in budget.records if r.get('usage') and
                        all(type(r['usage'].get(k)) is int and r['usage'][k] >= 0
                            for k in ('input_tokens', 'output_tokens'))), Decimal(0))
        summary = {'status': 'stopped' if stopped else 'completed', 'stopType': stopped,
                   'requestIntents': len(budget.records), 'completedCandidates': sum(r['ok'] for r in results),
                   'budgetHeldCNY': str(budget.held()), 'knownUsageCatalogueEstimateCNY': str(estimate),
                   'cashDebitVerified': False, 'authorReview': 'pending', 'noAdoption': True}
        write_json(output / 'summary.json', summary)
        print(json.dumps(summary), flush=True)
    return 1 if stopped else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('Source comparison stopped: ' + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
