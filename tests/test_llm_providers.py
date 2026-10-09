"""Kiểm tra lớp LLM: cache/parse/repair và cách từng provider dựng payload (stub httpx, không gọi mạng)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fhir_pipeline.llm import LLMError, LLMSettings, make_llm, parse_json_loose


class _Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.text = data, status, json.dumps(data)

    def json(self):
        return self._d


class ParseTest(unittest.TestCase):
    def test_loose(self):
        self.assertEqual(parse_json_loose('Kết quả:\n```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(parse_json_loose('suy nghĩ... {"a": {"b": [1, 2]}} xong'), {"a": {"b": [1, 2]}})
        with self.assertRaises(ValueError):
            parse_json_loose("không có json")


class OpenAICompatTest(unittest.TestCase):
    def test_payload_and_cache(self):
        tmp = tempfile.mkdtemp()
        s = LLMSettings(provider="openai_compat", base_url="http://x/v1", api_key="k", model="m", cache_dir=tmp,
                        extra_body={"chat_template_kwargs": {"enable_thinking": False}})
        llm = make_llm(s)
        calls = []

        def fake_post(url, headers, json, timeout):
            calls.append((url, json))
            return _Resp({"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}})

        with mock.patch("fhir_pipeline.llm.openai_compat.httpx.post", fake_post):
            out1 = llm.complete_json("sys", "user", max_tokens=100)
            out2 = llm.complete_json("sys", "user", max_tokens=100)  # cache
        self.assertEqual(out1, {"ok": True}); self.assertEqual(out2, {"ok": True})
        self.assertEqual(len(calls), 1)
        url, payload = calls[0]
        self.assertEqual(url, "http://x/v1/chat/completions")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(payload["messages"][0], {"role": "system", "content": "sys"})
        self.assertEqual(llm.stats["calls"], 1); self.assertEqual(llm.stats["cache_hits"], 1)

    def test_repair(self):
        s = LLMSettings(provider="openai_compat", base_url="http://x", api_key="k", model="m", cache_dir=None)
        llm = make_llm(s)
        answers = iter(['{"a": 1,', '{"a": 1}'])

        def fake_post(url, headers, json, timeout):
            return _Resp({"choices": [{"message": {"content": next(answers)}}], "usage": {}})

        with mock.patch("fhir_pipeline.llm.openai_compat.httpx.post", fake_post):
            self.assertEqual(llm.complete_json("s", "u", cache=False), {"a": 1})
        self.assertEqual(llm.stats["repairs"], 1)


class AnthropicTest(unittest.TestCase):
    def test_payload_prefill_and_images(self):
        s = LLMSettings(provider="anthropic", api_key="k", model="claude-sonnet-5-5", json_mode="prefill", cache_dir=None)
        llm = make_llm(s)
        calls = []

        def fake_post(url, headers, json, timeout):
            calls.append((url, headers, json))
            return _Resp({"content": [{"type": "text", "text": '"ok": true}'}], "usage": {"input_tokens": 3, "output_tokens": 2}})

        with mock.patch("fhir_pipeline.llm.anthropic.httpx.post", fake_post):
            out = llm.complete_json("sys", "user", images=["QUJD"], max_tokens=50, cache=False)
        self.assertEqual(out, {"ok": True})
        url, headers, payload = calls[0]
        self.assertTrue(url.endswith("/v1/messages"))
        self.assertEqual(headers["x-api-key"], "k")
        self.assertEqual(payload["system"], "sys")
        self.assertEqual(payload["messages"][0]["content"][1]["type"], "image")
        self.assertEqual(payload["messages"][-1], {"role": "assistant", "content": "{"})


class MockTest(unittest.TestCase):
    def test_missing_fixture(self):
        tmp = tempfile.mkdtemp()
        llm = make_llm(LLMSettings(provider="mock", model="m", cache_dir=tmp))
        with self.assertRaises(LLMError):
            llm.complete_json("s", "u")

    def test_replay(self):
        tmp = tempfile.mkdtemp()
        real = make_llm(LLMSettings(provider="openai_compat", base_url="http://x", api_key="k", model="m", cache_dir=tmp))
        with mock.patch("fhir_pipeline.llm.openai_compat.httpx.post", lambda url, headers, json, timeout: _Resp({"choices": [{"message": {"content": '{"v": 7}'}}]})):
            real.complete_json("s", "u", max_tokens=10)
        replay = make_llm(LLMSettings(provider="mock", model="m", cache_dir=tmp))
        self.assertEqual(replay.complete_json("s", "u", max_tokens=10), {"v": 7})
        self.assertTrue(any(Path(tmp).glob("*.json")))


if __name__ == "__main__":
    unittest.main()
