import asyncio
import json
import unittest
from unittest.mock import patch

import support  # Initialize the repository import path for standalone discovery.
import httpx
from app import llm_client
from app.errors import AppError
from app.extraction import ModelEvidenceRepair, ModelExtraction

CONFIG = llm_client.LLMConfig("azure", "https://test.services.ai.azure.com/openai/v1", "deployment-name", "test-secret")


class TestModelTransport(unittest.TestCase):
    def test_provider_schema_enforces_five_type_subtype_combinations(self):
        schema = ModelExtraction.model_json_schema()
        branches = schema['properties']['info_units']['items']['anyOf']
        self.assertEqual(len(branches), 5)
        actual = {}
        for branch in branches:
            definition = schema['$defs'][branch['$ref'].split('/')[-1]]
            fields = definition['properties']
            subtype = fields['subtype']
            if 'enum' in subtype:
                values = set(subtype['enum'])
            elif 'const' in subtype:
                values = {subtype['const']}
            elif subtype.get('type') == 'null':
                values = {None}
            else:
                values = set()
                for item in subtype['anyOf']:
                    if 'enum' in item:
                        values.update(item['enum'])
                    elif 'const' in item:
                        values.add(item['const'])
                    elif item.get('type') == 'null':
                        values.add(None)
            actual[fields['type']['const']] = values
        self.assertEqual(actual, {
            'entity': {'person','organization','object','location'},
            'event': {'reported','documented',None},
            'state': {None},
            'rule': {None},
            'position': {None},
        })

    def test_provider_schema_references_do_not_have_unsupported_sibling_keywords(self):
        def inspect(value):
            if isinstance(value, dict):
                if '$ref' in value:
                    self.assertEqual(set(value), {'$ref'}, 'Azure rejects description siblings next to $ref')
                for child in value.values():
                    inspect(child)
            elif isinstance(value, list):
                for child in value:
                    inspect(child)
        inspect(ModelExtraction.model_json_schema())
        inspect(ModelEvidenceRepair.model_json_schema())

    def call(self, handler):
        real_client = httpx.AsyncClient
        with patch.object(llm_client.httpx, "AsyncClient", side_effect=lambda **kwargs:
                          real_client(transport=httpx.MockTransport(handler), **kwargs)):
            return asyncio.run(llm_client.call_structured(CONFIG, "prompt", {"files": []}, ModelExtraction.model_json_schema()))

    def test_request_uses_configured_model_strict_json_schema_and_one_completion(self):
        calls = []
        def handler(request):
            calls.append(request)
            body = json.loads(request.content)
            self.assertEqual(str(request.url), CONFIG.base_url + "/chat/completions")
            self.assertEqual(body["model"], "deployment-name")
            self.assertTrue(body["response_format"]["json_schema"]["strict"])
            self.assertEqual(request.headers["api-key"], "test-secret")
            self.assertNotIn("test-secret", request.content.decode())
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]})
        reply = self.call(handler)
        self.assertEqual(reply.content, "{}")
        self.assertEqual(len(calls), 1)

    def test_http_errors_are_not_retried_or_downgraded(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(429, text="rate limit")
        with self.assertRaises(llm_client.LLMFailure) as caught:
            self.call(handler)
        self.assertEqual(caught.exception.code, "llm_call_failed")
        self.assertEqual(caught.exception.response, "rate limit")
        self.assertEqual(len(calls), 1)

    def test_timeout_and_transport_failure_are_explicit(self):
        for exception, code in [(httpx.ReadTimeout("timeout"), "llm_timeout"),
                                (httpx.ConnectError("connection"), "llm_call_failed")]:
            def handler(request):
                raise exception
            with self.subTest(code=code), self.assertRaises(llm_client.LLMFailure) as caught:
                self.call(handler)
            self.assertEqual(caught.exception.code, code)

    def test_refusal_truncation_empty_and_malformed_envelopes_fail(self):
        values = [({"choices": [{"finish_reason": "stop", "message": {"refusal": "refused"}}]}, "llm_refused"),
                  ({"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}, "llm_incomplete"),
                  ({"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}, "llm_response_invalid"),
                  ({"choices": []}, "llm_response_invalid"), ({"choices": [1]}, "llm_response_invalid"),
                  ({"choices": [{"message": []}]}, "llm_response_invalid")]
        for body, code in values:
            with self.subTest(code=code, body=body), self.assertRaises(llm_client.LLMFailure) as caught:
                self.call(lambda request: httpx.Response(200, json=body))
            self.assertEqual(caught.exception.code, code)

    def test_missing_or_partial_configuration_does_not_choose_a_default_model(self):
        for values in ({}, {"AZURE_OPENAI_API_KEY": "x"},
                       {"OPENAI_API_KEY": "x", "OPENAI_BASE_URL": "https://example.com/v1"}):
            with self.subTest(keys=list(values)), patch.object(llm_client, "configuration_values", return_value=values):
                with self.assertRaises(AppError) as caught:
                    llm_client.load_config()
                self.assertEqual(caught.exception.status_code, 503)

    def test_azure_project_endpoint_is_normalized_to_inference_route(self):
        values = {"AZURE_OPENAI_ENDPOINT": "https://example.services.ai.azure.com/api/projects/project-name",
                  "AZURE_OPENAI_MODEL": "user-deployment", "AZURE_OPENAI_API_KEY": "secret"}
        with patch.object(llm_client, "configuration_values", return_value=values):
            config = llm_client.load_config()
        self.assertEqual(config.base_url, "https://example.services.ai.azure.com/openai/v1")
        self.assertEqual(config.model, "user-deployment")
        self.assertNotIn("secret", repr(config))
        self.assertNotIn("secret", json.dumps(config.public_metadata()))

    def test_redaction_preserves_ordinary_token_limits_and_business_amounts(self):
        value = {"raw": "key=test-secret; amount=20000", "max_completion_tokens": 20000}
        clean = llm_client.redact(value, {"AZURE_OPENAI_API_KEY": "test-secret", "LLM_MAX_COMPLETION_TOKENS": "20000"})
        self.assertEqual(clean, {"raw": "key=[REDACTED]; amount=20000", "max_completion_tokens": 20000})


if __name__ == "__main__":
    unittest.main()
