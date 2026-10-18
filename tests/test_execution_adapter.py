"""Real local HTTP stages through existing workflows, with Codex construction prohibited."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from lg_cli.book_analysis import BookAnalysisStore, ChapterAnalysisService
from lg_cli.core_adapter import run_model_turn
from lg_cli.doctor import probe_provider, run_doctor
from lg_cli.execution_adapter import NativeStageAdapter, execution_adapter
from lg_cli.init_project import init_workspace
from lg_cli.project_store import ProjectStore
from lg_cli.run_store import RunStore
from lg_cli.story_workflow import StoryWorkflowService
from lg_cli.workflow_runner import WorkflowRunner
from tests.helpers import make_config
from tests.test_anthropic_bridge import message_events


def specimen(schema):
    if 'enum' in schema:
        return schema['enum'][0]
    kind = schema.get('type')
    if isinstance(kind, list):
        kind = next(k for k in kind if k != 'null')
    if kind == 'object':
        return {key: specimen(schema['properties'][key]) for key in schema.get('required', [])}
    if kind == 'array':
        return [specimen(schema['items']) for _ in range(schema.get('minItems', 0))]
    if kind == 'integer':
        return max(1, schema.get('minimum', 0))
    if kind == 'boolean':
        return False
    return '维修师在浮城检查失灵的闸门，决定留下救援。'


class NativeStageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        init_workspace(self.root)
        self.project = ProjectStore(self.root)
        self.original = self.project.create_document(kind='chapter', slug='one', title='原稿',
            content='作者已采纳的原稿。', state='accepted')
        self.requests, self.fail_at, self.invalid = [], None, False
        owner = self
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                if self.path not in {'/responses', '/v1/messages'}:
                    self.send_response(404);self.end_headers();return
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append(request)
                if len(owner.requests) == owner.fail_at:
                    self.send_response(503);self.end_headers()
                    self.wfile.write(b'SENSITIVE_TEST_BODY');return
                anthropic = self.path == '/v1/messages'
                schema = request['tools'][-1]['input_schema'] if anthropic else ((request.get('text') or {}).get('format') or {}).get('schema')
                text = json.dumps(specimen(schema), ensure_ascii=False) if schema else 'LG_READY'
                if owner.invalid:
                    text = '{"wrong_shape":true}'
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                events = list(message_events(block={'type':'tool_use','id':'local-final','name':'lg_structured_output','input':{}},
                    delta={'type':'input_json_delta','partial_json':text},stop='tool_use')) if anthropic else [{'type':'response.output_text.delta','delta':text},
                    {'type':'response.completed','response':{'status':'completed',
                        'output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':text}]}],
                        'usage':{'input_tokens':10,'output_tokens':5}}}]
                for event in events:
                    self.wfile.write(('event: '+event['type']+'\ndata: '+json.dumps(event,ensure_ascii=False)+'\n\n').encode())
                self.wfile.flush()
        self.server = ThreadingHTTPServer(('127.0.0.1',0), Provider)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.close_provider)
        self.config = replace(make_config(self.root), runtime_engine='native', protocol='responses',
            default_model='native-default', writer_model='native-writer', critic_model='native-reviewer',
            base_url=f'http://127.0.0.1:{self.server.server_port}', max_stage_context_chars=200000)
        self.addCleanup(patch.stopall)
        patch('lg_cli.execution_adapter.CodexExecAdapter', side_effect=AssertionError('Native must not construct Codex')).start()
        patch('lg_cli.core_adapter.discover_core_commands', side_effect=AssertionError('Native must not probe Codex')).start()

    def close_provider(self):
        self.server.shutdown();self.server.server_close();self.worker.join(timeout=2)

    def test_role_model_guidance_and_no_stage_tools(self):
        events = []
        with patch.dict('os.environ', {'LG_WORKFLOW_GUIDANCE':'只调整动机，不增加场景'}):
            result = execution_adapter(self.config).run(prompt='只讨论结构', config=self.config,
                mode='check', model_profile='critic', on_event=events.append)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.requests[0]['model'], 'native-reviewer')
        self.assertEqual(self.requests[0]['tools'], [])
        self.assertIn('只调整动机', self.requests[0]['input'][0]['content'])
        self.assertEqual(events[0]['type'], 'lg.model.text.delta')
        self.assertEqual(result.command, [])

    def test_outline_failure_and_resume_use_existing_stage_store(self):
        self.fail_at = 3
        runner = WorkflowRunner(self.config)
        failed = runner.run('outline', '只规划第一卷')
        self.assertFalse(failed.ok)
        self.assertNotIn('SENSITIVE_TEST_BODY', failed.error)
        runs = RunStore(self.root)
        self.assertEqual(len(runs.load(failed.run_id)['completed_stages']), 2)
        self.fail_at = None
        resumed = runner.resume(failed.run_id)
        self.assertTrue(resumed.ok, resumed.error)
        self.assertEqual(len(self.requests), 8)
        self.assertEqual(len(runs.load(resumed.run_id)['completed_stages']), 7)
        self.assertTrue(resumed.artifact_path.is_relative_to(self.root/'ReferenceLibrary/plans'))
        self.assertEqual(self.project.get_document('one').active_version_id, self.original.active_version_id)
        self.assertEqual(self.project.list_proposals(), [])

    def test_revision_saves_candidate_without_overwriting_accepted_prose(self):
        result = StoryWorkflowService(self.config).revise_document('one', mode='light', request='加强动作因果')
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.project.get_document('one').content, '作者已采纳的原稿。')
        versions = self.project.list_versions('one')
        self.assertEqual(len(versions), 2)
        self.assertEqual(max(versions, key=lambda version:version.version_number).state, 'candidate')
        self.assertTrue(result.artifact_path.is_relative_to(self.root/'ReferenceLibrary/drafts'))
        self.assertEqual(self.requests[0]['model'], 'native-writer')

    def test_chapter_analysis_uses_native_and_preserves_source(self):
        result = ChapterAnalysisService(self.config).analyze('one', categories=['Worldview'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(BookAnalysisStore(self.project).list()[0]['status'], 'current')
        self.assertEqual(self.project.get_document('one').active_version_id, self.original.active_version_id)
        # Worldview keeps the existing worldbuilding specialist's writer profile.
        self.assertEqual(self.requests[0]['model'], 'native-writer')

    def test_invalid_structured_result_cannot_publish_workflow_artifact(self):
        self.invalid = True
        result = WorkflowRunner(self.config).run('world', '只设计世界规则')
        self.assertFalse(result.ok)
        self.assertEqual(RunStore(self.root).load(result.run_id)['completed_stages'], [])
        self.assertIsNone(result.artifact_path)
        self.assertEqual(self.project.get_document('one').active_version_id, self.original.active_version_id)

    def test_native_configuration_failure_never_falls_back(self):
        for config, mode in [(replace(self.config, auth_mode='chatgpt'),'write'),
            (replace(self.config, protocol='chat_completions'),'write'),
            (replace(self.config, api_key=None),'write'),
            (replace(self.config, default_model='', writer_model='', critic_model=''),'write'),
            (self.config,'code')]:
            with self.subTest(mode=mode,protocol=config.protocol):
                result = execution_adapter(config).run(prompt='任务', config=config, mode=mode)
                self.assertFalse(result.ok)
                self.assertTrue(result.used_stub)
        self.assertEqual(self.requests, [])
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            execution_adapter(replace(self.config, runtime_engine='unknown'))

    def test_compatibility_turn_and_doctor_use_selected_native_runtime(self):
        self.assertTrue(run_model_turn(prompt='连通性检查', config=self.config).ok)
        self.assertEqual(probe_provider(self.config).status, 'PASS')
        with patch('lg_cli.doctor.inspect_core', side_effect=AssertionError('Native doctor must not probe Codex')):
            report = {check.name:check for check in run_doctor(self.config).checks}
        self.assertEqual(report['native-runtime'].status, 'PASS')
        self.assertNotIn('core-runtime', report)
        with (
            patch('lg_cli.doctor.inspect_core', side_effect=AssertionError('No Codex probe')),
            patch('lg_cli.doctor.subprocess.run', side_effect=AssertionError('No subprocess auth probe')),
        ):
            invalid = {check.name:check for check in run_doctor(replace(self.config,auth_mode='chatgpt')).checks}
        self.assertEqual(invalid['native-runtime'].status,'FAIL')

    def test_external_schema_is_rejected_before_provider_call(self):
        schema = self.root/'external.schema.json'
        schema.write_text(json.dumps({'$ref':'https://example.invalid/private.json'}))
        result = execution_adapter(self.config).run(prompt='任务', config=self.config, mode='write', output_schema=schema)
        self.assertFalse(result.ok)
        self.assertIn('local schema', result.error)
        self.assertEqual(self.requests, [])

    def test_anthropic_stage_uses_structured_final_tool_without_codex(self):
        config = replace(self.config, protocol='anthropic', provider='stepfun')
        result = WorkflowRunner(config).run('world', '只设计世界规则')
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(self.requests), 4)
        self.assertTrue(all(request['tools'][-1]['name']=='lg_structured_output' for request in self.requests))
        self.assertTrue(result.artifact_path.is_relative_to(self.root/'ReferenceLibrary/bible'))

    def test_actual_cli_command_uses_native_stage_factory(self):
        # Both parent and owned worker use the same local synthetic profile.
        source = '''
import sys
from pathlib import Path
from unittest.mock import patch
import lg_cli.main as cli
from tests.test_cli_backend import isolated_cli
root = Path(sys.argv[1])
cli._load_runtime()
with isolated_cli(root,sys.argv[2]), patch("lg_cli.execution_adapter.CodexExecAdapter", side_effect=AssertionError("Native CLI invoked Codex")):
    raise SystemExit(cli.main(["-C", str(root), "--runtime-engine", "native", "--model", "native-cli", "--json", "world", "只设计世界规则"]))
'''
        repo = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable,'-c',source,str(self.root),self.config.base_url],
            cwd=self.root, env={**os.environ,'PYTHONPATH':str(repo)+':'+str(repo/'lg-cli')},
            capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode,0,result.stderr)
        records = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertTrue(any(record.get('type')=='run.completed' for record in records))
        self.assertEqual(len(self.requests),4)
        self.assertTrue(all(value['model']=='native-cli' for value in self.requests))
        self.assertEqual(self.project.get_document('one').active_version_id,self.original.active_version_id)

    def test_total_stage_deadline_cancels_model_without_fallback(self):
        class SlowModel:
            def __init__(self, config):
                pass
            async def generate(self, request, delta):
                await asyncio.sleep(2)
        result = NativeStageAdapter(model_factory=SlowModel).run(prompt='任务',
            config=replace(self.config, timeout_seconds=1), mode='write')
        self.assertFalse(result.ok)
        self.assertFalse(result.used_stub)
        self.assertIn('TimeoutError', result.error)
        self.assertEqual(self.requests, [])
