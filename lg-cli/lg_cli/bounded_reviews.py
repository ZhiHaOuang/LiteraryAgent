"""Product-owned bounded expert execution, independent of Runtime history formats."""
import asyncio
import copy
import json
import time
import uuid
from dataclasses import replace
from typing import Callable, Protocol

import jsonschema

from .agent_runtime import ExpertPolicy
from .config import LGConfig
from .execution_adapter import _local_schema, native_stage_config

TERMINAL = {'completed','failed','cancelled'}
MAX_RESULT_BYTES = 32768


class ExpertModel(Protocol):
    async def generate(self, request: dict, delta: Callable[[str], None]) -> dict: ...


def _expert_names(arguments):
    names = arguments.get('experts')
    if (not isinstance(names,list) or not 1 <= len(names) <= 2
        or any(not isinstance(name,str) or not 1 <= len(name) <= 128 for name in names)
        or len(set(names)) != len(names)):
        raise ValueError('Invalid bounded expert identities')
    return names


class BoundedReviews:
    record_schema = 'lg.expert-review.v1'
    child_prefix = 'expert-review-'

    def __init__(self, policy: ExpertPolicy, *, config: Callable[[], LGConfig],
                 scope: Callable[[], dict | None], parent: Callable[[], str | None],
                 enabled: Callable[[], bool], model_factory: Callable[[LGConfig], ExpertModel],
                 save: Callable[[], None], emit: Callable[[dict], None]):
        self.policy, self.tool = policy, copy.deepcopy(policy.tool)
        self.config, self.scope, self.parent, self.enabled = config, scope, parent, enabled
        self.model_factory, self.save, self.emit = model_factory, save, emit
        self.records, self.inputs = {}, {}
        self.used, self.seen, self.live, self.tasks = set(), set(), set(), set()
        self.deadline = 0
        self.parent_turn = None

    def available(self):
        return self.enabled() and self.policy.available(self.scope())

    def begin(self):
        if self.records or any(not task.done() for task in self.tasks):
            raise ValueError('Previous expert calls must be settled before a new parent turn')
        parent = self.parent()
        if not isinstance(parent,str) or not 1 <= len(parent) <= 128:
            raise ValueError('Expert analysis requires an active parent turn')
        if parent == self.parent_turn:
            raise ValueError('Expert budget is already bound to this parent turn')
        self.parent_turn = parent
        self.used.clear()
        self.seen.clear()
        self.deadline = time.monotonic() + self.config().timeout_seconds

    def prepare(self, call_id, arguments):
        jsonschema.validate(arguments,self.tool['parameters'])
        names = _expert_names(arguments)
        if not self.available():
            raise ValueError('Expert analysis requires a host-bound review task')
        if not isinstance(call_id,str) or not 1 <= len(call_id) <= 128 or call_id in self.seen:
            raise ValueError('Repeated or invalid expert call identity')
        if self.used.intersection(names) or len(self.used.union(names)) > 2:
            raise ValueError('Expert request budget exhausted for this turn')
        parent = self.parent()
        if not isinstance(parent,str) or not 1 <= len(parent) <= 128:
            raise ValueError('Expert analysis requires an active parent turn')
        if parent != self.parent_turn:
            raise ValueError('Expert budget is not bound to the current parent turn')
        config = self.config()
        limit = min(64000,config.max_stage_context_chars)
        prepared = self.policy.prepare(self.scope(),arguments,context_limit=limit)
        if ([expert.name for expert in prepared.experts] != arguments['experts']
            or not 1 <= len(prepared.experts) <= 2):
            raise ValueError('Invalid prepared expert identities')
        inputs = {}
        for expert in prepared.experts:
            expert_config = replace(native_stage_config(config,'check',expert.model_profile),
                max_output_tokens=min(4096,config.max_output_tokens))
            schema = json.loads(expert.output_schema_json)
            _local_schema(schema)
            request = {'model':expert_config.default_model,'instructions':expert.instructions,
                'input':[{'role':'user','content':expert.prompt}],'tools':[],'stream':True,
                'text':{'format':{'type':'json_schema','name':'lg_expert_handoff','schema':schema,'strict':True}}}
            serialized = json.dumps(request,ensure_ascii=False)
            if len(serialized) > limit:
                raise ValueError('Review request exceeds the expert context budget; narrow the author task')
            inputs[expert.name] = (expert_config,serialized)
        self.seen.add(call_id)
        self.used.update(arguments['experts'])
        self.inputs[call_id] = inputs
        self.records[call_id] = {'schema':self.record_schema,'parentTurn':parent,
            'taskId':prepared.task_id,'taskFingerprint':prepared.task_fingerprint,
            'sourceFingerprint':prepared.source_fingerprint,
            'children':[{'id':self.child_prefix+uuid.uuid4().hex,'expert':name,'state':'waiting',
                         'result':None,'error':None} for name in arguments['experts']]}

    def unchanged(self, record):
        return (record['parentTurn'] == self.parent() and self.available()
            and self.policy.unchanged(self.scope(),record))

    async def work(self, call_id, child):
        record = self.records[call_id]
        child['state'] = 'running'
        self.save()
        self.emit(child)
        result, error = None, None
        try:
            if record['parentTurn'] != self.parent():
                raise ValueError('Parent turn changed')
            prepared_config,serialized = self.inputs[call_id][child['expert']]
            request = json.loads(serialized)
            schema = request['text']['format']['schema']
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            config = replace(prepared_config,timeout_seconds=remaining)
            response = await asyncio.wait_for(self.model_factory(config).generate(request,lambda _:None),remaining)
            output = response.get('output') or []
            if response.get('status') != 'completed' or any(i.get('type') == 'function_call' for i in output):
                raise ValueError('Invalid expert completion')
            text = ''.join(c.get('text','') for i in output if i.get('type') == 'message'
                for c in i.get('content',[]) if c.get('type') == 'output_text')
            if len(text.encode()) > MAX_RESULT_BYTES:
                raise ValueError('Expert output exceeds its limit')
            result = json.loads(text)
            if len(json.dumps(result,ensure_ascii=False).encode()) > MAX_RESULT_BYTES:
                raise ValueError('Expert output exceeds its persistence limit')
            jsonschema.validate(result,schema)
            if not self.unchanged(record):
                raise ValueError('Source changed')
        except asyncio.CancelledError:
            raise
        except Exception:
            result,error = None,'expert_analysis_failed'
        if call_id not in self.live or record['parentTurn'] != self.parent() or child['state'] != 'running':
            return
        child.update(state='failed' if error else 'completed',result=result,error=error)
        self.save()

    async def execute(self, call_id):
        record = self.records[call_id]
        if call_id in self.live or any(child['state'] != 'waiting' for child in record['children']):
            raise ValueError('Expert call cannot be replayed')
        self.live.add(call_id)
        jobs = [asyncio.create_task(self.work(call_id,child)) for child in record['children']]
        self.tasks.update(jobs)
        try:
            await asyncio.gather(*jobs)
            if call_id not in self.live:
                return BoundedReviews.result(record,interrupted=True)
            try:
                valid = self.unchanged(record)
            except Exception:
                valid = False
            if not valid:
                for child in record['children']:
                    child.update(state='failed',result=None,error='review_source_changed')
            self.save()
            # Consumers treat terminal task events as immutable; publish only after the joint source check.
            for child in record['children']:
                self.emit(child)
            return BoundedReviews.result(record,source_current=valid)
        except BaseException:
            self.settle_interrupted()
            raise
        finally:
            self.live.discard(call_id)
            for job in jobs:
                if not job.done():
                    job.cancel()
            await asyncio.gather(*jobs,return_exceptions=True)
            self.tasks.difference_update(jobs)

    @staticmethod
    def result(record, *, source_current=True, interrupted=False):
        children = record['children']
        status = 'completed' if all(c['state'] == 'completed' for c in children) else (
            'cancelled' if any(c['state'] == 'cancelled' for c in children) else 'failed')
        if interrupted:
            status = 'cancelled'
        value = {'status':status,'provisional':True,'sourceCurrent':source_current and not interrupted,'taskId':record['taskId'],
            'sourceFingerprint':record['sourceFingerprint'],'experts':copy.deepcopy(children)}
        return value

    def finish(self, call_id):
        if call_id in self.live:
            raise ValueError('Cannot discard an active expert call')
        self.records.pop(call_id,None)
        self.inputs.pop(call_id,None)

    def settle_interrupted(self):
        """Revoke live calls before cancellation; adapters decide history settlement."""
        self.live.clear()
        for job in self.tasks:
            job.cancel()
        for record in self.records.values():
            for child in record['children']:
                if child['state'] not in TERMINAL:
                    child.update(state='cancelled',result=None,error='process_interrupted')
                self.emit(child)
        self.save()
