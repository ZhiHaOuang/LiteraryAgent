"""Candidate contract validation precedes manuscript activation."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lg_cli import book_analysis as analysis
from lg_cli import reference_contracts as references
from lg_cli.project_store import ProjectStore, ProjectStoreError
from lg_cli.supervision import DEFAULT_CATEGORIES, candidate_reports, configure_supervision
from tests.helpers import reference_card


class SupervisionContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.catalog_path = self.root / 'catalog.json'
        self.catalog_path.write_text(references._catalog_text())
        catalog_patch = patch.object(references, '_catalog_text', side_effect=self.catalog_path.read_text)
        catalog_patch.start()
        self.addCleanup(catalog_patch.stop)
        self.project = ProjectStore(self.root)
        self.project.initialize(name='Supervised contract fixture')
        self.document = self.project.create_document(kind='chapter', slug='one', title='One',
            content='The gate closes at dusk.')
        self.version = self.project.create_version('one', content='The gate closes at dusk. A guard watches.')
        configure_supervision(self.project)
        self.records = analysis.BookAnalysisStore(self.project)
        base = self.records.snapshot('one')
        snapshot = self.records.candidate_snapshot('one', self.version)
        response = {'summary': 'Gate rule checked', 'entries': [{
            'instance_card': reference_card('Worldview'), 'key': 'gate', 'label': 'Gate',
            'analysis': 'The gate closes at dusk.',
            'evidence': [{'span_id': next(iter(analysis.source_spans(snapshot)))}]}], 'findings': []}
        report = analysis.resolve_evidence(snapshot,
            references.finalize_cards(response, 'Worldview', analysis.source_spans(snapshot)))
        self.paths = []
        for category in DEFAULT_CATEGORIES:
            self.records.save(snapshot, category,
                report if category == 'Worldview' else {'summary': 'Checked', 'entries': [], 'findings': []},
                'synthetic-review', candidate_id=self.version.id, base_snapshot=base)
            self.paths.append(self.records._path('one', category, candidate_id=self.version.id))

    def rewrite_reports(self, transform):
        for path in self.paths:
            payload = json.loads(path.read_text())
            transform(payload)
            path.write_text(json.dumps(payload))

    def change_catalog(self, *, schema):
        value = json.loads(self.catalog_path.read_text())
        if schema:
            value['INSTANCE_CARD_PROMPT_SCHEMAS']['Worldview']['new_required_marker'] = 'text'
        else:
            value['libraries']['Worldview']['perspective'] += ' Updated review method.'
        self.catalog_path.write_text(json.dumps(value))

    def assert_rejected_without_activation(self):
        before = (self.project.get_document('one'), self.project.list_versions('one'))
        files = {path: path.read_bytes() for path in self.paths}
        with self.assertRaisesRegex(ProjectStoreError, 'Missing or stale .* candidate review'):
            self.project.accept_version('one', self.version.version_number)
        self.assertEqual((self.project.get_document('one'), self.project.list_versions('one')), before)
        self.assertEqual(self.project.get_version('one', self.version.version_number).state, 'candidate')
        self.assertEqual({path: path.read_bytes() for path in self.paths}, files)
        self.assertEqual(self.records.list(), [])

    def test_changed_card_schema_rejects_before_activation(self):
        self.change_catalog(schema=True)
        self.assert_rejected_without_activation()

    def test_changed_methods_with_identical_card_schema_rejects_old_fingerprint(self):
        before = references.card_schema('Worldview')
        self.change_catalog(schema=False)
        self.assertEqual(references.card_schema('Worldview'), before)
        self.assert_rejected_without_activation()

    def test_v1_without_fingerprint_still_enforces_current_category_schema(self):
        self.rewrite_reports(lambda payload: payload.pop('contract_fingerprint'))
        self.change_catalog(schema=True)
        self.assert_rejected_without_activation()

    def test_v1_without_fingerprint_keeps_valid_historical_reports_compatible(self):
        self.rewrite_reports(lambda payload: payload.pop('contract_fingerprint'))
        accepted = self.project.accept_version('one', self.version.version_number)
        self.assertEqual(accepted.active_version_id, self.version.id)
        self.assertEqual(len(self.records.list()), len(DEFAULT_CATEGORIES))

    def test_non_v1_reports_keep_base_schema_reading(self):
        def legacy(payload):
            payload.pop('contract')
            payload.pop('contract_fingerprint')
            for entry in payload['report']['entries']:
                entry.pop('instance_card')

        self.rewrite_reports(legacy)
        reports = candidate_reports(self.project, self.document, self.version)
        self.assertEqual(len(reports), len(DEFAULT_CATEGORIES))
        self.assertEqual(self.project.get_document('one').active_version_id, self.document.active_version_id)

    def test_current_contract_reports_allow_activation_and_publication(self):
        accepted = self.project.accept_version('one', self.version.version_number)
        self.assertEqual(accepted.active_version_id, self.version.id)
        self.assertEqual(accepted.content, self.version.content)
        self.assertEqual(len(self.records.list()), len(DEFAULT_CATEGORIES))
