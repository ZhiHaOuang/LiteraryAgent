#!/usr/bin/env python3
"""Bounded live comparison through LG's actual R5 and Native stage paths.

Default is a plan only. --run explicitly spends from the specified, capped budget.
All books are new synthetic fixtures. No adoption, automatic retry or paid judge.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import time

from lg_cli.anthropic_bridge import SchemaOutputError, translate_request
from lg_cli.config import load_config
from lg_cli.credentials import read_profiles
from lg_cli.execution_adapter import NativeStageExecutor
from lg_cli.literary_tasks import start
from lg_cli.native_model import NativeModel
from lg_cli.project_store import ProjectStore

CASES = Path(__file__).with_name('literary_quality_cases.json')
MODELS = {'zhipu-air': 'glm-4.5-air', 'zhipu-vision': 'glm-4.6v'}
RATES = {'glm-4.5-air': (Decimal('1.2'), Decimal('8')),
         'glm-4.6v': (Decimal('2'), Decimal('6'))}
MAX_INPUT = 131072  # Published 128K context, conservatively counted as input only.
MAX_OUTPUT = 4096
MAX_WIRE_BYTES = 64000
MAX_REQUESTS = 16
PRICE_SOURCE = 'https://docs.bigmodel.cn/cn/guide/start/pricing'
AUTHOR_INPUT_CONTRACT = 'lg.author-request.v1'


def author_request(case):
    """Share R5's outer-whitespace normalization; preserve the actual requirements."""
    request, constraints = case['request'], case.get('constraints', [])
    if not isinstance(request, str) or not request.strip():
        raise ValueError('A nonempty author request is required.')
    if not isinstance(constraints, list) or any(not isinstance(c, str) or not c.strip() for c in constraints):
        raise ValueError('Author constraints must be a list of nonempty strings.')
    request = request.strip()
    if not constraints:
        return request
    return request + '\n\n作者补充约束：\n' + '\n'.join('- ' + c.strip() for c in constraints)


def write_json(path: Path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def safe_response(response):
    """Never persist provider private reasoning, encrypted content or arbitrary metadata."""
    value = {key: response.get(key) for key in ('id', 'model', 'status', 'usage')}
    value['output'] = [{'type': 'message', 'role': item.get('role'),
                        'content': [{'type': 'output_text', 'text': part['text']}
                                    for part in item.get('content', []) if part.get('type') == 'output_text']}
                       for item in response.get('output', []) if item.get('type') == 'message']
    return value


def worst_cost(model: str, output=MAX_OUTPUT) -> Decimal:
    pin, pout = RATES[model]
    return (MAX_INPUT * pin + output * pout) / Decimal(1000000)


def known_cost(model: str, inputs: int, outputs: int, *, upper=False) -> Decimal:
    if any(type(v) is not int or v < 0 for v in (inputs, outputs)):
        raise ValueError('Provider token counts are absent or invalid.')
    if upper:
        pin, pout = RATES[model]
    elif model == 'glm-4.5-air':
        pin, pout = (Decimal('1.2'), Decimal('8')) if inputs >= 32000 else (Decimal('.8'), Decimal('2' if outputs < 200 else '6'))
    else:
        pin, pout = (Decimal('2'), Decimal('6')) if inputs >= 32000 else (Decimal('1'), Decimal('3'))
    return (inputs * pin + outputs * pout) / Decimal(1000000)


class Budget:
    def __init__(self, output: Path, limit: Decimal, initial_reserve: Decimal):
        if not limit.is_finite() or not Decimal('0') < limit <= Decimal('5'):
            raise ValueError('This round permits a positive total budget no greater than CNY 5.')
        if not initial_reserve.is_finite() or not Decimal('0') <= initial_reserve < limit:
            raise ValueError('Invalid already-paid probe reserve.')
        self.output, self.limit, self.initial = output, limit, initial_reserve
        self.records = []
        self.pending = False
        self.save()

    def held(self):
        return self.initial + sum((Decimal(item['heldCNY']) for item in self.records), Decimal(0))

    def save(self):
        write_json(self.output / 'budget.json', {
            'schema': 'lg.quality-budget.v1', 'limitCNY': str(self.limit),
            'initialProbeReserveCNY': str(self.initial), 'heldCNY': str(self.held()),
            'priceSource': PRICE_SOURCE, 'priceDate': '2026-10-22',
            'discountAssumed': False, 'actualAccountDebitCNY': None,
            'unknownCostStopsFurtherRequests': self.pending, 'requests': self.records})

    def begin(self, label, model, wire):
        if self.pending:
            raise ValueError('A prior request has uncertain cost or incomplete transport; no replay.')
        if len(self.records) >= MAX_REQUESTS:
            raise ValueError('This experiment permits at most 16 comparison requests.')
        if len(json.dumps(wire, ensure_ascii=False).encode()) > MAX_WIRE_BYTES:
            raise ValueError('Prepared wire payload exceeds this experiment input limit.')
        reserve = worst_cost(model)
        if self.held() + reserve > self.limit:
            raise ValueError('Insufficient remaining budget for the full conservative reservation.')
        self.pending = True
        record = {'index': len(self.records) + 1, 'label': label, 'model': model,
                  'startedUTC': datetime.now(timezone.utc).isoformat(), 'state': 'intent_before_send',
                  'maxOutputTokens': MAX_OUTPUT, 'inputReservationTokens': MAX_INPUT,
                  'heldCNY': str(reserve), 'usage': None, 'catalogueEstimateCNY': None}
        self.records.append(record)
        self.save()
        return record

    def finish(self, record, usage, *, transport_complete, host_schema_rejected=False):
        valid = bool(usage) and all(type(usage.get(k)) is int and usage[k] >= 0 for k in ('input_tokens', 'output_tokens'))
        valid = valid and usage['input_tokens'] <= MAX_INPUT and usage['output_tokens'] <= MAX_OUTPUT
        record['usage'] = usage
        record['state'] = 'completed' if transport_complete and valid else 'uncertain_stop'
        if host_schema_rejected and valid:
            # A Responses final event reached local schema validation. Preserve the
            # entire reservation, reject the candidate, and permit other distinct jobs.
            record['state'] = 'host_schema_rejected'
            record['fullReservationRetained'] = True
            self.pending = False
        if transport_complete and valid:
            record['heldCNY'] = str(known_cost(record['model'], usage['input_tokens'], usage['output_tokens'], upper=True))
            record['catalogueEstimateCNY'] = str(known_cost(record['model'], usage['input_tokens'], usage['output_tokens']))
            self.pending = False
        self.save()


class ObservedModel:
    def __init__(self, config, budget, folder, label):
        self.config, self.budget, self.folder, self.label = config, budget, folder, label

    async def generate(self, request, delta):
        if self.config.uses_anthropic:
            wire, _ = translate_request(request, self.config.max_output_tokens)
        else:
            wire = {**request, 'store': False, 'max_output_tokens': self.config.max_output_tokens}
        write_json(self.folder / 'model-input.json', {'protocol': self.config.protocol, 'baseURL': self.config.base_url,
                                                    'payload': wire, 'headersOmitted': True})
        record = self.budget.begin(self.label, request['model'], wire)
        response, started, host_rejection = None, time.monotonic(), False
        try:
            response = await NativeModel(self.config).generate(request, delta)
            write_json(self.folder / 'model-output.json', safe_response(response))
            return response
        except SchemaOutputError:
            host_rejection = self.config.uses_responses
            raise
        finally:
            rows = []
            database = self.config.workspace / '.literarygiant/usage.sqlite3'
            if database.exists():
                with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as db:
                    db.row_factory = sqlite3.Row
                    rows = [dict(row) for row in db.execute('SELECT input_tokens,output_tokens,partial FROM usage ORDER BY rowid DESC LIMIT 1')]
            reported = rows[0] if rows else {}
            complete = response is not None and response.get('status') == 'completed' and reported.get('partial') == 0
            usage = (response.get('usage') or {}) if complete else reported
            # The ledger's presence check avoids treating translator-default zeros as reported usage.
            complete = complete and all(type(reported.get(k)) is int for k in ('input_tokens', 'output_tokens'))
            record['seconds'] = round(time.monotonic() - started, 3)
            record['providerResponseId'] = response.get('id') if response else None
            record['returnedModel'] = response.get('model') if response else None
            reported_model = response.get('model') if response else None
            model_match = reported_model is None or (isinstance(reported_model, str) and reported_model.lower() == request['model'].lower())
            record['returnedModelStatus'] = 'not_exposed' if reported_model is None else ('matches' if model_match else 'mismatch')
            complete = complete and model_match
            self.budget.finish(record, usage, transport_complete=complete, host_schema_rejected=host_rejection)


def reviewed_pending_jobs(path, cases):
    """Explicit inspected continuation; never infer that an interrupted call is replayable."""
    manifest = json.loads(path.read_text())
    previous = Path(manifest['previousOutput']).resolve()
    raw = (previous / 'budget.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest['previousBudgetSha256']:
        raise ValueError('Previous budget changed after review.')
    previous_plan = json.loads((previous / 'plan.json').read_text())
    if previous_plan['cases'] != cases or previous_plan['models'] != MODELS or previous_plan['maxOutputTokens'] != MAX_OUTPUT:
        raise ValueError('Cases, model identity or output budget changed after the original run.')
    budget = json.loads(raw)
    sent = {r['label'] for r in budget['requests']}
    all_jobs = {(profile, c['id'], arm) for profile in MODELS for c in cases for arm in ('lg', 'baseline')}
    pending = {(profile, case, arm) for profile, case, arm in all_jobs if MODELS[profile]+'/'+case+'/'+arm not in sent}
    proposed = [tuple(value) for value in manifest['jobs']]
    if len(proposed) != len(set(proposed)) or set(proposed) != pending:
        raise ValueError('Reviewed jobs must be exactly the unsent jobs; no duplicate, retry or omission.')
    if Decimal(manifest['carriedReserveCNY']) != Decimal(budget['heldCNY']):
        raise ValueError('The complete prior budget reservation must be carried forward.')
    if manifest.get('neverReplaySentRequests') is not True:
        raise ValueError('Continuation must explicitly preserve all prior request outcomes.')
    return proposed, Decimal(manifest['carriedReserveCNY']), manifest


def metrics(case, text):
    value = {'chars': len(text), 'forbiddenLiteralHits': [s for s in case['forbiddenExact'] if s in text],
             'semanticQualityAssessed': False, 'literalChecksAreNotEntailmentTests': True}
    if case['kind'] == 'polish':
        before, after = case['source'].split(case['passage'])
        preserved = text.startswith(before) and text.endswith(after) and len(text) >= len(before) + len(after)
        value['outsideSelectionPreserved'] = preserved
        value['originalPassageChars'] = len(case['passage'])
        value['replacementChars'] = len(text) - len(before) - len(after) if preserved else None
    return value


def selected_config(workspace, environment, profile_name):
    profile = read_profiles(environment)['profiles'][profile_name]
    if profile['model'] != MODELS[profile_name]:
        raise ValueError('The selected profile model differs from the author-selected experiment model.')
    permitted = {'glm': ('anthropic', 'https://open.bigmodel.cn/api/anthropic'),
                 'responses-compatible': ('responses', 'https://open.bigmodel.cn/api/v1')}
    if (profile['protocol'], profile['base_url']) != permitted.get(profile['provider']):
        raise ValueError('Profile is not bound to the explicitly selected official Zhipu endpoint.')
    return replace(load_config(workspace, environment=environment), provider=profile['provider'],
                   protocol=profile['protocol'], base_url=profile['base_url'], default_model=profile['model'],
                   writer_model='', coder_model='', critic_model='', api_key=profile['key'],
                   api_key_source='profile:' + environment + '/' + profile_name, profile_name=profile_name,
                   max_output_tokens=MAX_OUTPUT, timeout_seconds=150, runtime_engine='native')


def execute_case(case, arm, folder, config, budget):
    request = author_request(case)
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    workspace = config.workspace
    workspace.mkdir(mode=0o700, parents=True, exist_ok=False)
    store = ProjectStore(workspace)
    store.initialize(name='SYNTHETIC QUALITY EXPERIMENT ONLY')
    original = store.create_document(kind='chapter', slug='sample', title=case['title'],
                                     content=case['source'], state='accepted', sequence=1)
    label = config.default_model + '/' + case['id'] + '/' + arm
    first_record = len(budget.records)
    stage_results = []
    class RecordedStage(NativeStageExecutor):
        def run(self, **kwargs):
            result = super().run(**kwargs)
            stage_results.append(result.ok)
            return result
    adapter = RecordedStage(model_factory=lambda cfg: ObservedModel(cfg, budget, folder, label))
    started = time.monotonic()
    if arm == 'lg':
        options = {'strategy': case['kind'], 'document': 'sample', 'request': request,
                   'maxModelCalls': 1, 'maxInputChars': 24000}
        if case['kind'] == 'polish':
            options['passage'] = case['passage']
        result = start(config, options, adapter=adapter)
        ok = result['status'] == 'awaiting_review'
        text = store.get_version('sample', result['candidate']['versionNumber']).content if ok else ''
        task = result
    else:
        packet = {'source': case['source'], 'request': request}
        if case['kind'] == 'polish':
            packet['selected_passage'] = case['passage']
            instruction = '你是一位小说编辑。按作者要求只润色指定片段，其余文字逐字保持。返回修改后的完整正文，不解释、不使用代码围栏。'
        else:
            instruction = '你是一位小说作者。遵守给定事实、人物视角、保密范围与已定方向，完成作者请求。只返回候选正文，不解释，不宣称正式采用。'
        result = adapter.run(prompt=json.dumps(packet, ensure_ascii=False), instructions=instruction,
                             config=config, mode='write', model_profile='writer')
        ok, text = result.ok, result.output_text if result.ok else ''
        task = {'status': 'candidate_only' if ok else 'failed', 'error': result.error}
    active_unchanged = store.get_document('sample').active_version_id == original.active_version_id
    if not active_unchanged:
        raise ValueError('Experiment must never adopt or replace the initial manuscript.')
    contract_passed = True if stage_results and stage_results[-1] else None
    if any(r['label'] == label and r['state'] == 'host_schema_rejected' for r in budget.records[first_record:]):
        contract_passed = False
    (folder / 'candidate.txt').write_text(text)
    value = {'case': case['id'], 'arm': arm, 'model': config.default_model, 'protocol': config.protocol,
             'ok': ok, 'seconds': round(time.monotonic() - started, 3), 'activeManuscriptUnchanged': active_unchanged,
             'contractPassed': contract_passed, 'candidateAvailable': ok,
             'taskCompliance': 'pending_review' if ok else 'unavailable', 'authorUtility': 'pending',
             'authorInput': {'schema': AUTHOR_INPUT_CONTRACT, 'sha256': hashlib.sha256(request.encode()).hexdigest()},
             'task': task, 'metrics': metrics(case, text) if ok else None,
             'interventionsDuringGeneration': {'authorChoices': 0, 'clarifications': 0, 'adoptions': 0,
                                               'manualEdits': None, 'humanReviewMinutes': None, 'reviewStatus': 'pending'}}
    write_json(folder / 'result.json', value)
    return value


def write_review(output, results, cases):
    rng = random.Random(20261022)
    mapping, parts = {}, ['# 首轮匿名评审材料', '', '模型内部两组配对；A/B顺序随机。暂不显示组别。失败也保留，未人工评分。', '']
    with (output / 'author-review.csv').open('w', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['pair', 'side', 'causality_1to5', 'character_1to5', 'voice_1to5', 'task_1to5', 'manual_edit_chars', 'author_minutes', 'evidence'])
        for profile, model in MODELS.items():
            for case in cases:
                pair = f'M{list(MODELS).index(profile) + 1}-{case["id"]}'
                arms = ['lg', 'baseline'];rng.shuffle(arms)
                parts.extend([f'## {pair}：{case["title"]}', '', author_request(case), ''])
                for side, arm in zip(('A', 'B'), arms):
                    record = next((r for r in results if r['model'] == model and r['case'] == case['id'] and r['arm'] == arm), None)
                    folder = output / model / case['id'] / arm
                    text = (folder / 'candidate.txt').read_text() if (folder / 'candidate.txt').exists() else '[未执行或失败]'
                    parts.extend([f'### {side}', '', text or '[执行失败，无候选正文]', ''])
                    mapping[pair + '-' + side] = {'model': model, 'arm': arm, 'completed': bool(record and record['ok'])}
                    writer.writerow([pair, side, '', '', '', '', '', '', ''])
    (output / 'blind-review.md').write_text('\n'.join(parts) + '\n')
    write_json(output / 'blind-key.json', mapping)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='A new directory outside source and formal books.')
    parser.add_argument('--environment', default='sandbox')
    parser.add_argument('--budget-cny', default='5')
    parser.add_argument('--initial-probe-reserve-cny', default='.74', help='Hold the full worst-case reservation for all three documented setup probes.')
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--reviewed-pending-jobs', type=Path, help='Explicit reviewed manifest of only unsent jobs, carrying the prior reservation.')
    args = parser.parse_args(argv)
    cases = json.loads(CASES.read_text())
    output = args.output.expanduser().resolve()
    if output.exists() or output.is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError('Use a new external experiment directory; no resume or replay is automatic.')
    if any((parent / '.literarygiant/story.sqlite3').exists() for parent in output.parents):
        raise ValueError('Experiment output must not be inside an existing formal book.')
    output.mkdir(mode=0o700, parents=True)
    jobs = [(profile, case['id'], arm) for profile in MODELS for index, case in enumerate(cases)
            for arm in (('lg', 'baseline') if index % 2 == 0 else ('baseline', 'lg'))]
    reserve, continuation = Decimal(args.initial_probe_reserve_cny), None
    if args.reviewed_pending_jobs:
        jobs, reserve, continuation = reviewed_pending_jobs(args.reviewed_pending_jobs, cases)
    budget = Budget(output, Decimal(args.budget_cny), reserve)
    plan = {'schema': 'lg.same-model-quality.v1', 'models': MODELS, 'cases': cases, 'comparisonCalls': 16,
            'authorInputContract': AUTHOR_INPUT_CONTRACT,
            'authorRequestSha256': {c['id']: hashlib.sha256(author_request(c).encode()).hexdigest() for c in cases},
            'maxOutputTokens': MAX_OUTPUT, 'maxWireBytes': MAX_WIRE_BYTES,
            'temperature': 'omitted in both arms; provider default effective value unknown',
            'reasoning': 'provider/protocol default identical within each model; not a cross-model controlled comparison',
            'cashDebitVerified': False, 'resourcePackReportedByAuthor': {'glm-4.5-air': 12000000, 'glm-4.6v': 5769930},
            'literaryHumanReview': 'pending', 'formalBooksReadOrWritten': False,
            'baseline': 'simple direct prompt through the same Native stage transport; not a complete coding Agent',
            'limit': 'LG uses structured stages and exact selection assembly; baseline follows plain-text instructions. Both differences are reported.'}
    identity = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    plan['sourceIdentity'] = {'commit': identity.stdout.strip() if identity.returncode == 0 else None,
                              'scriptSha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                              'casesSha256': hashlib.sha256(CASES.read_bytes()).hexdigest(),
                              'python': sys.executable}
    plan['comparisonCalls'] = len(jobs)
    plan['reviewedContinuation'] = continuation
    write_json(output / 'plan.json', plan)
    if not args.run:
        print(json.dumps({'status': 'plan_only', 'callsSent': 0, 'output': str(output)}))
        return 0
    results, stopped = [], None
    try:
        # Serial requests let every provider usage settle before the next reservation.
        for profile, case_id, arm in jobs:
            model = MODELS[profile]
            case = next(c for c in cases if c['id'] == case_id)
            folder = output / model / case['id'] / arm
            workspace = output / 'synthetic-books' / model / case['id'] / arm
            config = selected_config(workspace, args.environment, profile)
            value = execute_case(case, arm, folder, config, budget)
            results.append(value)
            write_json(output / 'results.json', results)
            print(json.dumps({'model': model, 'case': case['id'], 'arm': arm, 'ok': value['ok'],
                              'seconds': value['seconds'], 'budgetHeldCNY': str(budget.held())}), flush=True)
            if budget.pending:
                raise ValueError('Uncertain provider completion or usage; stopped without replay.')
    except Exception as exc:
        stopped = type(exc).__name__
    finally:
        write_review(output, results, cases)
        summary = {'status': 'stopped' if stopped else 'completed', 'stopType': stopped,
                   'completedOutputs': sum(r['ok'] for r in results), 'comparisonRequestsSent': len(budget.records),
                   'budgetHeldCNY': str(budget.held()), 'cashDebitVerified': False,
                   'authorReview': 'pending', 'noAdoption': True, 'noPrivateReasoningPersisted': True}
        write_json(output / 'summary.json', summary)
        print(json.dumps(summary), flush=True)
    return 1 if stopped else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('Comparison stopped: ' + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
