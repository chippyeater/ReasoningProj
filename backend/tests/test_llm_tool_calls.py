"""Native tool protocol over MockTransport, without provider/network access."""
import asyncio
import json
import unittest
from unittest.mock import patch

import support
import httpx
from app import investigation_agent as agent, llm_client

CONFIG = llm_client.LLMConfig("azure", "https://test.openai.azure.com/openai/v1", "deployment-name", "test-secret")


def completion(message, finish="tool_calls"):
    return {"choices": [{"finish_reason": finish, "message": message}], "model": "deployment-name"}


def function_message():
    return {"content": None, "tool_calls": [{"id": "call-1", "type": "function", "function": {
        "name": "search_info_units", "arguments": '{"query":"押金","types":null,"limit":null}'}}]}


class TestToolTransport(unittest.TestCase):
    def call(self, handler, *, choice="auto", config=CONFIG, messages=None):
        real_client = httpx.AsyncClient
        with patch.object(llm_client.httpx, "AsyncClient", side_effect=lambda **kwargs:
                          real_client(transport=httpx.MockTransport(handler), **kwargs)):
            return asyncio.run(llm_client.call_tools(config, messages or [{"role": "system", "content": "prompt"}],
                               agent.tool_definitions(), agent.InvestigationResult.model_json_schema(), tool_choice=choice))

    def test_native_request_and_reply_use_strict_single_tool_protocol(self):
        calls = []

        def handler(request):
            calls.append(request)
            body = json.loads(request.content)
            self.assertEqual(str(request.url), CONFIG.base_url + "/chat/completions")
            self.assertEqual(request.headers["api-key"], CONFIG.api_key)
            self.assertEqual(body["model"], CONFIG.model)
            self.assertFalse(body["parallel_tool_calls"])
            self.assertEqual(body["tool_choice"], "auto")
            self.assertTrue(body["response_format"]["json_schema"]["strict"])
            self.assertEqual(body["response_format"]["json_schema"]["name"], "investigation_result")
            for definition in body["tools"]:
                function = definition["function"]
                self.assertTrue(function["strict"])
                parameters = function["parameters"]
                self.assertFalse(parameters["additionalProperties"])
                self.assertEqual(set(parameters["required"]), set(parameters["properties"]))
                self.assertNotIn("case_id", parameters["properties"])
            return httpx.Response(200, json=completion(function_message()))

        reply = self.call(handler)
        self.assertEqual(len(calls), 1)
        self.assertEqual(reply.message["role"], "assistant")
        self.assertEqual(reply.message["tool_calls"][0]["id"], "call-1")
        self.assertEqual(reply.content, "")

    def test_assistant_call_and_tool_result_are_preserved_in_followup_request(self):
        messages = [{"role": "system", "content": "prompt"},
                    {"role": "assistant", **function_message()},
                    {"role": "tool", "tool_call_id": "call-1", "content": '{"status":"empty"}'}]

        def handler(request):
            body = json.loads(request.content)
            self.assertEqual(body["messages"], messages)
            self.assertEqual(body["tool_choice"], "none")
            return httpx.Response(200, json=completion({"content": "{}"}, "stop"))

        reply = self.call(handler, choice="none", messages=messages)
        self.assertEqual(reply.content, "{}")
        self.assertNotIn("tool_calls", reply.message)

    def test_invalid_parallel_forbidden_or_malformed_tool_responses_fail(self):
        parallel = function_message()
        parallel["tool_calls"].append(parallel["tool_calls"][0])
        bad_calls = [parallel, {"tool_calls": []}, {"tool_calls": [None]},
                     {"tool_calls": [{"id": "x", "type": "function", "function": {"name": "x", "arguments": {}}}]}]
        for message in bad_calls:
            with self.subTest(message=message), self.assertRaises(llm_client.LLMFailure) as caught:
                self.call(lambda request: httpx.Response(200, json=completion(message)))
            self.assertEqual(caught.exception.code, "llm_response_invalid")
        with self.assertRaises(llm_client.LLMFailure) as caught:
            self.call(lambda request: httpx.Response(200, json=completion(function_message())), choice="none")
        self.assertEqual(caught.exception.code, "llm_response_invalid")

    def test_refusal_length_http_and_timeouts_are_not_retried(self):
        scenarios = [
            (200, completion({"refusal": "no"}, "stop"), "llm_refused"),
            (200, completion({"content": "partial"}, "length"), "llm_incomplete"),
            (200, {"choices": []}, "llm_response_invalid"),
            (200, completion({"content": ""}, "stop"), "llm_response_invalid"),
            (429, {"error": "rate limit"}, "llm_call_failed"),
        ]
        for status, body, code in scenarios:
            count = []

            def handler(request):
                count.append(request)
                return httpx.Response(status, json=body)

            with self.subTest(code=code), self.assertRaises(llm_client.LLMFailure) as caught:
                self.call(handler)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(len(count), 1)
        count = []

        def timeout(request):
            count.append(request)
            raise httpx.ReadTimeout("mock timeout")

        with self.assertRaises(llm_client.LLMFailure) as caught:
            self.call(timeout)
        self.assertEqual(caught.exception.code, "llm_timeout")
        self.assertEqual(len(count), 1)

    def test_other_providers_reuse_existing_authentication(self):
        for provider in ("openai", "github"):
            config = llm_client.LLMConfig(provider, "https://test.example/v1", "test-model", "test-secret")

            def handler(request):
                self.assertEqual(request.headers["Authorization"], "Bearer test-secret")
                self.assertNotIn("api-key", request.headers)
                if provider == "github":
                    self.assertEqual(request.headers["X-GitHub-Api-Version"], "2022-11-28")
                return httpx.Response(200, json=completion({"content": "{}"}, "stop"))

            with self.subTest(provider=provider):
                self.call(handler, config=config)


if __name__ == "__main__":
    unittest.main()
