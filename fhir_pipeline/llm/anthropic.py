"""Provider Anthropic Messages API (Claude). Chưa chạy thử trong lab (không có key) — cài theo tài liệu API.

json_mode:
  - prefill : thêm lượt assistant mở đầu bằng "{" để ép JSON (khuyến nghị)
  - none    : chỉ dựa vào prompt
Ảnh: content block {"type":"image","source":{"type":"base64","media_type":"image/png","data":...}}.
"""
from __future__ import annotations

import os

import httpx

from .base import JsonLLM, LLMError, LLMSettings


class AnthropicLLM(JsonLLM):
    name = "anthropic"

    def __init__(self, settings: LLMSettings):
        super().__init__(settings)
        self.base = (settings.base_url or "https://api.anthropic.com").rstrip("/")
        self.key = settings.api_key or (os.environ.get(settings.api_key_env) if settings.api_key_env else None) or os.environ.get("ANTHROPIC_API_KEY")
        if not self.key:
            raise LLMError("anthropic: thiếu API key (env ANTHROPIC_API_KEY)")
        if settings.model == "glm53-flash":  # model mặc định của lab không phải Claude
            self.s.model = "claude-sonnet-5-5"

    def _call(self, messages, max_tokens, temperature, json_mode):
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        user_msgs = []
        for m in messages:
            if m["role"] != "user":
                continue
            if isinstance(m["content"], str):
                user_msgs.append({"role": "user", "content": m["content"]})
            else:
                blocks = []
                for part in m["content"]:
                    if part.get("type") == "text":
                        blocks.append({"type": "text", "text": part["text"]})
                    elif part.get("type") == "image_url":
                        data = part["image_url"]["url"].split(",", 1)[1]
                        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}})
                user_msgs.append({"role": "user", "content": blocks})
        prefill = json_mode == "prefill"
        if prefill:
            user_msgs.append({"role": "assistant", "content": "{"})
        payload = {"model": self.s.model, "max_tokens": min(max_tokens, 64000), "temperature": temperature, "system": system, "messages": user_msgs}
        payload.update(self.s.extra_body or {})
        r = httpx.post(f"{self.base}/v1/messages", headers={"x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                       json=payload, timeout=self.s.timeout)
        if r.status_code >= 400:
            raise LLMError(f"anthropic HTTP {r.status_code}: {r.text[:300]}")
        j = r.json()
        text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
        if prefill:
            text = "{" + text
        u = j.get("usage") or {}
        return text, {"prompt_tokens": u.get("input_tokens"), "completion_tokens": u.get("output_tokens")}
