"""Codex compatibility adapter for the LG execution contract."""
import asyncio

from .agent_runtime import ExpertPolicy, RuntimeContext, RuntimeLaunchPolicy, RuntimeResult
from .codex_events import CodexEvents
from .codex_reviews import CodexReviews
from .native_events import NativeEvents


class CodexRuntime:
    def __init__(self, config, event, *, context_provider: RuntimeContext, launch_policy: RuntimeLaunchPolicy,
                 expert_policy: ExpertPolicy, agent_factory=None, on_request=None, conversation=None,
                 review_only=False, model_factory=None):
        if agent_factory is None:
            from .live_agent import LiveAgent
            agent_factory = LiveAgent
        self.event, self.request_handler, self.conversation = event, on_request, conversation
        self.review_only, self.expert_policy = review_only, expert_policy
        self._run_active = False
        self._running = False
        self._generation = 0
        self._bound_turn = None
        self._child_threads = set()
        self.execution_retired = False
        self._retirement_handler = None
        self.presentation = CodexEvents(event, lambda: self.thread_id, expected_turn=lambda: self.turn_id)
        tool = expert_policy.tool
        dynamic_tools = None if review_only else [{'type': 'function', 'name': tool['name'],
            'description': tool['description'], 'inputSchema': tool['parameters'], 'deferLoading': False}]
        self.agent = agent_factory(config, self._event, on_request=self._request,
            conversation=conversation, review_only=review_only, context_provider=context_provider,
            launch_policy=launch_policy, dynamic_tools=dynamic_tools)
        options = {} if model_factory is None else {'model_factory': model_factory}
        self.delegations = CodexReviews(config, expert_policy, runtime=self.runtime,
            root=lambda: self.thread_id, turn=lambda: self.turn_id,
            enabled=lambda: self._run_active and not self.review_only
                and getattr(self.agent, 'dynamic_tools_registered', False),
            emit=NativeEvents(event).review_task, **options)

    @property
    def runtime(self):
        return self.agent.runtime

    @property
    def on_request(self):
        return self._request

    @property
    def thread_id(self):
        return self.agent.thread_id

    @property
    def turn_id(self):
        return self.agent.turn_id

    @property
    def closed(self):
        return getattr(self.agent, 'closed', False)

    @property
    def control_socket(self):
        return getattr(self.agent, 'control_socket', None)

    @control_socket.setter
    def control_socket(self, value):
        self.agent.control_socket = value

    def _event(self, method, params):
        if (method == 'lg.turnBound' and self._run_active
            and params.get('threadId') == self.thread_id and params.get('turnId') == self.turn_id
            and self.turn_id and self._bound_turn != self.turn_id):
            if getattr(self.agent, 'dynamic_tools_registered', False) and not self.review_only:
                self.delegations.begin()
            self._bound_turn = self.turn_id
        if method in {'item/started', 'item/completed'}:
            item = params.get('item') or {}
            if item.get('type') == 'subAgentActivity' and item.get('agentThreadId'):
                self._child_threads.add(item['agentThreadId'])
            elif item.get('type') == 'collabAgentToolCall':
                self._child_threads.update(item.get('receiverThreadIds') or [])
        self.presentation(method, params)

    async def _request(self, method, params):
        if method == 'item/tool/call':
            generation = self._generation
            if (not self._run_active or not isinstance(params, dict)
                or params.get('threadId') != self.thread_id
                or not getattr(self.agent, 'dynamic_tools_registered', False) or self.review_only):
                raise ValueError('Expert request is outside an active registered root turn.')
            turn = await self.agent.wait_for_turn()
            if (not self._run_active or generation != self._generation
                or turn != self._bound_turn or turn != params.get('turnId')):
                raise ValueError('Expert request is outside the host-bound parent turn.')
            result = await self.delegations.call(params)
            if (not self._run_active or generation != self._generation
                or turn != self.turn_id or params.get('threadId') != self.thread_id):
                raise ValueError('Expert parent turn has ended.')
            return result
        if method == 'mcpServer/elicitation/request':
            return {'action': 'decline', 'content': None}
        kinds = {'item/permissions/requestApproval': 'permissions',
            'item/tool/requestUserInput': 'user_input', 'item/commandExecution/requestApproval': 'command',
            'item/fileChange/requestApproval': 'file_change'}
        if method not in kinds or self.request_handler is None:
            raise ValueError('Unsupported runtime request; no authorization granted.')
        result = await self.request_handler(kinds[method], params)
        if kinds[method] == 'permissions':
            return {'permissions': params.get('permissions', {}) if result['decision'] == 'accept' else {}, 'scope': 'turn'}
        return result

    async def start(self, *, context=''):
        if self.runtime:
            self.runtime.acquire()
            self.runtime.select_engine('codex')
        scope = self.runtime.read().get('task') if self.runtime else None
        review = not self.review_only and self.expert_policy.available(scope)
        self.agent.allow_core_delegation = not review
        self.agent.review_delegation = review
        try:
            await self.agent.start(context=context)
            if (getattr(self.agent, 'dynamic_tools_registered', False)
                or self.runtime and self.runtime.read().get('codex_delegations') is not None):
                self.delegations.restore()
        except BaseException:
            await self.agent.close()
            raise
        if review and not getattr(self.agent, 'dynamic_tools_registered', False):
            self.event('item.updated', {'item': {'id': 'codex-review-capability', 'kind': 'notice',
                'title': '独立审读', 'state': 'completed',
                'text': '本会话保留原有历史；独立专家审读需要新建会话。主 Agent 仍可使用文学工具完成审读。'}})
        self.execution_retired = False

    def set_retirement_handler(self, handler):
        """Host revocation runs synchronously before waiting for Core shutdown."""
        self._retirement_handler = handler

    async def _retire_execution(self):
        await self._settle_execution('stop')

    async def _settle_execution(self, action=None):
        self._run_active = False
        self._generation += 1
        self.delegations.revoke()
        revoked = self._retirement_handler() if action == 'stop' and self._retirement_handler else None

        async def stop():
            try:
                try:
                    if revoked is not None:
                        await revoked
                finally:
                    await self.delegations.drain()
            finally:
                if action == 'stop':
                    await self.agent.stop_execution()
                    self.execution_retired = True
                elif action == 'close':
                    await self.agent.close()
                if action in {'stop', 'close'}:
                    self._child_threads.clear()

        cleanup = asyncio.create_task(stop())
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError()

    async def run(self, text, *, output_schema=None):
        if self._running:
            raise ValueError('A Codex parent turn is already active.')
        self._running = True
        self._run_active = True
        self._generation += 1
        self._bound_turn = None
        self.presentation.begin()
        completed = False
        try:
            result = await self.agent.run(text, output_schema=output_schema)
            completed = result.get('status', 'completed') == 'completed'
        finally:
            try:
                # A display terminal event is not an execution cancellation. Retire
                # the owned Core process before declaring unfinished children stopped.
                if self._child_threads or not completed or self.delegations.pending:
                    await self._retire_execution()
                else:
                    await self._settle_execution()
            finally:
                self.presentation.finish()
                self._running = False
        messages = [i.get('text', '') for i in result.get('items', []) if i.get('type') == 'agentMessage']
        status = 'cancelled' if result.get('status') == 'interrupted' else result.get('status', 'completed')
        return RuntimeResult(status=status, result_id=result.get('id'), text=messages[-1] if messages else None)

    async def steer(self, text):
        return await self.agent.steer(text)

    async def interrupt(self):
        self._run_active = False
        self._generation += 1
        self.delegations.revoke()
        try:
            # The host's interrupt deadline must still cancel a missing Core ack.
            await self.agent.interrupt()
        finally:
            await self._settle_execution()

    async def close(self):
        await self._settle_execution('close')
