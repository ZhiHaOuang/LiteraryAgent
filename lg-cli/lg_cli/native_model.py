"""Async provider transport for the LG tool loop, reusing existing protocol validators."""
import uuid

import httpx
from httpx_sse import aconnect_sse

from .anthropic_bridge import ProtocolError, translate_request, translate_stream
from .provider_transport import ResponsesBridge
from .usage_ledger import UsageLedger


class NativeModel:
    def __init__(self, config):
        self.config = config

    async def generate(self, request, delta):
        config, usage, complete = self.config, {}, False
        ledger = UsageLedger(config.workspace)
        ledger.start_capture()
        try:
            if config.uses_responses:
                async with httpx.AsyncClient(timeout=config.timeout_seconds, follow_redirects=False, trust_env=False) as client:
                    payload = {**request, 'store': False, 'max_output_tokens': config.max_output_tokens}
                    async with aconnect_sse(client, 'POST', config.base_url.rstrip('/') + '/responses',
                        headers={'Authorization': 'Bearer ' + config.api_key}, json=payload) as stream:
                        if stream.response.status_code != 200:
                            raise ProtocolError(f'Native provider returned HTTP {stream.response.status_code}')
                        events = []
                        async for item in stream.aiter_sse():
                            if item.data == '[DONE]':
                                continue
                            event = item.json()
                            events.append(event)
                            usage.update((event.get('response') or {}).get('usage') or {})
                            if event.get('type') == 'response.output_text.delta':
                                delta(event.get('delta', ''))
                            if event.get('type') == 'response.completed':
                                break
                result = list(ResponsesBridge(config)._validated_events(events, request))[-1]['response']
            elif config.uses_anthropic:
                payload, mapping = translate_request(request, config.max_output_tokens)
                if config.provider == 'stepfun':
                    payload.pop('thinking', None)
                    payload.pop('tool_choice', None)
                if config.provider == 'glm' and request['model'].lower() in {'glm-5.3', 'glm-5.3-flash', 'glm-5.3-flashx'}:
                    if config.max_output_tokens <= 1024:
                        raise ProtocolError('GLM-5.3 requires max_output_tokens greater than 1024')
                    payload['thinking'] = {'type': 'enabled', 'budget_tokens': 1024}
                    payload['output_config'] = {'effort': 'low'}
                async with httpx.AsyncClient(timeout=config.timeout_seconds, follow_redirects=False, trust_env=False) as client:
                    async with aconnect_sse(client, 'POST', config.base_url.rstrip('/') + '/v1/messages',
                        headers={'x-api-key': config.api_key, 'anthropic-version': '2023-06-01'}, json=payload) as stream:
                        if stream.response.status_code != 200:
                            raise ProtocolError(f'Native provider returned HTTP {stream.response.status_code}')
                        events = []
                        async for item in stream.aiter_sse():
                            event = item.json()
                            events.append(event)
                            usage.update((event.get('message') or {}).get('usage') or event.get('usage') or {})
                            if (event.get('delta') or {}).get('type') == 'text_delta':
                                delta(event['delta'].get('text', ''))
                result = list(translate_stream(events, mapping))[-1]['response']
                ResponsesBridge.validate_final_schema(request, result)
            else:
                raise ProtocolError('Native runtime supports Anthropic or Responses API profiles')
            complete = True
            return result
        except ProtocolError:
            raise
        except Exception as exc:
            raise ProtocolError('Native provider request failed: ' + type(exc).__name__) from None
        finally:
            ledger.record(uuid.uuid4().hex, config.provider, request['model'], usage, partial=not complete)
