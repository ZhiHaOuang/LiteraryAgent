"""LG-owned bounded model/tool loop. Literary state stays in the existing MCP services."""
import asyncio
import json
import uuid

import jsonschema

from .agent_runtime import ExpertPolicy, RuntimeContext, RuntimeResult
from .native_model import NativeModel
from .native_tools import NativeTools
from .native_events import NativeEvents
from .output_writer import _atomic_text
from .session_runtime import SessionRuntime


class NativeRuntime:
    def __init__(self, config, event, *, context_provider: RuntimeContext, expert_policy: ExpertPolicy, conversation=None, on_request=None, review_only=False,
        model_factory=NativeModel, tools_factory=NativeTools):
        self.config, self.event, self.conversation, self.review_only = config, event, conversation, review_only
        self.context_provider = context_provider
        self.presentation = NativeEvents(event)
        self.runtime = SessionRuntime(config.workspace, conversation) if conversation else None
        self.model, self.tools_factory = model_factory(config), tools_factory
        self.model_factory = model_factory
        from .native_delegation import NativeDelegations
        self.delegations = NativeDelegations(self, expert_policy)
        self.control_socket = self.thread_id = self.turn_id = None
        self.tools = self.task = None
        self.messages, self.pending, self.steering = [], [], []
        self.checkpoint = None
        self.checkpoint_failed = False
        self.executing_tool = False

    async def start(self, *, context=''):
        if self.tools:
            return
        if self.config.auth_mode != 'api_key' or not self.config.api_key:
            raise ValueError('Native runtime requires an explicitly configured API profile; ChatGPT subscription access is no longer supported.')
        if not (self.config.uses_responses or self.config.uses_anthropic):
            raise ValueError('Native runtime supports Anthropic or Responses API profiles')
        if not self.config.default_model:
            raise ValueError('Select an explicit model for the Native runtime')
        if self.runtime:
            self.runtime.acquire()
            self.runtime.select_engine('native')
            self.thread_id = self.runtime.read().get('thread_id') or 'lg-native-' + uuid.uuid4().hex
            self.runtime.update(thread_id=self.thread_id)
            self.checkpoint = self.config.workspace / '.literarygiant' / 'runtime' / 'native' / (self.conversation + '.json')
            if not self.checkpoint.resolve().is_relative_to(self.config.workspace.resolve()) or self.checkpoint.is_symlink():
                raise ValueError('Native checkpoint points outside this workspace')
            if self.checkpoint.exists():
                saved = json.loads(self.checkpoint.read_text())
                if saved.get('schema') != 'lg.native-runtime.v1' or saved.get('thread') != self.thread_id:
                    raise ValueError('Native checkpoint identity does not match the selected session')
                self.messages, self.pending = saved['messages'], saved['pending']
                self.validate_checkpoint()
                self.delegations.restore(saved.get('delegations',{}))
            elif context:
                history = self.context_provider.history(context)
                if history:
                    self.messages.append({'role': 'user', 'content': history})
        else:
            self.thread_id = 'lg-native-review-' + uuid.uuid4().hex
        self.tools = self.tools_factory(self.config, self.control_socket, review_only=self.review_only)
        try:
            await self.tools.start()
        except BaseException:
            await self.tools.close()
            self.tools = None
            raise

    def validate_checkpoint(self):
        if (not isinstance(self.messages,list) or not isinstance(self.pending,list)
            or any(not isinstance(item,dict) for item in self.messages)
            or any(not isinstance(value,str) for value in self.pending)):
            raise ValueError('Invalid Native checkpoint history')
        calls, results = set(), set()
        unresolved = []
        for item in self.messages:
            kind, identity = item.get('type'), item.get('call_id')
            if kind not in {'function_call','function_call_output'}:
                continue
            if not isinstance(identity,str) or not identity:
                raise ValueError('Invalid Native checkpoint tool identity')
            if kind == 'function_call':
                if identity in calls or not isinstance(item.get('name'),str) or not isinstance(item.get('arguments'),str):
                    raise ValueError('Invalid Native checkpoint tool call')
                calls.add(identity)
                unresolved.append(identity)
            else:
                if identity not in calls or identity in results or not isinstance(item.get('output'),str):
                    raise ValueError('Invalid Native checkpoint tool result')
                results.add(identity)
                unresolved.remove(identity)
        if self.pending != unresolved:
            raise ValueError('Native checkpoint pending calls do not match its history')

    def save(self):
        if self.checkpoint:
            try:
                _atomic_text(self.checkpoint, json.dumps({'schema': 'lg.native-runtime.v1', 'thread': self.thread_id,
                    'messages': self.messages, 'pending': self.pending,
                    'delegations':self.delegations.records}, ensure_ascii=False) + '\n')
                self.checkpoint.chmod(0o600)
            except OSError:
                self.checkpoint_failed = True
                raise ValueError('Native checkpoint could not be saved; reopen and inspect the session') from None

    async def run(self, text, *, output_schema=None):
        if self.checkpoint_failed:
            raise ValueError('Native checkpoint is unavailable; reopen and inspect the session')
        if self.turn_id:
            raise ValueError('Native runtime already has an active turn')
        if self.pending:
            raise ValueError('A previous tool result is unresolved; inspect saved artifacts and use a new conversation. It will not be replayed.')
        self.turn_id = uuid.uuid4().hex
        self.delegations.begin()
        self.messages.append({'role': 'user', 'content': text})
        self.save()
        self.task = asyncio.create_task(self._run(output_schema))
        try:
            return await asyncio.wait_for(self.task, self.config.timeout_seconds)
        finally:
            try:
                if self.delegations.records:
                    self.delegations.settle_interrupted()
            finally:
                self.turn_id = None
                self.task = None

    async def _run(self, output_schema):
        delegation_tool = self.delegations.tool
        delegation_name = delegation_tool['name']
        delegates = self.delegations.available()
        instructions = self.context_provider.instructions(control_socket=self.control_socket, review_delegation=delegates)
        definitions = [{'type': 'function', 'name': tool.name, 'description': tool.description or tool.name,
            'parameters': tool.inputSchema} for tool in self.tools.tools.values()]
        if delegates:
            definitions.append(delegation_tool)
        for iteration in range(16):
            item_id = self.turn_id + ':' + str(iteration)
            streamed = []
            def delta(value):
                if value:
                    streamed.append(value)
                    self.presentation.assistant_delta(item_id, value)
            request = {'model': self.config.default_model, 'instructions': instructions,
                'input': list(self.messages), 'tools': definitions, 'stream': True}
            if output_schema:
                request['text'] = {'format': {'type': 'json_schema', 'name': 'lg_runtime_final', 'schema': output_schema, 'strict': True}}
            response = await self.model.generate(request, delta)
            if response.get('status') != 'completed':
                raise ValueError('Model generation was incomplete')
            output = response.get('output') or []
            final = ''.join(c.get('text', '') for item in output if item.get('type') == 'message'
                for c in item.get('content', []) if c.get('type') == 'output_text')
            if final or streamed:
                self.presentation.assistant_completed(item_id, final or ''.join(streamed))
            calls = [item for item in output if item.get('type') == 'function_call']
            if self.steering:
                if final:
                    self.messages.append({'role': 'assistant', 'content': final})
                self.messages.extend({'role': 'user', 'content': message} for message in self.steering)
                self.steering.clear()
                self.save()
                continue
            if not calls:
                if not final:
                    raise ValueError('Model returned no final text')
                if output_schema:
                    jsonschema.validate(json.loads(final), output_schema)
                self.messages.extend(item for item in output if item.get('type') == 'message')
                self.save()
                return RuntimeResult('completed', self.turn_id, final)
            if len(calls) > 16 or len({c.get('call_id') for c in calls}) != len(calls):
                raise ValueError('Invalid or repeated tool calls')
            validated = []
            historical_ids = {item.get('call_id') for item in self.messages if item.get('type') == 'function_call'}
            for call in calls:
                tool = self.tools.tools.get(call.get('name'))
                delegated = delegates and call.get('name') == delegation_name
                if (tool is None and not delegated) or not isinstance(call.get('call_id'), str):
                    raise ValueError('Model requested an unavailable tool')
                if not call['call_id'] or call['call_id'] in historical_ids:
                    raise ValueError('Model reused a previous tool call identity')
                arguments = json.loads(call.get('arguments', ''))
                if not isinstance(arguments, dict):
                    raise ValueError('Tool arguments must be an object')
                jsonschema.validate(arguments, delegation_tool['parameters'] if delegated else tool.inputSchema)
                validated.append((call, arguments))
            for call, arguments in validated:
                if call['name'] == delegation_name:
                    self.delegations.prepare(call['call_id'],arguments)
            self.messages.extend(item for item in output if item.get('type') in {'message', 'function_call'})
            self.pending = [call['call_id'] for call, _ in validated]
            self.save()
            for call, arguments in validated:
                tool_id = call['call_id']
                self.presentation.tool_started(tool_id, call['name'], arguments)
                self.executing_tool = True
                try:
                    result = await (self.delegations.execute(tool_id) if call['name'] == delegation_name else
                        self.tools.call(call['name'], arguments))
                finally:
                    self.executing_tool = False
                data = result.model_dump(exclude_none=True)
                self.presentation.tool_completed(tool_id, call['name'], arguments, data)
                self.messages.append({'type': 'function_call_output', 'call_id': tool_id,
                    'output': json.dumps(data, ensure_ascii=False)})
                self.pending.remove(tool_id)
                self.delegations.finish(tool_id)
                self.save()
        raise ValueError('Native runtime reached its bounded iteration limit')

    async def steer(self, text):
        if not self.turn_id or self.executing_tool:
            raise ValueError('No steerable model turn; stop the current tool operation before changing direction')
        self.steering.append(text)

    async def interrupt(self):
        if self.task:
            if self.delegations.records:
                self.delegations.settle_interrupted()
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def close(self):
        await self.interrupt()
        if self.tools:
            await self.tools.close()
            self.tools = None
        if self.runtime:
            self.runtime.release()
