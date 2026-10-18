"""Persistent app-server session for the author's main agent and turn steering."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
from contextlib import ExitStack

from .agent_runtime import RuntimeContext, RuntimeLaunchPolicy, RuntimePolicyError

from .core_adapter import _adapter_env, discover_core_commands
from .provider_transport import provider_bridge


class LiveAgentError(RuntimeError):
    pass


def dynamic_tools_sha256(tools: list[dict]) -> str:
    """Hash the frozen submitted contract, not a claimed Core tool readback."""
    return hashlib.sha256(json.dumps(tools, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


class CodexSession:
    """Codex session transport consuming host-prepared context and enforced tool policy."""

    def __init__(self, config, on_event, *, context_provider: RuntimeContext, launch_policy: RuntimeLaunchPolicy, command=None, on_request=None, control_socket=None, conversation=None, review_only=False, dynamic_tools: list[dict] | None = None):
        from .session_runtime import SessionRuntime
        self.runtime = SessionRuntime(config.workspace, conversation) if conversation else None
        self.config = config
        self.context_provider = context_provider
        self.launch_policy = launch_policy
        self.on_event = on_event
        self.on_request = on_request
        self.control_socket = control_socket
        self.review_only = review_only
        if dynamic_tools is not None and (not isinstance(dynamic_tools, list)
                                         or any(not isinstance(tool, dict) for tool in dynamic_tools)):
            raise ValueError('Invalid Codex dynamic tool contract')
        try:
            self._dynamic_tools_json = (json.dumps(dynamic_tools, sort_keys=True, separators=(',', ':'),
                                                  ensure_ascii=False, allow_nan=False)
                                        if dynamic_tools is not None else None)
        except (TypeError, ValueError):
            raise ValueError('Invalid Codex dynamic tool contract') from None
        self.dynamic_tools_registered = False
        self.allow_core_delegation = True
        self.review_delegation = False
        self._requests = set()
        self.command = command
        self.process = None
        self.thread_id = None
        self.turn_id = None
        self._pending = {}
        self._sequence = 0
        self._reader = None
        self._stderr = None
        self._completion = None
        self._completed = {}
        self._resources = ExitStack()
        self._write_lock = asyncio.Lock()
        self._turn_lock = asyncio.Lock()
        self._steer_lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._thread_models = {}
        self._usage_totals = {}
        self._model_lookups = set()

    async def start(self, *, context=''):
        try:
            if self.runtime:
                self.runtime.acquire()
            await self._start(context=context)
        except BaseException:
            await self.close()
            raise

    async def _start(self, *, context=''):
        if self.process:
            return
        self.dynamic_tools_registered = False
        self.config.require_api_auth()
        saved = self.runtime.read() if self.runtime else {}
        tools = json.loads(self._dynamic_tools_json) if self._dynamic_tools_json is not None and not self.review_only else None
        contract = saved.get('codex_tool_contract')
        if contract is not None:
            if (not isinstance(contract, dict) or set(contract) != {'schema', 'rootThreadId', 'sha256'}
                or contract.get('schema') != 'lg.codex-tools.v1'
                or not saved.get('thread_id') or contract.get('rootThreadId') != saved['thread_id']
                or not isinstance(contract.get('sha256'), str) or len(contract['sha256']) != 64
                or any(char not in '0123456789abcdef' for char in contract['sha256'])):
                raise RuntimePolicyError('Codex 会话工具契约无效，未恢复会话。')
            if tools is not None and contract['sha256'] != dynamic_tools_sha256(tools):
                raise RuntimePolicyError('Codex 会话工具契约已变化，请使用新会话。')
        registering = bool(tools) and not saved.get('thread_id')
        registered = bool(tools) and contract is not None
        if not self.command and not self.config.credentials_configured:
            raise LiveAgentError('请先使用 /model 配置模型。')
        env = None
        if self.command:
            argv = self.command
        else:
            candidates = await asyncio.to_thread(discover_core_commands,
                probe=True, runtime_manifest=self.config.runtime_manifest)
            candidate = next((c for c in candidates if c.available), None)
            if candidate is None:
                raise LiveAgentError('没有可用的内核运行时，请运行 literary doctor。')
            bridge = await asyncio.to_thread(self._resources.enter_context, provider_bridge(self.config))
            env = _adapter_env(self.config)
            if bridge:
                env.pop('CODEX_API_KEY', None)
                env['LG_BRIDGE_TOKEN'] = bridge.token
                env['NO_PROXY'] = env['no_proxy'] = ','.join(filter(None, [env.get('NO_PROXY', ''), '127.0.0.1,localhost,::1']))
                provider = bridge.codex_args()
            else:
                provider = []
            try:
                policy_options = {'control_socket': self.control_socket, 'review_only': self.review_only}
                if not self.allow_core_delegation:
                    policy_options['allow_core_delegation'] = False
                plan = self.launch_policy.prepare(self.config, **policy_options)
                overrides = [arg for key,value in plan.overrides for arg in ('-c',f'{key}={value}')]
            except RuntimePolicyError:
                raise
            except Exception:
                raise RuntimePolicyError('Codex 启动配置准备失败，未启动或恢复会话。') from None
            argv = [*candidate.command, *provider, *overrides, 'app-server', '--listen', 'stdio://']
        try:
            self.process = await asyncio.create_subprocess_exec(*argv, cwd=self.config.workspace,
                env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=4 * 1024 * 1024)
            self._reader = asyncio.create_task(self._read())
            self._stderr = asyncio.create_task(self._drain_stderr())
            await self.request('initialize', {'clientInfo': {'name': 'literarygiant', 'version': '0.3.0'},
                'capabilities': {'experimentalApi': True}})
            await self._send({'method': 'initialized', 'params': {}})
            if not self.command:
                try:
                    effective=await self.request('config/read',{'cwd':str(self.config.workspace),'includeLayers':True})
                    plan.validate(effective)
                except RuntimePolicyError:
                    raise
                except Exception:
                    raise RuntimePolicyError('Codex 启动配置核验失败，未启动或恢复会话。') from None
            context_options = {'control_socket': self.control_socket}
            if not self.allow_core_delegation:
                context_options['core_delegation'] = False
            if self.review_delegation and (registering or registered):
                context_options['review_delegation'] = True
            instructions = self.context_provider.instructions(**context_options)
            if context and not (self.runtime and self.runtime.read().get('thread_id')):
                history = self.context_provider.history(context)
                if history:
                    instructions += '\n' + history
            from .session_runtime import permissions
            params = {
                'cwd': str(self.config.workspace), 'model': self.config.default_model or None,
                **permissions(saved.get('mode', 'ask')),
                'sandbox': 'read-only',
                'baseInstructions': instructions,
            }
            if self.review_only:
                params.update(sandbox='read-only', approvalPolicy='never', approvalsReviewer='user')
            if saved.get('thread_id'):
                params['threadId'] = saved['thread_id']
                result = await self.request('thread/resume', params)
            else:
                params['ephemeral'] = self.runtime is None
                if tools is not None:
                    params['dynamicTools'] = tools
                result = await self.request('thread/start', params)
            thread = result['thread']
            thread_id = thread['id']
            if (not isinstance(thread_id, str) or not thread_id or thread.get('parentThreadId') is not None
                or saved.get('thread_id') and thread_id != saved['thread_id']):
                raise RuntimePolicyError('Codex 会话身份不匹配，未绑定工具契约。')
            self.thread_id = thread_id
            if self.runtime:
                updates = {'thread_id': self.thread_id}
                if registering:
                    updates['codex_tool_contract'] = {'schema': 'lg.codex-tools.v1',
                        'rootThreadId': self.thread_id, 'sha256': dynamic_tools_sha256(tools)}
                self.runtime.update(**updates)
            self.dynamic_tools_registered = bool(registering or registered)
            self._thread_models[self.thread_id] = result.get('model') or self.config.default_model or '模型名未记录'
        except BaseException:
            await self.close()
            raise

    async def _send(self, payload, *, turn_guard=None):
        if not self.process or self.process.returncode is not None:
            raise LiveAgentError('主 Agent 连接已断开。')
        async with self._write_lock:
            if turn_guard is not None and (turn_guard[0] is not self.process
                                          or turn_guard[1:] != (self.thread_id, self.turn_id)
                                          or not self.turn_id or self._completion is None or self._completion.done()):
                return
            self.process.stdin.write((json.dumps(payload, ensure_ascii=False) + '\n').encode())
            await self.process.stdin.drain()

    async def request(self, method, params):
        self._sequence += 1
        key = self._sequence
        future = asyncio.get_running_loop().create_future()
        self._pending[key] = future
        try:
            await self._send({'id': key, 'method': method, 'params': params})
            return await asyncio.wait_for(future, 45)
        finally:
            self._pending.pop(key, None)

    async def _read(self):
        error = LiveAgentError('主 Agent 连接已结束。')
        try:
            async for line in self.process.stdout:
                record = json.loads(line)
                if 'id' in record and 'method' not in record:
                    future = self._pending.get(record['id'])
                    if future and not future.done():
                        if 'error' in record:
                            future.set_exception(LiveAgentError(str(record['error'].get('message', '请求失败'))))
                        else:
                            future.set_result(record.get('result', {}))
                    continue
                method, params = record.get('method', ''), record.get('params') or {}
                self._record_usage(method, params)
                if method == 'thread/tokenUsage/updated' and self.config.auth_mode == 'chatgpt':
                    thread = params.get('threadId')
                    if thread and thread not in self._thread_models and thread not in self._model_lookups:
                        self._model_lookups.add(thread)
                        task = asyncio.create_task(self._load_thread_model(thread))
                        self._requests.add(task)
                        task.add_done_callback(self._requests.discard)
                if 'id' in record:
                    task = asyncio.create_task(self._server_request(record))
                    self._requests.add(task)
                    task.add_done_callback(self._requests.discard)
                    continue
                if method == 'turn/completed' and params.get('threadId') == self.thread_id:
                    turn = params['turn']
                    self._completed[turn['id']] = turn
                    if self._completion and not self._completion.done() and turn['id'] == self.turn_id:
                        self._completion.set_result(turn)
                self.on_event(method, params)
        except asyncio.CancelledError:
            raise
        except (ValueError, OSError, KeyError) as exc:
            error = LiveAgentError(f'主 Agent 协议连接失败：{type(exc).__name__}')
        finally:
            self._ready.set()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            if self._completion and not self._completion.done():
                self._completion.set_exception(error)

    def _record_usage(self, method, params):
        if method == 'thread/started':
            thread = params.get('thread') or {}
            if thread.get('model'):
                self._thread_models[thread['id']] = thread['model']
        elif method == 'model/rerouted' and params.get('toModel'):
            self._thread_models[params['threadId']] = params['toModel']
        if method == 'thread/tokenUsage/updated' and self.config.auth_mode == 'chatgpt':
            from .usage_ledger import UsageLedger
            usage = params.get('tokenUsage') or {}
            total, last = usage.get('total') or {}, usage.get('last') or {}
            current = (total.get('inputTokens'), total.get('outputTokens'))
            if any(type(value) is not int or value < 0 for value in current):
                return
            thread = params['threadId']
            previous = self._usage_totals.get(thread)
            # A fork can inherit cumulative history; only its first new response is chargeable here.
            delta = (last.get('inputTokens'), last.get('outputTokens')) if previous is None else tuple(max(0,a-b) for a,b in zip(current,previous))
            self._usage_totals[thread] = current
            if any(type(value) is not int or value < 0 for value in delta) or not any(delta):
                return
            UsageLedger(self.config.workspace).record(f'subscription:{thread}:{current[0]}:{current[1]}', self.config.provider,
                self._thread_models.get(thread, '模型名未记录'),
                {'input_tokens': delta[0], 'output_tokens': delta[1]}, source='core-thread-delta')

    async def _load_thread_model(self, thread):
        try:
            result = await self.request('thread/read', {'threadId': thread, 'includeTurns': False})
            model = (result.get('thread') or {}).get('model')
            if model:
                self._thread_models[thread] = model
                from .usage_ledger import UsageLedger
                UsageLedger(self.config.workspace).label_thread(thread, model)
        except (LiveAgentError, OSError, ValueError):
            pass

    async def _server_request(self, record):
        try:
            dynamic = record['method'] == 'item/tool/call'
            process = self.process
            params = record.get('params') or {}
            if self.on_request and record['method'] in {
                'item/tool/requestUserInput', 'item/commandExecution/requestApproval',
                'item/fileChange/requestApproval', 'item/permissions/requestApproval', 'mcpServer/elicitation/request',
                'item/tool/call'}:
                try:
                    if dynamic and (not self.dynamic_tools_registered or not isinstance(params, dict)
                                    or params.get('threadId') != self.thread_id or not self.thread_id):
                        raise ValueError('Unregistered dynamic tool caller')
                    result = await self.on_request(record['method'], params)
                except Exception:
                    # Host policy refusals must resolve the request without exposing its details.
                    response = {'id': record['id'], 'error': {'code': -32000,
                        'message': 'Request rejected by host policy'}}
                else:
                    response = {'id': record['id'], 'result': result}
                if dynamic and (self.process is not process or 'result' in response and (
                        params.get('threadId') != self.thread_id or params.get('turnId') != self.turn_id
                        or not self.turn_id or self._completion is None or self._completion.done())):
                    # Old requests must never respond into a replacement process/turn.
                    return
                if dynamic and 'result' in response:
                    await self._send(response, turn_guard=(process, params['threadId'], params['turnId']))
                else:
                    await self._send(response)
            else:
                await self._send({'id': record['id'], 'error': {'code': -32601,
                    'message': 'Unsupported interactive request'}})
                self.on_event('lg.requestUnsupported', {'method': record['method']})
        except (OSError, LiveAgentError):
            pass

    async def _drain_stderr(self):
        while await self.process.stderr.read(4096):
            pass

    async def run(self, text, *, output_schema=None):
        async with self._turn_lock:
            self._ready.clear()
            self._completion = asyncio.get_running_loop().create_future()
            try:
                params = {'threadId': self.thread_id,
                    'input': [{'type': 'text', 'text': text, 'text_elements': []}]}
                if output_schema is not None:
                    import jsonschema
                    jsonschema.Draft202012Validator.check_schema(output_schema)
                    params['outputSchema'] = output_schema
                result = await self.request('turn/start', params)
                self.turn_id = result['turn']['id']
                self.on_event('lg.turnBound', {'threadId': self.thread_id, 'turnId': self.turn_id})
                self._ready.set()
                if self.turn_id in self._completed:
                    self._completion.set_result(self._completed.pop(self.turn_id))
                try:
                    turn = await asyncio.wait_for(self._completion, self.config.timeout_seconds)
                except asyncio.TimeoutError:
                    await self.interrupt()
                    raise LiveAgentError('主 Agent 超时，已发送中断请求。') from None
                if turn.get('status') == 'failed':
                    raise LiveAgentError((turn.get('error') or {}).get('message', '主 Agent 执行失败。'))
                return turn
            finally:
                self._completed.pop(self.turn_id, None)
                self.turn_id = None
                self._ready.set()

    async def steer(self, text):
        async with self._steer_lock:
            await asyncio.wait_for(self._ready.wait(), 45)
            if not self.turn_id:
                raise LiveAgentError('上一轮已经结束，请重新发送这条消息。')
            return await self.request('turn/steer', {'threadId': self.thread_id,
                'expectedTurnId': self.turn_id,
                'input': [{'type': 'text', 'text': text, 'text_elements': []}]})

    async def wait_for_turn(self):
        try:
            await asyncio.wait_for(self._ready.wait(), min(45, self.config.timeout_seconds))
        except asyncio.TimeoutError:
            raise ValueError('No active Codex turn was bound') from None
        if not self.turn_id or self._completion is None or self._completion.done():
            raise ValueError('No active Codex turn was bound')
        return self.turn_id

    async def interrupt(self):
        if self.turn_id:
            await self.request('turn/interrupt', {'threadId': self.thread_id, 'turnId': self.turn_id})

    async def stop_execution(self):
        """Stop owned workers while retaining the host's exclusive session lease."""
        if self.process:
            # Stdio EOF lets Core shut down threads and flush their saved history.
            if self.process.stdin:
                self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await self.process.wait()
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for task in (*self._requests, self._reader, self._stderr):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (*self._requests, self._reader, self._stderr) if t), return_exceptions=True)
        await asyncio.to_thread(self._resources.close)
        self.process = None
        self.dynamic_tools_registered = False

    async def close(self):
        await self.stop_execution()
        if self.runtime:
            self.runtime.release()


class LiveAgent(CodexSession):
    """Compatibility product entry for callers outside the shared runtime factory."""

    def __init__(self, config, on_event, *, context_provider=None, launch_policy=None, **kwargs):
        if context_provider is None:
            from .runtime_context import LiteraryContext
            context_provider = LiteraryContext(config, review_only=kwargs.get('review_only', False))
        if launch_policy is None:
            from .codex_policy import WritingLaunchPolicy
            launch_policy = WritingLaunchPolicy()
        super().__init__(config, on_event, context_provider=context_provider, launch_policy=launch_policy, **kwargs)
