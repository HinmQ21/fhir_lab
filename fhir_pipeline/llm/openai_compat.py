"""Provider OpenAI-compatible: vLLM, OpenAI, Azure OpenAI (qua base_url), Ollama, LM Studio, Gemini (OpenAI endpoint)...

json_mode:
  - response_format : gửi response_format={"type":"json_object"} (vLLM guided decoding, OpenAI JSON mode)
  - none            : chỉ dựa vào prompt
extra_body: trộn thêm vào payload (vd chat_template_kwargs.enable_thinking=false cho GLM/Qwen trên vLLM).
"""
from __future__ import annotations

import os

import httpx

from .base import JsonLLM, LLMError, LLMSettings


class OpenAICompatLLM(JsonLLM):
    name = "openai_compat"

    def __init__(self, settings: LLMSettings):
        super().__init__(settings)
        self.base = (settings.base_url or os.environ.get("LLM_BASE_URL") or "").rstrip("/")
        self.key = settings.api_key or (os.environ.get(settings.api_key_env) if settings.api_key_env else None) or os.environ.get("OPENAI_API_KEY")
        if not self.base:
            raise LLMError("openai_compat: thiếu base_url (config llm.base_url hoặc env LLM_BASE_URL)")
        if not self.key:
            raise LLMError(f"openai_compat: thiếu API key (env {settings.api_key_env or 'LLM_API_KEY'})")

    def _call(self, messages, max_tokens, temperature, json_mode):
        payload = {"model": self.s.model, "temperature": temperature, "max_tokens": max_tokens, "messages": messages}
        payload.update(self.s.extra_body or {})
        if json_mode == "response_format":
            payload["response_format"] = {"type": "json_object"}
        url = f"{self.base}/v1/chat/completions" if not self.base.endswith("/v1") else f"{self.base}/chat/completions"
        r = httpx.post(url, headers={"Authorization": f"Bearer {self.key}"}, json=payload, timeout=self.s.timeout)
        if r.status_code >= 400:
            raise LLMError(f"openai_compat HTTP {r.status_code}: {r.text[:300]}")
        j = r.json()
        msg = j["choices"][0]["message"]
        return msg.get("content") or "", j.get("usage")
