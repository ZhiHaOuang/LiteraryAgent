"""Analysis generation freezes trusted contracts; storage still enforces current resources."""
import copy
import json
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import jsonschema

from lg_cli import book_analysis as analysis
from lg_cli import reference_contracts as references
from lg_cli.core_adapter import CoreExecutionResult
from lg_cli.events import EventType
from lg_cli.project_store import ProjectStore, ProjectStoreError
from tests.helpers import make_config, reference_card


class AnalysisContractSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base_text = analysis.schema_path().read_text()
        self.catalog_text = references._catalog_text()
        self.base_path = self.root / 'base.schema.json'
        self.catalog_path = self.root / 'catalog.json'
        self.base_path.write_text(self.base_text)
        self.catalog_path.write_text(self.catalog_text)
        self.addCleanup(patch.stopall)
        patch.object(analysis, 'schema_path', return_value=self.base_path).start()
        patch.object(references, '_catalog_text', side_effect=lambda: self.catalog_path.read_text()).start()
        self.project = ProjectStore(self.root)
        self.project.initialize(name='Contract fixture')
        self.document = self.project.create_document(kind='chapter', slug='one', title='One',
            content='The gate closes at dusk.')
        self.records = analysis.BookAnalysisStore(self.project)
        self.snapshot = self.records.snapshot('one')
        self.response = {'summary': 'Observed gate rule', 'entries': [{
            'instance_card': reference_card('Worldview'), 'key': 'gate', 'label': 'Gate',
            'analysis': 'Access closes at dusk.',
            'evidence': [{'span_id': next(iter(analysis.source_spans(self.snapshot)))}]}], 'findings': []}
        self.requests, self.events = [], []
        self.generate = lambda number, kwargs: copy.deepcopy(self.response)
        outer = self

        class Adapter:
            def run(self, **kwargs):
                schema_text = kwargs['output_schema'].read_text()
                outer.requests.append({'schema': schema_text, 'prompt': kwargs['prompt']})
                response = outer.generate(len(outer.requests), kwargs)
                return CoreExecutionResult(True, False, json.dumps(response), None, 'offline', [], 0, '', '', 0)

        self.service = analysis.ChapterAnalysisService(
            replace(make_config(self.root), enable_reference=False, max_stage_context_chars=64000), adapter=Adapter())

    def change_catalog(self):
        value = json.loads(self.catalog_text)
        value['INSTANCE_CARD_PROMPT_SCHEMAS']['Worldview']['new_required_marker'] = 'text'
        self.catalog_path.write_text(json.dumps(value))

    def change_schema(self):
        value = json.loads(self.base_text)
        value['properties']['summary'] = {'type': 'string', 'const': 'B-only summary'}
        self.base_path.write_text(json.dumps(value))

    def stored_report(self):
        return analysis.resolve_evidence(self.snapshot,
            references.finalize_cards(self.response, 'Worldview', analysis.source_spans(self.snapshot)))

    def fail_analysis(self, *, calls, message=None):
        with self.assertRaises(ProjectStoreError) as caught:
            self.service.analyze('one', categories=['Worldview'], on_event=self.events.append)
        if message:
            self.assertIn(message, str(caught.exception))
        self.assertNotIn('PRIVATE_', str(caught.exception))
        self.assertEqual(len(self.requests), calls)
        self.assertEqual(self.events[-1].type, EventType.RUN_FAILED)
        manifest = self.service.runs.load(self.events[-1].run_id)
        self.assertEqual(manifest['status'], 'failed')
        self.assertEqual(manifest['completed_stages'], [])
        self.assertNotIn('PRIVATE_', manifest['error'])
        self.assertFalse(list(self.records.root.rglob('*.json')))
        return manifest

    def test_prepared_methods_response_card_and_stored_contracts_ignore_later_reads(self):
        contract = analysis._prepare_contracts(['Worldview'])['Worldview']
        original = (contract.methods, contract.response_schema_json, contract.stored_schema_json, contract.fingerprint)
        self.change_catalog()
        self.change_schema()
        self.assertEqual((contract.methods, contract.response_schema_json, contract.stored_schema_json,
                          contract.fingerprint), original)
        jsonschema.validate(self.response, json.loads(contract.response_schema_json))
        report = analysis.resolve_evidence(self.snapshot,
            contract.reference().finalize(self.response, analysis.source_spans(self.snapshot)))
        analysis._validate_report(self.snapshot, report, json.loads(contract.stored_schema_json), category='Worldview')
        with self.assertRaisesRegex(analysis.AnalysisContractError, 'resources changed'):
            contract.assert_current()

    def test_catalog_change_after_model_never_finalizes_or_repairs_with_b(self):
        def generate(*_):
            self.change_catalog()
            return copy.deepcopy(self.response)

        self.generate = generate
        with patch.object(references.ReferenceContract, 'finalize') as finalize:
            manifest = self.fail_analysis(calls=1, message='resources changed')
        finalize.assert_not_called()
        self.assertFalse(list(self.service.runs.handle(manifest['run_id']).directory.glob('*rejected*')))

    def test_report_schema_change_after_model_never_rejects_a_as_bad_model_output(self):
        def generate(*_):
            self.change_schema()
            return copy.deepcopy(self.response)

        self.generate = generate
        manifest = self.fail_analysis(calls=1, message='resources changed')
        self.assertFalse(list(self.service.runs.handle(manifest['run_id']).directory.glob('*rejected*')))

    def test_change_only_at_storage_is_rejected_before_any_report_write(self):
        original = self.service.records.save

        def save(*args, **kwargs):
            self.change_schema()
            return original(*args, **kwargs)

        with patch.object(self.service.records, 'save', side_effect=save), patch.object(analysis, '_atomic_write') as write:
            self.fail_analysis(calls=1, message='resources changed')
        write.assert_not_called()

    def test_resource_changes_after_prompt_save_and_during_storage_preflight_are_rechecked(self):
        original_prompt = self.service.runs.write_stage_prompt

        def prompt(*args, **kwargs):
            value = original_prompt(*args, **kwargs)
            self.change_catalog()
            return value

        with patch.object(self.service.runs, 'write_stage_prompt', side_effect=prompt):
            self.fail_analysis(calls=0, message='resources changed')
        self.catalog_path.write_text(self.catalog_text)
        report = self.stored_report()
        contract = analysis._prepare_contracts(['Worldview'])['Worldview']
        original_current = self.records.current_snapshot

        def current(snapshot):
            value = original_current(snapshot)
            self.change_schema()
            return value

        with patch.object(self.records, 'current_snapshot', side_effect=current), \
                patch.object(analysis, '_atomic_write') as write:
            with self.assertRaisesRegex(analysis.AnalysisContractError, 'resources changed'):
                self.records.save(self.snapshot, 'Worldview', report, 'late-change',
                    expected_contract_fingerprint=contract.fingerprint)
        write.assert_not_called()
        self.assertFalse(list(self.records.root.rglob('*.json')))

    def test_response_schema_file_changes_before_or_during_model_fail_closed(self):
        for when in ('before', 'during'):
            with self.subTest(when=when):
                self.requests.clear()

                def mutate(path):
                    value = json.loads(path.read_text())
                    value['description'] = 'B accepts the same response, but is a different request contract.'
                    path.write_text(json.dumps(value))

                def generate(number, kwargs):
                    if when == 'during': mutate(kwargs['output_schema'])
                    return copy.deepcopy(self.response)

                self.generate = generate
                original = self.service.runs.write_stage_prompt

                def write_prompt(handle, **kwargs):
                    value = original(handle, **kwargs)
                    if when == 'before': mutate(handle.directory / 'Worldview.schema.json')
                    return value

                with patch.object(self.service.runs, 'write_stage_prompt', side_effect=write_prompt):
                    self.fail_analysis(calls=0 if when == 'before' else 1, message='snapshot changed')

    def test_default_storage_and_none_expected_keep_current_host_validation(self):
        report = self.stored_report()
        payload = self.records.save(self.snapshot, 'Worldview', report, 'default')
        explicit_none = self.records.save(self.snapshot, 'Worldview', report, 'none', expected_contract_fingerprint=None)
        self.assertEqual(payload['fingerprint'], analysis.fingerprint(self.snapshot))
        self.assertEqual(payload['contract_fingerprint'], explicit_none['contract_fingerprint'])
        invalid = copy.deepcopy(report)
        invalid['entries'][0].pop('instance_card')
        with self.assertRaises(jsonschema.ValidationError):
            self.records.save(self.snapshot, 'Worldview', invalid, 'invalid', expected_contract_fingerprint=None)

    def test_wrong_expected_fingerprint_cannot_write_or_supply_a_relaxed_schema(self):
        with patch.object(analysis, '_atomic_write') as write:
            with self.assertRaisesRegex(analysis.AnalysisContractError, 'resources changed'):
                self.records.save(self.snapshot, 'Worldview', self.stored_report(), 'wrong',
                    expected_contract_fingerprint='0' * 64)
        write.assert_not_called()
        self.assertFalse(self.records.root.exists())
        with self.assertRaises(TypeError):
            self.records.save(self.snapshot, 'Worldview', {}, 'relaxed', schema=True)

    def test_public_report_validation_binds_expected_contract_without_changing_none_or_legacy(self):
        report = self.stored_report()
        contract = analysis._prepare_contracts(['Worldview'])['Worldview']
        analysis.validate_report(self.snapshot, report, category='Worldview',
                                 expected_contract_fingerprint=contract.fingerprint)
        analysis.validate_report(self.snapshot, report, category='Worldview', expected_contract_fingerprint=None)
        with self.assertRaisesRegex(analysis.AnalysisContractError, 'resources changed'):
            analysis.validate_report(self.snapshot, report, category='Worldview',
                                     expected_contract_fingerprint='0' * 64)
        with self.assertRaisesRegex(analysis.AnalysisContractError, 'requires a category'):
            analysis.validate_report(self.snapshot, report, expected_contract_fingerprint=contract.fingerprint)
        legacy = copy.deepcopy(report)
        legacy['entries'][0].pop('instance_card')
        analysis.validate_report(self.snapshot, legacy, expected_contract_fingerprint=None)
        self.change_catalog()
        with self.assertRaisesRegex(analysis.AnalysisContractError, 'resources changed'):
            analysis.validate_report(self.snapshot, report, category='Worldview',
                                     expected_contract_fingerprint=contract.fingerprint)

    def test_public_bound_report_validation_rechecks_resources_after_validation(self):
        report = self.stored_report()
        contract = analysis._prepare_contracts(['Worldview'])['Worldview']
        original = analysis._validate_report

        def validate(*args, **kwargs):
            original(*args, **kwargs)
            self.change_catalog()

        with patch.object(analysis, '_validate_report', side_effect=validate):
            with self.assertRaisesRegex(analysis.AnalysisContractError, 'resources changed'):
                analysis.validate_report(self.snapshot, report, category='Worldview',
                                         expected_contract_fingerprint=contract.fingerprint)

    def test_source_change_still_rejects_frozen_generation(self):
        def generate(*_):
            self.project.create_version('one', content='The gate remains open all night.', activate=True)
            return copy.deepcopy(self.response)

        self.generate = generate
        self.fail_analysis(calls=1, message='Chapter or Canonical memory changed')

    def test_one_evidence_repair_reuses_contract_and_archives_exact_sources(self):
        def generate(number, kwargs):
            response = copy.deepcopy(self.response)
            if number == 1: response['entries'][0]['evidence'][0]['span_id'] = 'invented-span'
            return response

        self.generate = generate
        result = self.service.analyze('one', categories=['Worldview'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0]['schema'], self.requests[1]['schema'])
        directory = self.service.runs.handle(result['run_id']).directory
        archive = json.loads((directory / 'analysis-contract.json').read_text())['categories']['Worldview']
        self.assertEqual(archive['base_schema_text'], self.base_text)
        self.assertEqual(archive['catalog_text'], self.catalog_text)
        self.assertEqual(archive['response_schema_json'], self.requests[0]['schema'])
        self.assertIn(archive['methods'], self.requests[0]['prompt'])
        self.assertIn(archive['methods'], self.requests[1]['prompt'])
        record = self.records.list()[0]
        self.assertEqual(record['contract_fingerprint'], archive['fingerprint'])
        self.assertEqual(record['fingerprint'], analysis.fingerprint(self.snapshot))
        for name in ('analysis-contract.json', 'Worldview.schema.json', 'Worldview-rejected-1.json'):
            self.assertEqual(stat.S_IMODE((directory / name).stat().st_mode), 0o600)

    def test_schema_validation_errors_never_publish_instance_or_schema_dump(self):
        def generate(*_):
            response = copy.deepcopy(self.response)
            response['summary'] = {'secret': 'PRIVATE_INVALID_SUMMARY'}
            return response

        self.generate = generate
        manifest = self.fail_analysis(calls=2, message='Specialist response failed schema validation')
        self.assertNotIn('PRIVATE_', json.dumps([event.to_dict() for event in self.events]))
        repair = self.requests[1]['prompt'].split('Previous output was rejected: ', 1)[1]
        reason = repair.split('\nRebuild the report', 1)[0]
        self.assertEqual(reason, 'Specialist response failed schema validation.')
        directory = self.service.runs.handle(manifest['run_id']).directory
        self.assertIn('PRIVATE_INVALID_SUMMARY', (directory / 'Worldview-rejected-2.json').read_text())

    def test_unknown_span_and_adapter_exception_never_publish_model_controlled_errors(self):
        for operation, calls, error in (
            ('unknown-span', 2, 'Analysis evidence does not match the supplied source.'),
            ('adapter-exception', 1, 'Specialist model execution failed.'),
        ):
            with self.subTest(operation=operation):
                self.requests.clear()
                self.events.clear()

                def generate(*_):
                    if operation == 'adapter-exception':
                        raise RuntimeError('PRIVATE_ADAPTER_BODY')
                    response = copy.deepcopy(self.response)
                    response['entries'][0]['evidence'][0]['span_id'] = 'PRIVATE_UNKNOWN_SPAN'
                    return response

                self.generate = generate
                manifest = self.fail_analysis(calls=calls, message=error)
                self.assertEqual(manifest['error'], error)
                self.assertNotIn('PRIVATE_', json.dumps([event.to_dict() for event in self.events]))

    def test_snapshot_and_report_save_failures_are_private_and_not_repaired(self):
        for operation, calls in (('contract', 0), ('response', 0), ('report', 1)):
            with self.subTest(operation=operation):
                self.requests.clear()
                original = analysis._atomic_private_text

                def write_private(path, text):
                    if (operation == 'contract' and path.name == 'analysis-contract.json'
                        or operation == 'response' and path.name == 'Worldview.schema.json'):
                        raise OSError('PRIVATE_SAVE_ERROR')
                    return original(path, text)

                with patch.object(analysis, '_atomic_private_text', side_effect=write_private):
                    if operation == 'report':
                        with patch.object(analysis, '_atomic_write', side_effect=OSError('PRIVATE_SAVE_ERROR')):
                            self.fail_analysis(calls=calls)
                    else:
                        self.fail_analysis(calls=calls)

    def test_old_report_reads_still_apply_current_contract_and_legacy_mode(self):
        payload = self.records.save(self.snapshot, 'Worldview', self.stored_report(), 'old')
        self.change_catalog()
        with self.assertRaisesRegex(ProjectStoreError, 'Invalid analysis record'):
            self.records.list()
        payload.pop('contract')
        payload['report']['entries'][0].pop('instance_card')
        path = self.records.root / 'one' / 'Worldview.json'
        path.write_text(json.dumps(payload))
        before = path.read_bytes()
        self.assertEqual(self.records.list()[0]['status'], 'current')
        self.assertEqual(path.read_bytes(), before)
