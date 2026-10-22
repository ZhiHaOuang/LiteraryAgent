"""Source isolation and cross-process accounting must fail closed before paid work."""
import json
from decimal import Decimal
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.compare_literary_models import Budget, worst_cost, write_json
from scripts.compare_polish_sources import execute_source, load_cases, main, run_worker, source_identity, tool_identity
from tests import test_execution_adapter as fixtures


class SourceComparisonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.before = self.root / 'before'
        self.after = self.root / 'after'
        for source, text in ((self.before, 'old'), (self.after, 'new')):
            path = source / 'lg-cli/lg_cli/literary_tasks.py'
            path.parent.mkdir(parents=True)
            path.write_text(text)
        self.case = {'id': 'P', 'kind': 'polish', 'title': 'Synthetic'}

    def test_source_fingerprint_covers_schema_and_does_not_scan_books(self):
        first = source_identity(self.before)
        private = self.before / 'Projects/private.txt'
        private.parent.mkdir(); private.write_text('not application input')
        self.assertEqual(source_identity(self.before), first)
        schema = self.before / 'lg-cli/lg_cli/polish.schema.json'
        schema.write_text('{}')
        self.assertNotEqual(source_identity(self.before)['sha256'], first['sha256'])
        self.assertFalse(any(name.startswith('Projects/') for name in first['files']))

    def test_plan_only_never_reads_profiles_or_starts_workers(self):
        output = self.root / 'plan'
        args = ['--before-source', str(self.before), '--after-source', str(self.after), '--output', str(output)]
        with patch('scripts.compare_polish_sources.selected_config', side_effect=AssertionError('credentials read')):
            self.assertEqual(main(args), 0)
        self.assertEqual(json.loads((output / 'budget.json').read_text())['requests'], [])
        self.assertFalse((output / 'synthetic-books').exists())
        self.assertEqual(json.loads((output / 'plan.json').read_text())['requests'], 8)
        with self.assertRaises(ValueError):
            main(args + ['--run'])

    def test_worker_needs_explicit_run_and_matching_imported_source(self):
        with self.assertRaises(ValueError):
            main(['--worker-job', str(self.root / 'missing.json')])
        job = self.root / 'job.json'
        write_json(job, {'source': source_identity(self.before)})
        with self.assertRaisesRegex(ValueError, 'different application source'):
            run_worker(job, config_factory=lambda *_: self.fail('read config before source check'))

    def test_prospective_plan_selects_only_air_and_bounds_repetitions_without_credentials(self):
        cases = Path(__file__).resolve().parents[1] / 'scripts/literary_compliance_cases.json'
        output = self.root / 'prospective'
        args = ['--before-source', str(self.before), '--after-source', str(self.after),
                '--output', str(output), '--cases', str(cases), '--profile', 'zhipu-air', '--repeats', '2']
        with patch('scripts.compare_polish_sources.selected_config', side_effect=AssertionError('credentials read')):
            self.assertEqual(main(args), 0)
        plan = json.loads((output / 'plan.json').read_text())
        self.assertEqual(plan['models'], {'zhipu-air':'glm-4.5-air'})
        self.assertEqual(plan['requests'], 16)
        self.assertEqual(len(plan['cases']), 8)
        self.assertEqual(len(plan['authorRequestSha256']), 8)
        self.assertEqual(plan['toolIdentity'], tool_identity())
        self.assertEqual({c['repeat'] for c in plan['cases']}, {1, 2})
        self.assertTrue(Decimal(plan['worstCatalogueReserveCNY']) < Decimal('5'))
        with self.assertRaises(ValueError):
            main([*args[:args.index('--output')], '--output', str(self.root/'too-many'),
                  '--cases', str(cases), '--repeats', '2'])
        self.assertFalse((self.root/'too-many').exists())

    def test_case_validation_rejects_paths_duplicates_and_nonunique_selections(self):
        path = self.root / 'cases.json'
        base = {'id':'SAFE','kind':'polish','title':'Case','source':'前。选。后。','passage':'选。',
                'request':'润色。','constraints':[],'forbiddenExact':[]}
        for cases in ([{**base,'id':'../../outside'}], [base,base], [{**base,'source':'选。选。'}]):
            path.write_text(json.dumps(cases))
            with self.subTest(cases=cases), self.assertRaises(ValueError):
                load_cases(path, 1)

    def test_tool_change_stops_before_worker_or_credentials_and_keeps_budget_unused(self):
        changed = {**tool_identity(), 'sha256':'changed'}
        budget = Budget(self.root / 'tool-budget', Decimal('5'), Decimal(0))
        with patch('scripts.compare_polish_sources.subprocess.run', side_effect=AssertionError('launched')):
            with self.assertRaisesRegex(ValueError, 'tools changed after planning'):
                execute_source(self.case, 'before', 'zhipu-air', source_identity(self.before),
                    self.root / 'tool-output', 'sandbox', budget, expected_tools=changed)
        self.assertEqual(budget.records, [])
        job = self.root / 'tool-job.json'
        write_json(job, {'source':source_identity(Path(__file__).resolve().parents[1]), 'toolIdentity':changed})
        with self.assertRaisesRegex(ValueError, 'tools changed before execution'):
            run_worker(job, config_factory=lambda *_:self.fail('credentials read'))

    def test_uncertain_child_keeps_reservation_and_blocks_next_request(self):
        budget = Budget(self.root / 'budget', Decimal('5'), Decimal(0))
        def fail(*args, **kwargs):
            persisted = json.loads((self.root / 'budget/budget.json').read_text())
            self.assertEqual(persisted['requests'][0]['state'], 'intent_before_send')
            self.assertEqual(Decimal(persisted['heldCNY']), worst_cost('glm-4.5-air'))
            return SimpleNamespace(returncode=1)
        with self.assertRaisesRegex(ValueError, 'uncertain'):
            execute_source(self.case, 'before', 'zhipu-air', source_identity(self.before),
                           self.root / 'output', 'sandbox', budget, runner=fail)
        self.assertTrue(budget.pending)
        with self.assertRaises(ValueError):
            budget.begin('another', 'glm-4.5-air', {})

    def test_child_carries_prior_budget_and_rejection_keeps_full_reservation(self):
        budget = Budget(self.root / 'budget', Decimal('5'), Decimal(0))
        old = budget.begin('prior', 'glm-4.5-air', {})
        budget.finish(old, {'input_tokens': 100, 'output_tokens': 20}, transport_complete=True)
        carried = budget.held()
        def complete(command, **kwargs):
            job = json.loads(Path(command[command.index('--worker-job') + 1]).read_text())
            self.assertEqual(Decimal(job['carriedCNY']), carried)
            self.assertTrue(kwargs['env']['PYTHONPATH'].startswith(str(self.before.resolve() / 'lg-cli')))
            self.assertEqual(kwargs['cwd'], str(self.before.resolve()))
            write_json(Path(job['ledger']) / 'budget.json', {
                'initialProbeReserveCNY': job['carriedCNY'], 'requests': [{
                    'model': 'glm-4.5-air', 'label': 'glm-4.5-air/P/lg',
                    'state': 'host_schema_rejected', 'usage': {'input_tokens': 30, 'output_tokens': 40}}]})
            write_json(Path(job['folder']) / 'result.json', {
                'ok': False, 'activeManuscriptUnchanged': True, 'model': 'glm-4.5-air', 'case': 'P'})
            return SimpleNamespace(returncode=0)
        result = execute_source(self.case, 'before', 'zhipu-air', source_identity(self.before),
                                self.root / 'output', 'sandbox', budget, runner=complete)
        self.assertFalse(result['ok'])
        self.assertFalse(budget.pending)
        self.assertEqual(budget.held(), carried + worst_cost('glm-4.5-air'))
        self.assertEqual(budget.records[-1]['state'], 'host_schema_rejected')

    def test_budget_and_changed_source_block_worker_before_any_send(self):
        source = source_identity(self.before)
        budget = Budget(self.root / 'budget', Decimal('.1'), Decimal(0))
        with patch('scripts.compare_polish_sources.subprocess.run', side_effect=AssertionError('launched')):
            with self.assertRaises(ValueError):
                execute_source(self.case, 'before', 'zhipu-air', source, self.root / 'output', 'sandbox', budget)
        self.assertEqual(budget.records, [])
        (self.before / 'lg-cli/lg_cli/literary_tasks.py').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'changed after planning'):
            execute_source(self.case, 'after', 'zhipu-air', source, self.root / 'other', 'sandbox', budget)
        self.assertEqual(budget.records, [])

    def test_mismatched_child_usage_cannot_release_parent_reservation(self):
        budget = Budget(self.root / 'budget', Decimal('5'), Decimal(0))
        def mismatch(command, **kwargs):
            job = json.loads(Path(command[command.index('--worker-job') + 1]).read_text())
            write_json(Path(job['ledger']) / 'budget.json', {'initialProbeReserveCNY': '0', 'requests': [{
                'model': 'glm-4.6v', 'label': 'glm-4.6v/P/lg', 'state': 'completed',
                'usage': {'input_tokens': 1, 'output_tokens': 1}}]})
            return SimpleNamespace(returncode=0)
        with self.assertRaisesRegex(ValueError, 'accounting'):
            execute_source(self.case, 'before', 'zhipu-air', source_identity(self.before),
                           self.root / 'output', 'sandbox', budget, runner=mismatch)
        self.assertTrue(budget.pending)
        self.assertEqual(budget.held(), worst_cost('glm-4.5-air'))

    def test_real_children_load_each_source_and_settle_local_http_usage(self):
        application = Path(__file__).resolve().parents[1]
        for source, marker in ((self.before, 'synthetic-before'), (self.after, 'synthetic-after')):
            shutil.copytree(application / 'lg-cli', source / 'lg-cli', dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('__pycache__'))
            module = source / 'lg-cli/lg_cli/story_workflow.py'
            module.write_text(module.read_text() + '\nPOLISH_PROFILE = ' + repr(marker) + '\n')
        fixture = fixtures.NativeStageTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        code = '''
from dataclasses import replace
import sys
from scripts.compare_polish_sources import run_worker
from tests.helpers import make_config
def local_config(workspace, environment, profile):
    return replace(make_config(workspace), runtime_engine='native', protocol='responses',
        provider='responses-compatible', default_model='glm-4.5-air', writer_model='',
        critic_model='', coder_model='', base_url=sys.argv[2], max_output_tokens=4096)
run_worker(sys.argv[1], config_factory=local_config)
'''
        def local_child(command, **kwargs):
            job = command[command.index('--worker-job') + 1]
            return subprocess.run([sys.executable, '-B', '-c', code, job,
                f'http://127.0.0.1:{fixture.server.server_port}'], **kwargs)
        case = {'id':'LOCAL','kind':'polish','title':'Local source test','source':'前。选中。后。',
                'passage':'选中。','request':'只润色选中片段。','constraints':[],'forbiddenExact':[]}
        budget = Budget(self.root / 'real-budget', Decimal('5'), Decimal(0))
        for arm, source in (('before', self.before), ('after', self.after)):
            result = execute_source(case, arm, 'zhipu-air', source_identity(source),
                self.root / 'real-output', 'sandbox', budget, runner=local_child)
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['task']['inputProfile']['name'], 'synthetic-' + arm)
            self.assertTrue(result['activeManuscriptUnchanged'])
        self.assertEqual(len(fixture.requests), 2)
        self.assertEqual(len(budget.records), 2)
        self.assertFalse(budget.pending)
        self.assertEqual([r['usage']['input_tokens'] for r in budget.records], [10, 10])
