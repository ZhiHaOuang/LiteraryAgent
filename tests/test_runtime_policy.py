"""Effective config must not add unapproved tools or change LG's MCP transport."""
import asyncio
import json
import os
import shlex
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from lg_cli.anthropic_bridge import AnthropicBridge
from lg_cli.init_project import init_workspace
from lg_cli.live_agent import LiveAgent
from lg_cli.agent_runtime import RuntimePolicyError
from tests.helpers import make_config
from tests.test_anthropic_bridge import message_events


class RuntimePolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.config=make_config(Path(self.tmp.name))

    def snapshot(self,*,review=False):
        from lg_cli.native import writing_args
        from lg_cli.codex_policy import READ_TOOLS
        value={}
        args=writing_args(self.config)
        for index in range(0,len(args),2):
            key,raw=args[index+1].split('=',1);cursor=value
            parts=key.split('.')
            for part in parts[:-1]:cursor=cursor.setdefault(part,{})
            cursor[parts[-1]]=json.loads(raw)
        owned=value['mcp_servers']['lg_writing'];owned.pop('env_vars');owned['environment_id']='local'
        value['features']['multi_agent']=not review;value['features']['multi_agent_v2']={'enabled':not review}
        if review:
            owned['enabled_tools']=READ_TOOLS
            value['agents']={'enabled':False}
        return {'config':value,'layers':[]}

    def test_inactive_project_layers_cannot_hide_servers_until_trust_changes(self):
        from lg_cli.codex_policy import validate_writing_config,RuntimePolicyError
        value=self.snapshot();value['layers']=[{'config':{'mcp_servers':{'foreign':{'command':'untrusted'}}},'disabledReason':'untrusted project'}]
        with self.assertRaises(RuntimePolicyError):validate_writing_config(value,self.config)

    def test_registered_writing_and_review_surfaces_pass_but_review_cannot_keep_write_tools(self):
        from lg_cli.codex_policy import validate_writing_config,RuntimePolicyError
        validate_writing_config(self.snapshot(),self.config)
        validate_writing_config(self.snapshot(review=True),self.config,review_only=True)
        with self.assertRaises(RuntimePolicyError):validate_writing_config(self.snapshot(),self.config,review_only=True)

    def test_disabled_foreign_entry_is_allowed_without_activating_it(self):
        from lg_cli.codex_policy import validate_writing_config
        value=self.snapshot();value['config']['mcp_servers']['foreign']={'enabled':False,'command':'untrusted'}
        validate_writing_config(value,self.config)

    def test_foreign_servers_or_modified_transport_are_rejected_without_config_values(self):
        from lg_cli.codex_policy import validate_writing_config,RuntimePolicyError
        mutations=[lambda c:c['mcp_servers'].update(foreign={'enabled':True,'url':'SYNTHETIC_PRIVATE_VALUE'}),
            lambda c:c['mcp_servers']['lg_writing']['env'].update(PRIVATE='SYNTHETIC_PRIVATE_VALUE'),
            lambda c:c['mcp_servers']['lg_writing'].update(args=['SYNTHETIC_PRIVATE_VALUE']),
            lambda c:c['mcp_servers']['lg_writing'].update(cwd='SYNTHETIC_PRIVATE_VALUE'),
            lambda c:c['mcp_servers']['lg_writing'].update(env_vars=['SYNTHETIC_PRIVATE_VALUE']),
            lambda c:c['mcp_servers']['lg_writing'].update(environment_id='remote'),
            lambda c:c['mcp_servers']['lg_writing'].update(tools={'studio_accept':{'approval_mode':'approve'}}),
            lambda c:c['features'].update(hooks=True),lambda c:c.update(notify=['SYNTHETIC_PRIVATE_VALUE']),
            lambda c:c['features'].update(code_mode=True)]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                value=self.snapshot();mutate(value['config'])
                with self.assertRaises(RuntimePolicyError) as caught:validate_writing_config(value,self.config)
                self.assertNotIn('SYNTHETIC_PRIVATE_VALUE',str(caught.exception))

    def test_malformed_or_missing_config_fails_closed(self):
        from lg_cli.codex_policy import validate_writing_config,RuntimePolicyError
        for value in (None,{}, {'config':None},{'config':{'mcp_servers':[]}}, {'config':{'mcp_servers':{'lg_writing':None}}}):
            with self.subTest(value=value),self.assertRaises(RuntimePolicyError):validate_writing_config(value,self.config)


@unittest.skipUnless(os.environ.get('LG_TEST_RUNTIME_MANIFEST'),'set LG_TEST_RUNTIME_MANIFEST for pinned Core')
class RuntimePolicyIntegrationTests(unittest.TestCase):
    def test_full_access_lg_write_keeps_the_core_filesystem_read_only(self):
        from lg_cli.backend import Backend
        from lg_cli.book_overview import read_overview
        async def exercise(root):
            init_workspace(root)
            config=replace(make_config(root),provider='stepfun',protocol='anthropic',default_model='step-3.5-flash',
                runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']))
            payloads=[];sandboxes=[];original=LiveAgent.request
            async def audited(agent,method,params):
                result=await original(agent,method,params)
                if method in {'thread/start','thread/resume'}:sandboxes.append(result.get('sandbox'))
                return result
            text='维修师在停摆的浮城中寻找供能失衡的真相。'
            def response(payload):
                payloads.append(payload)
                if len(payloads)==1:
                    name=next(tool['name'] for tool in payload['tools'] if 'Save one concise Chinese sentence' in tool.get('description',''))
                    yield from message_events(block={'type':'tool_use','id':'overview','name':name,'input':{}},
                        delta={'type':'input_json_delta','partial_json':json.dumps({'text':text})},stop='tool_use')
                else:yield from message_events(delta={'type':'text_delta','text':'已更新指定概况。'})
            with patch('lg_cli.live_agent.provider_bridge',side_effect=lambda _:AnthropicBridge(config,stream_factory=response)),patch.object(LiveAgent,'request',audited):
                backend=Backend(config)
                try:
                    key=(await backend.dispatch('session/create',{}))['sessionId']
                    await backend.dispatch('permissions/set',{'sessionId':key,'mode':'full_access','creativeMode':'ask'})
                    run=await backend.run(key,'更新作品概况',task={'kind':'overview'})
                    await asyncio.wait_for(backend.tasks[run['runId']][1],45)
                    self.assertEqual(backend.journal.read(key)[-1]['params']['status'],'completed')
                    self.assertEqual(read_overview(root),text)
                    self.assertTrue(sandboxes);self.assertTrue(all(s and s.get('type')=='readOnly' for s in sandboxes),sandboxes)
                    self.assertFalse(backend.approvals.pending)
                finally:await backend.close()
        with tempfile.TemporaryDirectory() as raw:asyncio.run(exercise(Path(raw)))

    def test_untrusted_project_mcp_cannot_activate_during_thread_start(self):
        async def exercise(root):
            init_workspace(root)
            config=replace(make_config(root),provider='stepfun',protocol='anthropic',default_model='step-3.5-flash',
                runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']))
            project=root/'.codex';project.mkdir();canary=root/'autotrust-started'
            source='from pathlib import Path; Path('+repr(str(canary))+').write_text("STARTED"); from mcp.server.fastmcp import FastMCP; FastMCP("foreign").run()'
            (project/'config.toml').write_text('[mcp_servers.foreign]\ncommand = '+json.dumps(sys.executable)+'\nargs = '+json.dumps(['-c',source])+'\n')
            agent=LiveAgent(config,lambda *_:None)
            with patch('lg_cli.live_agent.provider_bridge',side_effect=lambda _:AnthropicBridge(config,stream_factory=lambda _:iter(()))):
                try:
                    caught=None
                    try:await agent.start()
                    except RuntimePolicyError as exc:caught=exc
                    if caught is None:
                        for _ in range(200):
                            if canary.exists():break
                            await asyncio.sleep(.01)
                    self.assertFalse(canary.exists(),'Project trust changes must not activate an unchecked MCP')
                    self.assertIsInstance(caught,RuntimePolicyError)
                finally:await agent.close()
        with tempfile.TemporaryDirectory() as raw:asyncio.run(exercise(Path(raw)))

    def test_cancel_during_config_check_closes_core_and_never_requests_a_model(self):
        async def exercise(root):
            init_workspace(root)
            config=replace(make_config(root),provider='stepfun',protocol='anthropic',default_model='step-3.5-flash',
                runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']))
            entered=asyncio.Event();process=[];payloads=[]
            def response(payload):payloads.append(payload);return iter(())
            agent=LiveAgent(config,lambda *_:None);request=agent.request
            async def waiting(method,params):
                if method=='config/read':
                    process.append(agent.process);entered.set();await asyncio.Future()
                return await request(method,params)
            agent.request=waiting
            with patch('lg_cli.live_agent.provider_bridge',side_effect=lambda _:AnthropicBridge(config,stream_factory=response)):
                task=asyncio.create_task(agent.start())
                try:
                    await asyncio.wait_for(entered.wait(),15);task.cancel()
                    with self.assertRaises(asyncio.CancelledError):await task
                    self.assertIsNone(agent.process);self.assertIsNotNone(process[0].returncode)
                    self.assertEqual(payloads,[])
                finally:
                    if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
                    await agent.close()
        with tempfile.TemporaryDirectory() as raw:asyncio.run(exercise(Path(raw)))

    def test_trusted_project_cannot_add_mcp_when_resuming_a_saved_session(self):
        from lg_cli.conversation_store import ConversationStore
        from lg_cli.session_runtime import SessionRuntime
        async def exercise(root):
            init_workspace(root)
            config=replace(make_config(root),provider='stepfun',protocol='anthropic',default_model='step-3.5-flash',
                runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']))
            payloads=[]
            def response(payload):
                payloads.append(payload)
                yield from message_events(delta={'type':'text_delta','text':'已保存会话。'})
            key=ConversationStore(root).create()
            with patch('lg_cli.live_agent.provider_bridge',side_effect=lambda _:AnthropicBridge(config,stream_factory=response)):
                first=LiveAgent(config,lambda *_:None,conversation=key)
                try:await first.start();await first.run('只回答，不调用工具。')
                finally:await first.close()
                thread=SessionRuntime(root,key).read()['thread_id']
                home=root/'.literarygiant/codex-home'
                (home/'config.toml').write_text('[projects.'+json.dumps(str(root))+']\ntrust_level = "trusted"\n')
                project=root/'.codex';project.mkdir()
                canary=root/'project-mcp-started'
                source='from pathlib import Path; Path('+repr(str(canary))+').write_text("STARTED")'
                (project/'config.toml').write_text('[mcp_servers.project_extra]\ncommand = '+json.dumps(sys.executable)+'\nargs = '+json.dumps(['-c',source])+'\n')
                second=LiveAgent(config,lambda *_:None,conversation=key)
                try:
                    with self.assertRaisesRegex(RuntimePolicyError,'隔离'):await second.start()
                    self.assertEqual(len(payloads),1)
                    self.assertFalse(canary.exists());self.assertIsNone(second.process)
                    self.assertEqual(SessionRuntime(root,key).read()['thread_id'],thread)
                finally:await second.close()
        with tempfile.TemporaryDirectory() as raw:asyncio.run(exercise(Path(raw)))

    def test_user_hooks_notify_and_optional_execution_surfaces_stay_disabled(self):
        async def exercise(root):
            init_workspace(root)
            config=replace(make_config(root),provider='stepfun',protocol='anthropic',default_model='step-3.5-flash',
                runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']))
            home=root/'.literarygiant/codex-home';home.mkdir(exist_ok=True)
            hook=root/'hook-started';notify=root/'notify-started'
            script=lambda target:'from pathlib import Path; Path('+repr(str(target))+').write_text("STARTED")'
            raw='notify = '+json.dumps([sys.executable,'-c',script(notify)])+'\n[features]\nhooks = true\nview_image = true\ncode_mode = true\nbrowser_use = true\n'
            (home/'config.toml').write_text(raw)
            (home/'hooks.json').write_text(json.dumps({'SessionStart':[{'hooks':[{'type':'command',
                'command':shlex.join([sys.executable,'-c',script(hook)])}]}]}))
            payloads=[]
            def response(payload):
                payloads.append(payload)
                yield from message_events(delta={'type':'text_delta','text':'{"result":"read-only"}'})
            agent=LiveAgent(config,lambda *_:None,review_only=True)
            with patch('lg_cli.live_agent.provider_bridge',side_effect=lambda _:AnthropicBridge(config,stream_factory=response)):
                try:
                    await agent.start();await agent.run('只返回 JSON，不调用工具。')
                    await asyncio.sleep(.2)
                    self.assertFalse(hook.exists());self.assertFalse(notify.exists())
                    names={tool.get('name') for tool in payloads[0].get('tools',[])}
                    for name in ('exec_command','view_image','apply_patch','code_mode','browser_use','image_generation','spawn_agent'):
                        self.assertFalse(any(name in item for item in names),names)
                    self.assertEqual((home/'config.toml').read_text(),raw)
                finally:await agent.close()
        with tempfile.TemporaryDirectory() as raw:asyncio.run(exercise(Path(raw)))

    def test_foreign_mcp_is_rejected_before_startup_and_before_any_model_request(self):
        async def exercise(root):
            init_workspace(root)
            config=replace(make_config(root),provider='stepfun',protocol='anthropic',default_model='step-3.5-flash',
                runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']))
            home=root/'.literarygiant/codex-home';home.mkdir(exist_ok=True)
            canary=root/'foreign-started'
            source='from pathlib import Path; Path('+repr(str(canary))+').write_text("STARTED"); from mcp.server.fastmcp import FastMCP; FastMCP("foreign").run()'
            (home/'config.toml').write_text('[mcp_servers.foreign]\ncommand = '+json.dumps(sys.executable)+'\nargs = '+json.dumps(['-c',source])+'\n')
            payloads=[]
            def response(payload):
                payloads.append(payload)
                yield from message_events(delta={'type':'text_delta','text':'not expected'})
            agent=LiveAgent(config,lambda *_:None,review_only=True)
            with patch('lg_cli.live_agent.provider_bridge',side_effect=lambda _:AnthropicBridge(config,stream_factory=response)):
                try:
                    caught=None
                    try:await asyncio.wait_for(agent.start(),35)
                    except RuntimePolicyError as exc:caught=exc
                    if caught is None:
                        for _ in range(200):
                            if canary.exists():break
                            await asyncio.sleep(.01)
                    self.assertFalse(canary.exists(),'Even listing tools must not launch the foreign process')
                    self.assertIsInstance(caught,RuntimePolicyError)
                    self.assertIn('隔离',str(caught))
                    self.assertEqual(payloads,[])
                    self.assertIsNone(agent.process)
                finally:await agent.close()
        with tempfile.TemporaryDirectory() as raw:asyncio.run(exercise(Path(raw)))

    def test_inherited_lg_environment_is_rejected_without_echoing_its_value(self):
        async def exercise(root):
            init_workspace(root)
            config=replace(make_config(root),provider='stepfun',protocol='anthropic',default_model='step-3.5-flash',
                runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']))
            home=root/'.literarygiant/codex-home';home.mkdir(exist_ok=True)
            value='SYNTHETIC_PRIVATE_CONFIG_VALUE'
            (home/'config.toml').write_text('[mcp_servers.lg_writing.env]\nUNREGISTERED = '+json.dumps(value)+'\n')
            with patch('lg_cli.live_agent.provider_bridge',side_effect=lambda _:AnthropicBridge(config,stream_factory=lambda _:iter(()))):
                agent=LiveAgent(config,lambda *_:None)
                try:
                    with self.assertRaisesRegex(RuntimePolicyError,'隔离') as caught:await agent.start()
                    self.assertNotIn(value,str(caught.exception));self.assertIsNone(agent.process)
                finally:await agent.close()
        with tempfile.TemporaryDirectory() as raw:asyncio.run(exercise(Path(raw)))
