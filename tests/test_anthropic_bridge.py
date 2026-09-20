from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from lg_cli.anthropic_bridge import (
    AnthropicBridge,
    ProtocolError,
    translate_request,
    translate_stream,
)
from lg_cli.core_adapter import CodexExecAdapter

from tests.helpers import make_config


def message_events(block=None, delta=None, stop="end_turn"):
    yield {
        "type": "message_start",
        "message": {"usage": {"input_tokens": 10, "output_tokens": 0}},
    }
    yield {
        "type": "content_block_start",
        "index": 0,
        "content_block": block or {"type": "text", "text": ""},
    }
    yield {
        "type": "content_block_delta",
        "index": 0,
        "delta": delta or {"type": "text_delta", "text": "LG bridge ready"},
    }
    yield {"type": "content_block_stop", "index": 0}
    yield {
        "type": "message_delta",
        "delta": {"stop_reason": stop},
        "usage": {"output_tokens": 3},
    }
    yield {"type": "message_stop"}


class BridgeTests(unittest.TestCase):
    def test_official_sdk_sends_anthropic_request(self):
        import httpx

        requests = []

        def upstream(request):
            requests.append(request)
            events = list(message_events())
            events[0]["message"].update(
                id="msg_test",
                type="message",
                role="assistant",
                content=[],
                model="deepseek-flash",
                stop_reason=None,
                stop_sequence=None,
            )
            body = "".join(
                f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events
            )
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, text=body
            )

        with tempfile.TemporaryDirectory() as raw:
            config = replace(
                make_config(Path(raw)),
                provider="deepseek-anthropic",
                default_model="deepseek-flash",
                base_url="https://api.deepseek.com/anthropic",
            )
            with (
                patch(
                    "anthropic.DefaultHttpxClient",
                    return_value=httpx.Client(transport=httpx.MockTransport(upstream)),
                ),
                AnthropicBridge(config) as bridge,
            ):
                payload, mapping = translate_request(
                    {"model": config.default_model, "input": "hello"}, 100
                )
                result = list(translate_stream(bridge.events(payload), mapping))
            self.assertEqual(
                str(requests[0].url), "https://api.deepseek.com/anthropic/v1/messages"
            )
            self.assertEqual(requests[0].headers["x-api-key"], "test-key")
            self.assertEqual(result[-1]["type"], "response.completed")

    def test_text_and_usage_stream(self):
        events = list(translate_stream(message_events(), {}))
        self.assertEqual(events[0]["type"], "response.created")
        self.assertEqual(events[2]["delta"], "LG bridge ready")
        self.assertEqual(events[-1]["response"]["usage"]["total_tokens"], 13)

    def test_namespaced_tool_roundtrip(self):
        request = {
            "model": "test",
            "input": [
                {"role": "developer", "content": "writing instructions"},
                {"role": "user", "content": "read chapter"},
                {
                    "type": "function_call",
                    "name": "read",
                    "namespace": "story",
                    "call_id": "call_1",
                    "arguments": '{"chapter":3}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "chapter text",
                },
            ],
            "tools": [
                {
                    "type": "namespace",
                    "name": "story",
                    "tools": [
                        {
                            "type": "function",
                            "name": "read",
                            "parameters": {"type": "object"},
                        }
                    ],
                }
            ],
        }
        payload, mapping = translate_request(request, 4096)
        self.assertEqual(payload["messages"][-1]["content"][0]["tool_use_id"], "call_1")
        self.assertEqual(payload["messages"][-2]["content"][0]["name"], "lg_tool_0")
        events = list(
            translate_stream(
                message_events(
                    {
                        "type": "tool_use",
                        "id": "call_2",
                        "name": "lg_tool_0",
                        "input": {},
                    },
                    {"type": "input_json_delta", "partial_json": '{"chapter":4}'},
                    "tool_use",
                ),
                mapping,
            )
        )
        item = events[-1]["response"]["output"][0]
        self.assertEqual(item["namespace"], "story")
        self.assertEqual(item["call_id"], "call_2")
        self.assertEqual(json.loads(item["arguments"]), {"chapter": 4})

    def test_custom_tool_and_structured_output(self):
        for kind in ("custom", "structured"):
            with self.subTest(kind=kind):
                request = {"model": "test", "input": "write"}
                if kind == "custom":
                    request["tools"] = [{"type": "custom", "name": "edit"}]
                    args = {"input": "replace manuscript"}
                else:
                    request["text"] = {
                        "format": {"type": "json_schema", "schema": {"type": "object"}}
                    }
                    args = {"chapter": "text"}
                payload, mapping = translate_request(request, 1024)
                name = payload["tools"][0]["name"]
                events = list(
                    translate_stream(
                        message_events(
                            {"type": "tool_use", "id": "c", "name": name, "input": {}},
                            {
                                "type": "input_json_delta",
                                "partial_json": json.dumps(args),
                            },
                            "tool_use",
                        ),
                        mapping,
                    )
                )
                item = events[-1]["response"]["output"][0]
                if kind == "custom":
                    self.assertEqual(item["input"], args["input"])
                else:
                    self.assertEqual(json.loads(item["content"][0]["text"]), args)

    def test_truncation_and_unsupported_content_fail(self):
        for events in (list(message_events())[:-1], message_events(stop="max_tokens")):
            with self.assertRaises(ProtocolError):
                list(translate_stream(events, {}))
        for request in (
            {
                "model": "x",
                "input": [{"type": "reasoning", "encrypted_content": "opaque"}],
            },
            {"model": "x", "input": "x", "tools": [{"type": "web_search"}]},
            {"model": "x", "input": "x", "previous_response_id": "old"},
        ):
            with self.assertRaises(ProtocolError):
                translate_request(request, 100)

    def test_http_auth_and_stream(self):
        with (
            tempfile.TemporaryDirectory() as raw,
            AnthropicBridge(
                make_config(Path(raw)), stream_factory=lambda _: message_events()
            ) as bridge,
        ):
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            req = urllib.request.Request(bridge.base_url + "/responses", data=b"{}")
            with self.assertRaises(urllib.error.HTTPError) as error:
                opener.open(req)
            self.assertEqual(error.exception.code, 401)
            req = urllib.request.Request(
                bridge.base_url + "/responses",
                data=json.dumps(
                    {"model": "test", "input": "hello", "stream": True}
                ).encode(),
                headers={"Authorization": "Bearer " + bridge.token},
            )
            with opener.open(req) as response:
                output = response.read().decode()
            self.assertIn("response.completed", output)
            self.assertNotIn(bridge.token, output)

    @unittest.skipUnless(
        os.environ.get("LG_TEST_CODEX_BINARY"),
        "set LG_TEST_CODEX_BINARY for native integration",
    )
    def test_real_codex_through_bridge(self):
        calls = []

        def stream(payload):
            calls.append(payload)
            yield from message_events()

        with tempfile.TemporaryDirectory() as raw:
            config = replace(
                make_config(Path(raw)),
                provider="deepseek-anthropic",
                default_model="deepseek-flash",
                base_url="https://api.deepseek.com/anthropic",
            )
            factory = lambda cfg: AnthropicBridge(cfg, stream_factory=stream)
            with (
                patch.dict(
                    os.environ,
                    {
                        "LG_CODEX_COMMAND": os.environ["LG_TEST_CODEX_BINARY"],
                        "LG_CODEX_RUNTIME_MANIFEST": "",
                    },
                ),
                patch("lg_cli.anthropic_bridge.AnthropicBridge", side_effect=factory),
            ):
                result = CodexExecAdapter(Path(raw) / "missing").run(
                    prompt="Say hello", config=config, mode="chat"
                )
            self.assertTrue(
                result.ok, f"{result.error}\n{result.stderr}\n{result.stdout}"
            )
            self.assertEqual(result.output_text, "LG bridge ready")
            self.assertEqual(len(calls), 1)

    @unittest.skipUnless(
        os.environ.get("LG_TEST_CODEX_BINARY"),
        "set LG_TEST_CODEX_BINARY for native integration",
    )
    def test_real_codex_tool_result_and_schema(self):
        for structured in (False, True):
            with (
                self.subTest(structured=structured),
                tempfile.TemporaryDirectory() as raw,
            ):
                calls = []

                def stream(payload, calls=calls, structured=structured):
                    calls.append(payload)
                    if structured:
                        tool = next(
                            t
                            for t in payload["tools"]
                            if t["name"] == "lg_structured_output"
                        )
                        args = {"chapter": "new chapter"}
                    elif len(calls) == 1:
                        tool = next(
                            t
                            for t in payload["tools"]
                            if "cmd" in t["input_schema"].get("properties", {})
                        )
                        args = {"cmd": "printf LG_TOOL_OK", "max_output_tokens": 100}
                    else:
                        results = [
                            part
                            for msg in payload["messages"]
                            for part in msg["content"]
                            if part["type"] == "tool_result"
                        ]
                        self.assertTrue(results)
                        self.assertEqual(results[-1]["tool_use_id"], "call_plan")
                        yield from message_events()
                        return
                    yield from message_events(
                        {
                            "type": "tool_use",
                            "id": "call_plan",
                            "name": tool["name"],
                            "input": {},
                        },
                        {"type": "input_json_delta", "partial_json": json.dumps(args)},
                        "tool_use",
                    )

                config = replace(
                    make_config(Path(raw)),
                    provider="deepseek-anthropic",
                    default_model="deepseek-flash",
                    base_url="https://api.deepseek.com/anthropic",
                    enable_shell=not structured,
                )
                schema = Path(raw) / "schema.json"
                schema.write_text(
                    json.dumps(
                        {
                            "type": "object",
                            "properties": {"chapter": {"type": "string"}},
                            "required": ["chapter"],
                            "additionalProperties": False,
                        }
                    )
                )
                factory = lambda cfg: AnthropicBridge(cfg, stream_factory=stream)
                with (
                    patch.dict(
                        os.environ,
                        {
                            "LG_CODEX_COMMAND": os.environ["LG_TEST_CODEX_BINARY"],
                            "LG_CODEX_RUNTIME_MANIFEST": "",
                        },
                    ),
                    patch(
                        "lg_cli.anthropic_bridge.AnthropicBridge", side_effect=factory
                    ),
                ):
                    result = CodexExecAdapter(Path(raw) / "missing").run(
                        prompt="Plan a chapter",
                        config=config,
                        mode="outline",
                        output_schema=schema if structured else None,
                    )
                self.assertTrue(
                    result.ok,
                    f"{result.error}\n{result.stdout}\n{[[(t['name'], list(t['input_schema'].get('properties', {}))) for t in p.get('tools', [])] for p in calls]}",
                )
                if structured:
                    self.assertEqual(
                        json.loads(result.output_text), {"chapter": "new chapter"}
                    )
                else:
                    self.assertEqual(len(calls), 2)

    def test_upstream_error_is_redacted(self):
        def stream(_):
            raise RuntimeError("a-secret-key and private manuscript")

        with (
            tempfile.TemporaryDirectory() as raw,
            AnthropicBridge(make_config(Path(raw)), stream_factory=stream) as bridge,
        ):
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            req = urllib.request.Request(
                bridge.base_url + "/responses",
                data=json.dumps(
                    {"model": "test", "input": "hello", "stream": True}
                ).encode(),
                headers={"Authorization": "Bearer " + bridge.token},
            )
            with opener.open(req) as response:
                output = response.read().decode()
            self.assertIn("response.failed", output)
            self.assertNotIn("a-secret-key", output)
            self.assertNotIn("private manuscript", output)

    @unittest.skipUnless(
        os.environ.get("LG_TEST_CODEX_BINARY"),
        "set LG_TEST_CODEX_BINARY for native integration",
    )
    def test_real_codex_uses_writing_mcp(self):
        from lg_cli.native import writing_args
        from lg_cli.project_store import ProjectStore

        calls = []

        def stream(payload):
            calls.append(payload)
            if len(calls) == 1:
                tool = next(
                    t
                    for t in payload["tools"]
                    if "shared project progress" in t.get("description", "")
                )
                yield from message_events(
                    {
                        "type": "tool_use",
                        "id": "call_memory",
                        "name": tool["name"],
                        "input": {},
                    },
                    {"type": "input_json_delta", "partial_json": "{}"},
                    "tool_use",
                )
            else:
                results = [
                    p
                    for m in payload["messages"]
                    for p in m["content"]
                    if p["type"] == "tool_result"
                ]
                self.assertIn("MCP Test Novel", json.dumps(results))
                yield from message_events()

        with (
            tempfile.TemporaryDirectory() as raw,
            patch.dict(os.environ, {"LITERARYGIANT_REGISTRY_HOME": raw}),
        ):
            config = replace(
                make_config(Path(raw)),
                provider="deepseek-anthropic",
                default_model="deepseek-flash",
                base_url="https://api.deepseek.com/anthropic",
            )
            ProjectStore(Path(raw)).initialize(name="MCP Test Novel")

            class WritingBridge(AnthropicBridge):
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
                    "lg_cli.anthropic_bridge.AnthropicBridge",
                    side_effect=lambda cfg: WritingBridge(cfg, stream_factory=stream),
                ),
            ):
                result = CodexExecAdapter(Path(raw) / "missing").run(
                    prompt="Read my project memory", config=config, mode="chat"
                )
            self.assertTrue(
                result.ok,
                f"{result.error}\n{result.stdout}\n{result.stderr}\n{[[t.get('description', '')[:100] for t in p.get('tools', [])] for p in calls]}",
            )
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
