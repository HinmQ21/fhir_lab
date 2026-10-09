from .base import JsonLLM, LLMError, LLMSettings, parse_json_loose


def make_llm(settings: LLMSettings) -> JsonLLM:
    """Factory theo settings.provider. Thêm provider mới = thêm một lớp con JsonLLM với `_call`."""
    p = (settings.provider or "openai_compat").lower()
    if p in ("openai_compat", "openai", "vllm"):
        from .openai_compat import OpenAICompatLLM
        return OpenAICompatLLM(settings)
    if p in ("anthropic", "claude"):
        from .anthropic import AnthropicLLM
        return AnthropicLLM(settings)
    if p == "mock":
        from .mock import MockLLM
        return MockLLM(settings)
    raise LLMError(f"provider không hỗ trợ: {settings.provider}")


__all__ = ["JsonLLM", "LLMError", "LLMSettings", "make_llm", "parse_json_loose"]
