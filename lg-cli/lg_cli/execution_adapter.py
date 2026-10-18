"""Runtime selection for non-interactive literary stages; result fields remain compatible."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import jsonschema

from .core_adapter import CodexExecAdapter, CoreExecutionResult
from .output_schema import validate_local_schema as _local_schema


def execution_adapter(config):
    if config.runtime_engine == 'codex':
        return CodexExecAdapter()
    if config.runtime_engine == 'native':
        return NativeStageAdapter()
    raise ValueError('Unknown LG execution runtime')


def native_stage_config(config, mode, model_profile=None):
    if config.auth_mode != 'api_key' or not config.api_key:
        raise ValueError('Native runtime requires an explicitly configured API profile; ChatGPT subscription access is no longer supported.')
    if not (config.uses_responses or config.uses_anthropic):
        raise ValueError('Native runtime supports Anthropic or Responses API profiles')
    if mode == 'code':
        raise ValueError('Native literary stages do not support code execution')
    model = config.model_for_profile(model_profile or '', mode=mode)
    if not model:
        raise ValueError('Select an explicit model for the Native runtime')
    return replace(config, default_model=model)


class NativeStageExecutor:
    """One model response per assigned stage. The domain service alone persists its output."""
    def __init__(self, *, model_factory=None):
        self.model_factory = model_factory

    def run(self, *, prompt, instructions, config, mode, model_profile=None, output_schema=None, on_event=None):
        started, events = time.monotonic(), []
        def result(ok, text='', error=None, *, configuration=False):
            return CoreExecutionResult(ok=ok, used_stub=configuration, output_text=text, error=error,
                command_name='lg-native-stage', command=[], returncode=0 if ok else 1,
                stdout='', stderr='', duration_seconds=time.monotonic()-started, events=tuple(events))
        try:
            selected = native_stage_config(config, mode, model_profile)
            schema = json.loads(output_schema.read_text()) if output_schema else None
            if schema is not None:
                _local_schema(schema)
            # Synchronous stage APIs belong in CLI processes or workers, never the UI read loop.
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                raise ValueError('Run synchronous literary stages in a worker outside the event loop')
        except ValueError as exc:
            return result(False, error=str(exc), configuration=True)
        except (OSError, jsonschema.SchemaError, TypeError):
            return result(False, error='Stage output schema is invalid or unreadable', configuration=True)
        request = {'model': selected.default_model,
            'instructions': instructions,
            'input': [{'role': 'user', 'content': prompt}], 'tools': [], 'stream': True}
        if schema is not None:
            request['text'] = {'format': {'type': 'json_schema', 'name': 'lg_stage_final', 'schema': schema, 'strict': True}}
        def delta(text):
            event = {'type': 'lg.model.text.delta', 'delta': text}
            events.append(event)
            if on_event:
                on_event(event)
        async def generate():
            from .native_model import NativeModel
            model = (self.model_factory or NativeModel)(selected)
            return await asyncio.wait_for(model.generate(request, delta), selected.timeout_seconds)
        try:
            response = asyncio.run(generate())
            if response.get('status') != 'completed':
                raise ValueError('Stage model generation was incomplete')
            output = response.get('output') or []
            if any(item.get('type') == 'function_call' for item in output):
                raise ValueError('Stage model requested an unavailable tool')
            text = ''.join(c.get('text', '') for item in output if item.get('type') == 'message'
                for c in item.get('content', []) if c.get('type') == 'output_text')
            if not text.strip():
                raise ValueError('Stage model returned no final text')
            if schema is not None:
                jsonschema.validate(json.loads(text), schema)
            return result(True, text)
        except Exception as exc:
            # Provider errors are sanitized by NativeModel; never publish arbitrary result bodies.
            from .anthropic_bridge import ProtocolError
            error = str(exc) if isinstance(exc, ProtocolError) else 'Native stage failed: ' + type(exc).__name__
            return result(False, error=error)


class NativeStageAdapter(NativeStageExecutor):
    """Compatibility product entry for callers that have not prepared their inputs."""

    def run(self, *, prompt, config, mode, instructions=None, model_profile=None, output_schema=None, on_event=None):
        if instructions is None:
            from .stage_input import prepare_stage_input
            prepared = prepare_stage_input(prompt, output_schema=output_schema)
            prompt, instructions = prepared.prompt, prepared.instructions
        return super().run(prompt=prompt, instructions=instructions, config=config, mode=mode,
                           model_profile=model_profile, output_schema=output_schema, on_event=on_event)
