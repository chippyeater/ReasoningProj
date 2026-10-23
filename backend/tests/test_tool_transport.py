"""Offline native function-call transport with optional structured final replies."""
import asyncio
import json
import unittest
from unittest.mock import patch

import support
import httpx
from app import llm_client

CONFIG = llm_client.LLMConfig("azure", "https://test.openai.azure.com/openai/v1", "mock", "test-secret")
TOOLS = [{"type": "function", "function": {"name": "read", "parameters": {
    "type": "object", "properties": {}, "required": [], "additionalProperties": False}, "strict": True}}]


class TestToolTransport(unittest.TestCase):
    def request(self, response):
        requests = []
        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=response)
        real_client = httpx.AsyncClient
        with patch.object(llm_client.httpx, "AsyncClient", side_effect=lambda **kwargs:
                real_client(transport=httpx.MockTransport(handle), **kwargs)):
            reply = asyncio.run(llm_client.call_tools(CONFIG, [{"role": "user", "content": "核对"}], TOOLS))
        return reply, requests

    def test_plain_final_reply_has_no_structured_response_format(self):
        reply, requests = self.request({"choices": [{"finish_reason": "stop", "message": {"content": "仍需核对计租时间。"}}]})
        self.assertEqual(reply.content, "仍需核对计租时间。")
        self.assertNotIn("response_format", requests[0])
        self.assertFalse(requests[0]["parallel_tool_calls"])
        self.assertEqual(len(requests), 1)

    def test_native_call_remains_structured_and_single(self):
        reply, _ = self.request({"choices": [{"finish_reason": "tool_calls", "message": {"content": None,
            "tool_calls": [{"id": "call", "type": "function", "function": {"name": "read", "arguments": "{}"}}]}}]})
        self.assertEqual(reply.message["tool_calls"][0]["function"]["name"], "read")
        with self.assertRaises(llm_client.LLMFailure):
            self.request({"choices": [{"finish_reason": "stop", "message": {"content": ""}}]})

    def test_explicit_schema_callers_keep_their_strict_contract(self):
        schema = {"type": "object", "properties": {}, "additionalProperties": False, "required": []}
        payload = llm_client.tool_request_payload(CONFIG, [], TOOLS, schema)
        self.assertEqual(payload["response_format"]["json_schema"]["schema"], schema)
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
