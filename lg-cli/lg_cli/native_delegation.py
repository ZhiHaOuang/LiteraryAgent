"""Native history and recovery adapter for product-owned expert execution."""
import json

import jsonschema
from mcp.types import CallToolResult, TextContent

from .agent_runtime import ExpertPolicy
from .bounded_reviews import BoundedReviews, MAX_RESULT_BYTES, TERMINAL, _expert_names
# Keep the historical tool exports available to existing callers.
from .review_policy import NAME, EXPERTS, SCHEMA, TOOL


class NativeDelegations(BoundedReviews):
    record_schema = 'lg.native-delegation.v1'
    child_prefix = 'native-review-'

    def __init__(self, owner, policy: ExpertPolicy):
        self.owner = owner
        super().__init__(policy, config=lambda: owner.config,
            scope=lambda: owner.runtime.read().get('task') if owner.runtime else None,
            parent=lambda: owner.turn_id, enabled=lambda: not owner.review_only,
            model_factory=lambda config: owner.model_factory(config),
            save=owner.save, emit=owner.presentation.review_task)

    def prepare(self, call_id, arguments):
        if any(item.get('call_id') == call_id for item in self.owner.messages):
            raise ValueError('Repeated or invalid expert call identity')
        super().prepare(call_id, arguments)

    @staticmethod
    def tool_result(value):
        return CallToolResult(content=[TextContent(type='text',text=json.dumps(value,ensure_ascii=False))],
            structuredContent=value,isError=value['status'] != 'completed')

    @staticmethod
    def result(record, *, source_current=True, interrupted=False):
        return NativeDelegations.tool_result(BoundedReviews.result(record,
            source_current=source_current, interrupted=interrupted))

    async def execute(self, call_id):
        return self.tool_result(await super().execute(call_id))

    def settle_interrupted(self):
        # Only registered tool-free calls can be resolved without replaying a model.
        for call_id in list(self.records):
            if call_id not in self.owner.pending:
                self.live.discard(call_id)
                self.finish(call_id)
        super().settle_interrupted()
        for call_id, record in list(self.records.items()):
            data = self.result(record,interrupted=True).model_dump(exclude_none=True)
            self.owner.messages.append({'type':'function_call_output','call_id':call_id,
                'output':json.dumps(data,ensure_ascii=False)})
            self.owner.pending.remove(call_id)
            self.finish(call_id)
        self.owner.save()

    def restore(self, records):
        if not isinstance(records,dict) or len(records) > 2:
            raise ValueError('Invalid Native delegation checkpoint')
        experts = set()
        for call_id,record in records.items():
            calls = [i for i in self.owner.messages if i.get('call_id') == call_id]
            if (call_id not in self.owner.pending or len(calls) != 1 or calls[0].get('type') != 'function_call'
                or calls[0].get('name') != self.tool['name'] or not isinstance(record,dict)
                or record.get('schema') != 'lg.native-delegation.v1'
                or set(record) != {'schema','parentTurn','taskId','taskFingerprint','sourceFingerprint','children'}):
                raise ValueError('Invalid Native delegation checkpoint identity')
            arguments = json.loads(calls[0]['arguments'])
            jsonschema.validate(arguments,self.tool['parameters'])
            names = _expert_names(arguments)
            children = record.get('children')
            if (not isinstance(children,list) or any(not isinstance(c,dict) for c in children)
                or [c.get('expert') for c in children] != arguments['experts']
                or experts.intersection(names) or len(experts.union(names)) > 2):
                raise ValueError('Invalid Native delegation children')
            experts.update(arguments['experts'])
            for key in ('parentTurn','taskId','taskFingerprint','sourceFingerprint'):
                if not isinstance(record.get(key),str) or not 1 <= len(record[key]) <= 128:
                    raise ValueError('Invalid Native delegation source identity')
            for child in children:
                if (not isinstance(child.get('id'),str) or len(child['id']) > 128 or
                    child.get('state') not in TERMINAL | {'waiting','running'} or
                    set(child) != {'id','expert','state','result','error'} or
                    child.get('error') not in {None,'expert_analysis_failed','review_source_changed','process_interrupted'}):
                    raise ValueError('Invalid Native delegation state')
                if child['state'] == 'completed':
                    result = child.get('result')
                    if len(json.dumps(result).encode()) > MAX_RESULT_BYTES:
                        raise ValueError('Invalid Native delegation result size')
                    jsonschema.validate(result,self.policy.output_schema(child['expert']))
                elif child.get('result') is not None:
                    raise ValueError('Unverified Native delegation result')
        self.records = records
        if records:
            self.settle_interrupted()
