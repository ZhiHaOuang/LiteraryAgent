from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
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
    def test_glm_forced_thinking_is_scoped_to_supported_models(self):
        for provider, model, enabled in (
            ("glm", "glm-5.3", True), ("glm", "glm-5.3-flash", True),
            ("glm", "GLM-5.3-FlashX", True), ("glm", "glm-4.7", False),
            ("deepseek", "glm-5.3-flash", False),
        ):
            with self.subTest(provider=provider, model=model):
                calls = []

                def factory(payload, captured=calls):
                    captured.append(payload)
                    return message_events()
                config = replace(make_config(Path("/tmp/test-bridge")), provider=provider)
                bridge = AnthropicBridge(config, stream_factory=factory)
                list(bridge.response_events({"model": model, "input": "hello"}))
                if enabled:
                    self.assertEqual(calls[0]["thinking"], {"type": "enabled", "budget_tokens": 1024})
                    self.assertEqual(calls[0]["output_config"], {"effort": "low"})
                else:
                    self.assertEqual(calls[0]["thinking"], {"type": "disabled"})
                    self.assertNotIn("output_config", calls[0])

    def test_glm_insufficient_output_budget_fails_before_request(self):
        calls = []
        config = replace(make_config(Path("/tmp/test-bridge")), provider="glm", max_output_tokens=1024)
        bridge = AnthropicBridge(config, stream_factory=lambda payload: calls.append(payload))
        with self.assertRaisesRegex(ProtocolError, "1024-token thinking budget"):
            list(bridge.response_events({"model": "glm-5.3-flash", "input": "hello"}))
        self.assertEqual(calls, [])

    def test_token_limit_diagnostics_count_content_without_exposing_it(self):
        cases = [
            ({"type": "text", "text": "PRIVATE"},
             {"type": "text_delta", "text": "-TEXT"}, {}, "text_chars=12"),
            ({"type": "thinking", "thinking": "PRIVATE"},
             {"type": "thinking_delta", "thinking": "-REASONING"}, {}, "reasoning_chars=17"),
            ({"type": "tool_use", "id": "call", "name": "tool", "input": {}},
             {"type": "input_json_delta", "partial_json": '{"value":"PRIVATE"}'},
             {"tool": {"type": "function", "name": "tool", "namespace": None}}, "tool_json_delta_chars=19"),
        ]
        for block, delta, mapping, count in cases:
            with self.subTest(kind=block["type"]):
                with self.assertRaises(ProtocolError) as failure:
                    list(translate_stream(message_events(block, delta, stop="max_tokens"), mapping))
                message = str(failure.exception)
                self.assertIn("output_tokens=3", message)
                self.assertIn(count, message)
                self.assertNotIn("PRIVATE", message)

    def test_core_stream_timeout_matches_lg_request_budget(self):
        with tempfile.TemporaryDirectory() as raw:
            for seconds in (10, 300, 900):
                config = replace(make_config(Path(raw)), timeout_seconds=seconds)
                with AnthropicBridge(config, stream_factory=lambda _: message_events()) as bridge:
                    args = bridge.codex_args()
                    self.assertIn(
                        f"model_providers.lg_anthropic.stream_idle_timeout_ms={seconds * 1000}", args,
                    )
                    self.assertIn("model_providers.lg_anthropic.stream_max_retries=0", args)

    def test_plain_text_schema_failure_gets_one_private_bounded_repair(self):
        request = {"model": "test", "input": "hello", "text": {"format": {
            "type": "json_schema", "schema": {"type": "object", "properties": {
                "answer": {"type": "string"}
            }, "required": ["answer"], "additionalProperties": False}
        }}}
        for succeeds in (True, False):
            calls = []

            def factory(payload):
                calls.append(payload)
                if succeeds and len(calls) == 2:
                    return message_events(
                        {"type": "tool_use", "id": "c", "name": "lg_structured_output", "input": {}},
                        {"type": "input_json_delta", "partial_json": '{"answer":"kept"}'}, "tool_use")
                return message_events(delta={"type": "text_delta", "text": "PRIVATE-MANUSCRIPT not JSON"})

            bridge = AnthropicBridge(make_config(Path("/tmp/test-bridge")), stream_factory=factory)
            if succeeds:
                events = list(bridge.response_events(request))
                self.assertEqual(sum(e["type"] == "response.completed" for e in events), 1)
                self.assertNotIn("PRIVATE-MANUSCRIPT", json.dumps(events))
            else:
                with self.assertRaises(ProtocolError) as failure:
                    list(bridge.response_events(request))
                self.assertNotIn("PRIVATE-MANUSCRIPT", str(failure.exception))
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[-1]["messages"][-2]["content"], "PRIVATE-MANUSCRIPT not JSON")

    def test_schema_repair_is_bounded_and_emits_only_valid_response(self):
        request = {"model": "test", "input": "hello", "text": {"format": {
            "type": "json_schema", "schema": {"type": "object", "properties": {
                "answer": {"type": "string"}
            }, "required": ["answer"], "additionalProperties": False}
        }}}
        for repair_succeeds in (True, False):
            calls = []

            def factory(payload):
                calls.append(payload)
                content = '{"answer":"kept"}' if repair_succeeds and len(calls) == 2 else '{"answer":"kept","extra":true}'
                return message_events(
                    {"type": "tool_use", "id": "c", "name": "lg_structured_output", "input": {}},
                    {"type": "input_json_delta", "partial_json": content}, "tool_use")

            bridge = AnthropicBridge(make_config(Path("/tmp/test-bridge")), stream_factory=factory)
            if repair_succeeds:
                events = list(bridge.response_events(request))
                self.assertEqual(sum(e["type"] == "response.completed" for e in events), 1)
                self.assertNotIn("extra", json.dumps(events))
            else:
                with self.assertRaises(ProtocolError):
                    list(bridge.response_events(request))
            self.assertEqual(len(calls), 2)
            self.assertIn("Repair only the JSON structure", calls[-1]["messages"][-1]["content"])

    def test_malformed_structured_arguments_get_one_bounded_repair(self):
        request = {"model": "test", "input": "hello", "text": {"format": {
            "type": "json_schema", "schema": {"type": "object", "properties": {
                "answer": {"type": "string"}
            }, "required": ["answer"], "additionalProperties": False}
        }}}
        for invalid in ('"PRIVATE string"', '["PRIVATE array"]', '{"PRIVATE":'):
            for repaired in (True, False):
                with self.subTest(invalid=invalid, repaired=repaired):
                    calls = []

                    def factory(payload):
                        calls.append(payload)
                        content = '{"answer":"OK"}' if repaired and len(calls) == 2 else invalid
                        return message_events(
                            {"type": "tool_use", "id": "c", "name": "lg_structured_output", "input": {}},
                            {"type": "input_json_delta", "partial_json": content}, "tool_use")

                    bridge = AnthropicBridge(make_config(Path("/tmp/test-bridge")), stream_factory=factory)
                    if repaired:
                        events = list(bridge.response_events(request))
                        self.assertEqual(sum(e["type"] == "response.completed" for e in events), 1)
                        self.assertNotIn("PRIVATE", json.dumps(events))
                    else:
                        with self.assertRaises(ProtocolError) as failure:
                            list(bridge.response_events(request))
                        self.assertNotIn("PRIVATE", str(failure.exception))
                    self.assertEqual(len(calls), 2)

    def test_schema_failure_reports_field_without_private_value(self):
        request = {"model": "test", "input": "hello", "text": {"format": {
            "type": "json_schema", "schema": {"type": "object", "properties": {
                "answer": {"type": "array", "items": {"type": "string"}}
            }, "required": ["answer"]}
        }}}
        _, mapping = translate_request(request, 1024)
        events = message_events(
            {"type": "tool_use", "id": "c", "name": "lg_structured_output", "input": {}},
            {"type": "input_json_delta", "partial_json": '{"answer":"PRIVATE-MANUSCRIPT"}'},
            "tool_use",
        )
        with self.assertRaisesRegex(ProtocolError, r"\$\.answer: type") as failure:
            list(translate_stream(events, mapping))
        self.assertNotIn("PRIVATE-MANUSCRIPT", str(failure.exception))

    def test_reasoning_only_token_exhaustion_reports_budget(self):
        events = message_events({"type": "thinking", "thinking": ""}, {"type": "thinking_delta", "thinking": "private"}, stop="max_tokens")
        with self.assertRaisesRegex(ProtocolError, "output token limit"):
            list(translate_stream(events, {}))

    def test_thinking_blocks_do_not_leak_into_manuscript(self):
        reasoning = list(message_events(
            {"type": "thinking", "thinking": "private reasoning"},
            {"type": "thinking_delta", "thinking": "private delta"},
        ))
        reasoning.insert(3, {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "private signature"}})
        final = list(message_events())
        for event in final[1:-2]:
            event["index"] = 1
        events = list(translate_stream(reasoning[:-2] + final[1:], {}))
        self.assertEqual(len(events[-1]["response"]["output"]), 1)
        self.assertNotIn("private", json.dumps(events))
        self.assertEqual(events[-1]["response"]["output"][0]["content"][0]["text"], "LG bridge ready")
        with self.assertRaises(ProtocolError):
            list(translate_stream(reasoning, {}))

    def test_structured_result_after_progress_message(self):
        request = {"model": "test", "input": "hello", "text": {"format": {
            "type": "json_schema", "schema": {"type": "object", "required": ["answer"]}
        }}}
        _, mapping = translate_request(request, 1024)
        progress = list(message_events())
        final = list(message_events(
            {"type": "tool_use", "id": "c", "name": "lg_structured_output", "input": {}},
            {"type": "input_json_delta", "partial_json": '{"answer":"hello"}'},
            "tool_use",
        ))
        for event in final[1:-2]:
            event["index"] = 1
        events = list(translate_stream(progress[:-2] + final[1:], mapping))
        response = events[-1]["response"]
        self.assertEqual(len(response["output"]), 2)
        AnthropicBridge.validate_final_schema(request, response)
        response["output"][-1]["content"][0]["text"] = '{}'
        with self.assertRaises(ProtocolError):
            AnthropicBridge.validate_final_schema(request, response)

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
    def test_real_core_stepfun_request_has_lg_identity_without_bundled_harness(self):
        calls = []

        def stream(payload):
            calls.append(payload)
            yield from message_events()

        with tempfile.TemporaryDirectory() as raw:
            config = replace(
                make_config(Path(raw)),
                provider="stepfun",
                default_model="step-3.7-flash",
                base_url="https://api.stepfun.com",
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
            system = "\n".join(block.get("text", "") for block in calls[0].get("system", []))
            self.assertIn("LiteraryGiant", system)
            self.assertNotIn("Codex", system)
            self.assertNotIn("skills_instructions", system)
            self.assertNotIn("coding agent", system)
            self.assertIn("read-only", system)

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

    def test_rate_limit_error_is_actionable_without_leaking_body_or_headers(self):
        class RateLimitError(Exception):
            status_code = 429

        for retry_after in ("12", "private manuscript and a-secret-key", "999999999999999", None):
            with self.subTest(retry_after=retry_after), tempfile.TemporaryDirectory() as raw:
                def stream(_, header=retry_after):
                    error = RateLimitError("a-secret-key and private manuscript")
                    error.response = SimpleNamespace(headers={"retry-after": header})
                    raise error

                with AnthropicBridge(make_config(Path(raw)), stream_factory=stream) as bridge:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    req = urllib.request.Request(
                        bridge.base_url + "/responses",
                        data=json.dumps({"model": "test", "input": "hello", "stream": True}).encode(),
                        headers={"Authorization": "Bearer " + bridge.token},
                    )
                    with opener.open(req) as response:
                        output = response.read().decode()
                self.assertIn("HTTP 429 (rate limit or quota)", output)
                self.assertNotIn("check the protocol Base URL", output)
                self.assertNotIn("a-secret-key", output)
                self.assertNotIn("private manuscript", output)
                self.assertEqual("Retry-After: 12 seconds" in output, retry_after == "12")
                if retry_after != "12":
                    self.assertNotIn("Retry-After", output)

    def test_glm_insufficient_balance_is_not_misreported_as_transient_throttling(self):
        class ProviderError(Exception):
            status_code = 429

        for provider, code, expected in (("glm", "1113", True), ("glm", 1113, True),
                                         ("deepseek", "1113", False), ("glm", "private manuscript", False)):
            with self.subTest(provider=provider, code=code), tempfile.TemporaryDirectory() as raw:
                def stream(_, error_code=code):
                    error = ProviderError("a-secret-key and private manuscript")
                    error.body = {"error": {"code": error_code, "message": "a-secret-key and private manuscript"}}
                    raise error

                config = replace(make_config(Path(raw)), provider=provider)
                with AnthropicBridge(config, stream_factory=stream) as bridge:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    req = urllib.request.Request(
                        bridge.base_url + "/responses",
                        data=json.dumps({"model": "test", "input": "hello", "stream": True}).encode(),
                        headers={"Authorization": "Bearer " + bridge.token},
                    )
                    with opener.open(req) as response:
                        output = response.read().decode()
                self.assertEqual("code 1113" in output, expected)
                self.assertEqual("check provider billing and plan access" in output, expected)
                self.assertEqual("wait before retrying" in output, not expected)
                self.assertNotIn("a-secret-key", output)
                self.assertNotIn("private manuscript", output)

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
