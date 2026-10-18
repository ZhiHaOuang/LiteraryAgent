"""Pinned Core requests prove host tool availability, using local synthetic SSE only."""
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from lg_cli.anthropic_bridge import translate_stream
from lg_cli.core_adapter import CodexExecAdapter, _exec_args
from lg_cli.provider_transport import ResponsesBridge
from tests.helpers import make_config
from tests.test_anthropic_bridge import message_events


def tool_names(tools):
    result = set()
    for tool in tools:
        if tool.get('type')=='namespace':
            result.update(tool_names(tool.get('tools',[])))
        else:
            result.add(tool.get('name',tool.get('type')))
    return result


@unittest.skipUnless(os.environ.get('LG_TEST_RUNTIME_MANIFEST'),'set LG_TEST_RUNTIME_MANIFEST for pinned integration')
class StageToolGateTests(unittest.TestCase):
    def exercise(self,root,*,baseline=False,temptation=False):
        requests = []
        config = replace(make_config(root),protocol='responses',default_model='synthetic-stage',enable_shell=True,
            runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']),timeout_seconds=45)
        canary = root/'private-canary.txt'
        canary.write_text('NOT_FOR_MODEL_PRIVATE_CANARY')
        schema = root/'reply.schema.json'
        schema.write_text(json.dumps({'type':'object','properties':{'answer':{'type':'string'}},
            'required':['answer'],'additionalProperties':False}))
        def factory(payload):
            requests.append(payload)
            if temptation and len(requests)==1:
                arguments = json.dumps({'cmd':'cat '+str(canary)})
                yield from translate_stream(message_events(
                    {'type':'tool_use','id':'attempted-exec','name':'synthetic-tool','input':{}},
                    {'type':'input_json_delta','partial_json':arguments},'tool_use'),
                    {'synthetic-tool':{'type':'function','name':'exec_command','namespace':None}})
            else:
                yield from translate_stream(message_events(delta={'type':'text_delta','text':'{"answer":"LG_OK"}'}),{})
        def old_args(**kwargs):
            values = _exec_args(**kwargs)
            removed = {'features.shell_tool=false','features.unified_exec=false','features.apply_patch_freeform=false',
                'features.view_image=false','features.apps=false','features.plugins=false','features.recommended_plugins=false',
                'features.sleep_tool=false','tools.experimental_request_user_input.enabled=false','tools.update_plan.enabled=false',
                'web_search="disabled"'}
            result = []
            index = 0
            while index<len(values):
                if values[index]=='-c' and values[index+1] in removed:
                    index+=2
                else:
                    result.append(values[index]);index+=1
            return result
        with patch('lg_cli.provider_transport.ResponsesBridge',side_effect=lambda _:ResponsesBridge(config,stream_factory=factory)):
            if baseline:
                with patch('lg_cli.core_adapter._exec_args',side_effect=old_args):
                    result = CodexExecAdapter().run(prompt='只返回指定 JSON，不读取文件。',config=config,mode='write',output_schema=schema)
            else:
                result = CodexExecAdapter().run(prompt='只返回指定 JSON，不读取文件。',config=config,mode='write',output_schema=schema)
        return result,requests

    def test_real_requests_remove_execution_and_image_tools(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            baseline,old = self.exercise(root,baseline=True)
            self.assertTrue(baseline.ok,baseline.error)
            old_names = tool_names(old[0].get('tools',[]))
            self.assertIn('exec_command',old_names)
            self.assertIn('view_image',old_names)
            result,requests = self.exercise(root)
            self.assertTrue(result.ok,result.error)
            names = tool_names(requests[0].get('tools',[]))
            self.assertFalse(names & {'exec_command','write_stdin','shell_command','shell','view_image','apply_patch',
                'spawn_agent','web_search','request_plugin_install'},names)
            self.assertEqual(names,set())
            self.assertEqual(json.loads(result.output_text),{'answer':'LG_OK'})

    def test_unadvertised_execution_cannot_return_private_file_content(self):
        with tempfile.TemporaryDirectory() as raw:
            result,requests = self.exercise(Path(raw),temptation=True)
            self.assertTrue(requests)
            self.assertNotIn('NOT_FOR_MODEL_PRIVATE_CANARY',json.dumps(requests))
            self.assertNotIn('NOT_FOR_MODEL_PRIVATE_CANARY',result.stdout)
            if len(requests)>1:
                outputs = [item.get('output','') for item in requests[-1].get('input',[]) if item.get('type')=='function_call_output']
                self.assertIn('unsupported call: exec_command',outputs)
