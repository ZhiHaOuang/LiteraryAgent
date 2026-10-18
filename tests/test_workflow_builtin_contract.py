"""Strict builtin schemas permit prose overviews and empty values for other stages."""
import json
import unittest
from importlib import resources
from unittest.mock import patch

import jsonschema

from lg_cli.run_store import RunStore
from lg_cli.workflow_runner import WorkflowRunner
from tests import test_execution_adapter as fixtures


OVERVIEW = '维修师在停摆的浮城中追查供能失衡的真相。'


class BuiltinWorkflowContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.NativeStageTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runner = WorkflowRunner(self.fixture.config)

    def test_builtin_write_final_accepts_the_overview_required_by_the_runner(self):
        stages = self.runner._workflow('write')[1]['stages']
        final = stages[-1]
        self.assertEqual(final['agent'], 'director')
        agent = self.runner.registry.agent(final['agent'])
        self.assertEqual(agent.source, 'builtin')
        schema = json.loads(agent.output_schema.read_text())
        bundled = json.loads(resources.files('lg_cli.resources').joinpath('stage-output.schema.json').read_text())
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(schema['properties']['book_overview'], bundled['properties']['book_overview'])
        self.assertIn('book_overview', schema['required'])
        payload = fixtures.specimen(schema)
        payload['book_overview'] = OVERVIEW
        jsonschema.validate(payload, schema)

    def test_non_prose_workflow_accepts_empty_overview_without_saving_one(self):
        original = fixtures.specimen

        def response(schema):
            payload = original(schema)
            if (isinstance(payload, dict) and 'artifact_markdown' in schema.get('properties', {})
                and 'book_overview' in schema['properties']):
                payload['book_overview'] = ''
            return payload

        with patch.object(fixtures, 'specimen', response):
            result = self.runner.run('world', '只规划世界规则，不生成正文。')
        self.assertTrue(result.ok, result.error)
        runs = RunStore(self.fixture.root)
        stages = runs.load(result.run_id)['completed_stages']
        self.assertEqual(len(stages), 4)
        self.assertEqual(len(self.fixture.requests), len(stages))
        for stage, request in zip(stages, self.fixture.requests):
            metadata = runs.read_stage_metadata(result.run_id, stage)
            payload = json.loads(metadata['response_text'])
            schema = request['text']['format']['schema']
            if 'book_overview' in schema.get('properties', {}):
                self.assertEqual(payload['book_overview'], '')
            else:
                self.assertNotIn('book_overview', payload)
            jsonschema.validate(payload, schema)
        self.assertFalse((self.fixture.root / 'ReferenceLibrary/bible/book-overview.json').exists())

    def test_native_http_runs_all_write_stages_and_keeps_prose_unadopted(self):
        generated = []
        original = fixtures.specimen

        def response(schema):
            payload = original(schema)
            if isinstance(payload, dict) and 'artifact_markdown' in schema.get('properties', {}):
                if 'book_overview' in schema['properties']:
                    payload['book_overview'] = OVERVIEW
                generated.append(payload)
            return payload

        with patch.object(fixtures, 'specimen', response):
            result = self.runner.run('write', '生成独立正文候选，不采纳或覆盖原稿。')
        self.assertTrue(result.ok, result.error)
        runs = RunStore(self.fixture.root)
        expected = [stage['id'] for stage in self.runner._workflow('write')[1]['stages']]
        self.assertEqual(len(expected), 6)
        self.assertEqual(runs.load(result.run_id)['completed_stages'], expected)
        self.assertEqual(len(self.fixture.requests), 6)
        self.assertEqual(len(generated), 6)
        for payload, request in zip(generated, self.fixture.requests):
            self.assertEqual(request['tools'], [])
            jsonschema.validate(payload, request['text']['format']['schema'])
        self.assertEqual(generated[-1]['book_overview'], OVERVIEW)
        self.assertTrue(result.artifact_path.is_relative_to(self.fixture.root / 'ReferenceLibrary/drafts'))
        self.assertEqual(result.artifact_path.read_text().strip(), generated[-1]['artifact_markdown'])
        document = self.fixture.project.get_document('one')
        self.assertEqual(document.active_version_id, self.fixture.original.active_version_id)
        self.assertEqual(document.content, '作者已采纳的原稿。')
        self.assertEqual(document.state, 'accepted')
        self.assertEqual(len(self.fixture.project.list_versions('one')), 1)
        self.assertEqual(self.fixture.project.list_facts(state='canonical'), [])
        overview = json.loads((self.fixture.root / 'ReferenceLibrary/bible/book-overview.json').read_text())
        self.assertEqual(overview['text'], OVERVIEW)
        self.assertEqual(overview['source']['state'], 'draft')
