"""Provider mock: chỉ trả lời từ cache/fixture đã ghi (không gọi mạng). Dùng cho test và chạy lại offline.

Thiếu fixture -> LLMError (để test phát hiện prompt thay đổi).
"""
from __future__ import annotations

from .base import JsonLLM, LLMError, LLMSettings


class MockLLM(JsonLLM):
    name = "mock"

    def __init__(self, settings: LLMSettings):
        super().__init__(settings)
        if not self.cache_dir and not settings.legacy_cache_dir:
            raise LLMError("mock: cần llm.cache_dir hoặc llm.legacy_cache_dir chứa fixture")

    def _call(self, messages, max_tokens, temperature, json_mode):
        user = messages[-1]["content"]
        head = user if isinstance(user, str) else user[0].get("text", "")
        raise LLMError(f"mock: không có fixture cho yêu cầu (model={self.s.model}, max_tokens={max_tokens}): {head[:120]!r}")
