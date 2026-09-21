from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx
from lg_cli.anthropic_bridge import AnthropicBridge, ProtocolError, translate_stream
from lg_cli.core_adapter import CodexExecAdapter
from lg_cli.provider_transport import ResponsesBridge

from tests.helpers import make_config
from tests.test_anthropic_bridge import message_events


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


class ProviderTransportTests(unittest.TestCase):
    def test_glm_stepfun_sdk_requests_and_tool_stream(self):
        for provider, base_url in [
            ("glm", "https://open.bigmodel.cn/api/anthropic"),
            ("stepfun", "https://api.stepfun.com"),
        ]:
            requests = []

            def upstream(request, requests=requests, provider=provider):
                requests.append(request)
                events = list(
                    message_events(
                        {
                            "type": "tool_use",
                            "id": "call_1",
                            "name": "lg_tool_0",
                            "input": {},
                        },
                        {"type": "input_json_delta", "partial_json": '{"chapter":3}'},
                        "tool_use",
                    )
                )
                events[0]["message"].update(
                    id="msg_test",
                    type="message",
                    role="assistant",
                    content=[],
                    model="account-model",
                    stop_reason=None,
                )
                if provider == "stepfun":
                    events[-2]["stop_reason"] = events[-2].pop("delta")["stop_reason"]
                return httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, text=sse(events)
                )

            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as raw:
                config = replace(
                    make_config(Path(raw)),
                    provider=provider,
                    protocol="anthropic",
                    default_model="account-model",
                    base_url=base_url,
                )
                with (
                    patch(
                        "anthropic.DefaultHttpxClient",
                        return_value=httpx.Client(
                            transport=httpx.MockTransport(upstream)
                        ),
                    ),
                    AnthropicBridge(config) as bridge,
                ):
                    result = list(
                        bridge.response_events(
                            {
                                "model": "account-model",
                                "input": "read chapter",
                                "tools": [
                                    {
                                        "type": "function",
                                        "name": "read",
                                        "parameters": {"type": "object"},
                                    }
                                ],
                            }
                        )
                    )
                self.assertEqual(str(requests[0].url), base_url + "/v1/messages")
                self.assertEqual(requests[0].headers["x-api-key"], "test-key")
                payload = json.loads(requests[0].content)
                if provider == "stepfun":
                    self.assertNotIn("thinking", payload)
                    self.assertNotIn("tool_choice", payload)
                self.assertEqual(
                    result[-1]["response"]["output"][0]["call_id"], "call_1"
                )

    def test_stepfun_rejects_unsupported_tool_choice_before_request(self):
        with tempfile.TemporaryDirectory() as raw:
            bridge = AnthropicBridge(
                replace(make_config(Path(raw)), provider="stepfun"),
                stream_factory=lambda p: message_events(),
            )
            with self.assertRaises(ProtocolError):
                list(
                    bridge.response_events(
                        {"model": "test", "input": "hi", "tool_choice": "required"}
                    )
                )

    def test_structured_final_is_validated_even_without_forced_tool_choice(self):
        with tempfile.TemporaryDirectory() as raw:
            bridge = AnthropicBridge(
                replace(make_config(Path(raw)), provider="stepfun"),
                stream_factory=lambda p: message_events(),
            )
            with self.assertRaises(ProtocolError):
                list(
                    bridge.response_events(
                        {
                            "model": "test",
                            "input": "hi",
                            "text": {
                                "format": {
                                    "type": "json_schema",
                                    "schema": {"type": "object"},
                                }
                            },
                        }
                    )
                )

    def test_responses_http_request_and_stream(self):
        requests = []

        def upstream(request):
            requests.append(request)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=sse(translate_stream(message_events(), {})),
            )

        with tempfile.TemporaryDirectory() as raw:
            config = replace(
                make_config(Path(raw)),
                provider="responses-compatible",
                protocol="responses",
                base_url="http://127.0.0.1:8317/v1",
                default_model="account-model",
            )
            client = httpx.Client(transport=httpx.MockTransport(upstream))
            with (
                patch.object(ResponsesBridge, "open_client", return_value=client),
                ResponsesBridge(config) as bridge,
            ):
                result = list(
                    bridge.response_events(
                        {
                            "model": "account-model",
                            "input": "hello",
                            "stream": True,
                            "reasoning": {"effort": "none"},
                        }
                    )
                )
            self.assertEqual(str(requests[0].url), config.base_url + "/responses")
            self.assertEqual(requests[0].headers["Authorization"], "Bearer test-key")
            payload = json.loads(requests[0].content)
            self.assertFalse(payload["store"])
            self.assertNotIn("reasoning", payload)
            self.assertEqual(result[-1]["type"], "response.completed")

    def test_responses_failure_and_incomplete_stream_are_not_successful(self):
        with tempfile.TemporaryDirectory() as raw:
            bridge = ResponsesBridge(make_config(Path(raw)))
            for events in [
                [],
                [{"type": "response.failed", "response": {"error": "secret"}}],
                [{"type": "response.incomplete"}],
                [{"type": "response.completed", "response": {"status": "failed"}}],
            ]:
                with (
                    self.subTest(events=events),
                    self.assertRaises(ProtocolError) as error,
                ):
                    list(bridge.validated_events(events, {}))
                self.assertNotIn("secret", str(error.exception))

    def test_http_error_body_is_never_forwarded_and_wrong_model_never_sent(self):
        for status in (302, 401, 429, 500):
            seen = []

            def upstream(request, seen=seen, status=status):
                seen.append(request)
                return httpx.Response(
                    status,
                    headers={"Location": "https://other.example"},
                    text="secret-token and private manuscript",
                )

            with self.subTest(status=status), tempfile.TemporaryDirectory() as raw:
                config = replace(
                    make_config(Path(raw)),
                    protocol="responses",
                    default_model="allowed",
                    base_url="https://provider.example/v1",
                )
                with (
                    patch.object(
                        ResponsesBridge,
                        "open_client",
                        return_value=httpx.Client(
                            transport=httpx.MockTransport(upstream)
                        ),
                    ),
                    ResponsesBridge(config) as bridge,
                ):
                    opener = urllib.request.build_opener(
                        urllib.request.ProxyHandler({})
                    )
                    req = urllib.request.Request(
                        bridge.base_url + "/responses",
                        data=json.dumps(
                            {"model": "allowed", "input": "hi", "stream": True}
                        ).encode(),
                        headers={"Authorization": "Bearer " + bridge.token},
                    )
                    output = opener.open(req).read().decode()
                    self.assertIn("response.failed", output)
                    self.assertNotIn("secret-token", output)
                    self.assertNotIn("private manuscript", output)
                    req.data = json.dumps(
                        {"model": "wrong-model", "input": "hi", "stream": True}
                    ).encode()
                    with self.assertRaises(urllib.error.HTTPError):
                        opener.open(req)
                    self.assertEqual(len(seen), 1)

    @unittest.skipUnless(
        os.environ.get("LG_TEST_CODEX_BINARY"),
        "set LG_TEST_CODEX_BINARY for native integration",
    )
    def test_real_codex_responses_bridge_and_writing_mcp_roundtrip(self):
        from lg_cli.native import writing_args
        from lg_cli.project_store import ProjectStore

        calls = []

        def stream(payload):
            calls.append(payload)
            if len(calls) > 1:
                results = [
                    i
                    for i in payload["input"]
                    if i.get("type") == "function_call_output"
                ]
                self.assertTrue(results)
                self.assertEqual(results[-1]["call_id"], "call_memory")
                self.assertIn("Responses Test Novel", json.dumps(results))
                yield from translate_stream(message_events(), {})
                return
            found = []
            for tool in payload["tools"]:
                children = (
                    tool.get("tools", []) if tool.get("type") == "namespace" else [tool]
                )
                for child in children:
                    if "shared project progress" in child.get("description", ""):
                        found.append(
                            (
                                child,
                                tool.get("name")
                                if tool.get("type") == "namespace"
                                else None,
                            )
                        )
            self.assertTrue(found)
            tool, namespace = found[0]
            mapping = {
                "wire": {
                    "name": tool["name"],
                    "namespace": namespace,
                    "type": "function",
                }
            }
            yield from translate_stream(
                message_events(
                    {
                        "type": "tool_use",
                        "id": "call_memory",
                        "name": "wire",
                        "input": {},
                    },
                    {"type": "input_json_delta", "partial_json": "{}"},
                    "tool_use",
                ),
                mapping,
            )

        with (
            tempfile.TemporaryDirectory() as raw,
            patch.dict(os.environ, {"LITERARYGIANT_REGISTRY_HOME": ""}),
        ):
            config = replace(
                make_config(Path(raw)),
                protocol="responses",
                provider="responses-compatible",
                default_model="account-model",
                base_url="https://proxy.example/v1",
            )
            with patch.dict(os.environ, {"LITERARYGIANT_REGISTRY_HOME": raw}):
                ProjectStore(Path(raw)).initialize(name="Responses Test Novel")

            class WritingResponsesBridge(ResponsesBridge):
                def codex_args(self):
                    return super().codex_args() + writing_args(config)

            with (
                patch.dict(
                    os.environ,
                    {
                        "LG_CODEX_COMMAND": os.environ["LG_TEST_CODEX_BINARY"],
                        "LG_CODEX_RUNTIME_MANIFEST": "",
                    },
                ),
                patch(
                    "lg_cli.provider_transport.ResponsesBridge",
                    side_effect=lambda cfg: WritingResponsesBridge(
                        cfg, stream_factory=stream
                    ),
                ),
            ):
                result = CodexExecAdapter(Path(raw) / "missing").run(
                    prompt="Read project memory", config=config, mode="chat"
                )
            self.assertTrue(
                result.ok, f"{result.error}\n{result.stderr}\n{result.stdout}"
            )
            self.assertEqual(len(calls), 2)
            self.assertEqual(result.output_text, "LG bridge ready")
