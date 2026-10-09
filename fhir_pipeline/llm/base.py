"""Giao diện LLM dùng chung cho toàn pipeline.

Pipeline chỉ cần MỘT thao tác: `complete_json(system, user, images=None, ...) -> dict`.
Lớp `JsonLLM` lo phần chung (cache theo hash yêu cầu, parse JSON "lỏng", nhờ model sửa JSON hỏng, lưu raw để chẩn đoán);
mỗi nhà cung cấp chỉ cài `_call(messages, max_tokens, temperature, json_mode) -> (text, usage)`.

Đổi model/provider = đổi config, không đụng code nghiệp vụ.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("fhir_pipeline.llm")

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class LLMError(RuntimeError):
    pass


def parse_json_loose(text: str | None):
    """Lấy JSON object/array đầu tiên trong text (model đôi khi thêm lời dẫn / markdown)."""
    if text is None:
        raise ValueError("empty response")
    m = _FENCE.search(text)
    if m:
        text = m.group(1)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError(f"no JSON in response: {text[:200]}")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return json.loads(text[start: i + 1])
    raise ValueError("unbalanced JSON in response")


@dataclass
class LLMSettings:
    provider: str = "openai_compat"          # openai_compat | anthropic | mock
    model: str = "glm53-flash"
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = "LLM_API_KEY"
    json_mode: str = "response_format"       # response_format | prefill | none
    extra_body: dict = field(default_factory=dict)   # vd {"chat_template_kwargs": {"enable_thinking": false}}
    timeout: float = 900.0
    max_tokens_default: int = 16000
    temperature: float = 0.0
    cache_dir: str | None = "output/_llm_cache"
    legacy_cache_dir: str | None = None      # cache của work/common.py (hash theo payload OpenAI) - tái dùng kết quả đã có
    record_dir: str | None = None            # ghi thêm bản sao yêu cầu/đáp ứng (để làm fixture cho mock)


class JsonLLM:
    """Lớp nền: cache + parse + sửa JSON. Provider kế thừa và cài `_call`."""

    name = "base"

    def __init__(self, settings: LLMSettings):
        self.s = settings
        self.cache_dir = Path(settings.cache_dir) if settings.cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.record_dir = Path(settings.record_dir) if settings.record_dir else None
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
        self.stats = {"calls": 0, "cache_hits": 0, "prompt_tokens": 0, "completion_tokens": 0, "repairs": 0}

    # ------------------------------------------------------------ API công khai
    def complete_json(self, system: str, user: str, *, images: list[str] | None = None, max_tokens: int | None = None,
                      temperature: float | None = None, cache: bool = True) -> Any:
        max_tokens = max_tokens or self.s.max_tokens_default
        temperature = self.s.temperature if temperature is None else temperature
        req = {"model": self.s.model, "system": system, "user": user, "images": [hashlib.sha256(b.encode()).hexdigest()[:16] for b in (images or [])],
               "max_tokens": max_tokens, "temperature": temperature, "json_mode": self.s.json_mode}
        key = hashlib.sha256(json.dumps(req, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
        cached = self._cache_get(key) if cache else None
        if cached is None and cache:
            cached = self._legacy_lookup(system, user, images, max_tokens, temperature)
        if cached is not None:
            self.stats["cache_hits"] += 1
            if self.record_dir and not (self.record_dir / f"{key}.json").exists():  # ghi fixture kể cả khi trúng cache
                (self.record_dir / f"{key}.json").write_text(json.dumps({"request": req, "response": cached}, ensure_ascii=False, indent=1), encoding="utf-8")
            if cache and self.cache_dir and not (self.cache_dir / f"{key}.json").exists():
                self._cache_put(key, {"request": req, "response": cached})
            return cached["parsed"]
        messages = self._messages(system, user, images)
        text, usage = self._call(messages, max_tokens, temperature, self.s.json_mode)
        self.stats["calls"] += 1
        self.stats["prompt_tokens"] += (usage or {}).get("prompt_tokens", 0) or 0
        self.stats["completion_tokens"] += (usage or {}).get("completion_tokens", 0) or 0
        try:
            parsed = parse_json_loose(text)
        except Exception as e:  # noqa: BLE001
            log.warning("JSON hỏng (%s) -> nhờ model sửa", e)
            if self.cache_dir:
                (self.cache_dir / f"failed_{key}.txt").write_text(text or "", encoding="utf-8")
            parsed = self._repair(text, max_tokens)
            self.stats["repairs"] += 1
        entry = {"request": req, "response": {"text": text, "usage": usage, "parsed": parsed}}
        if cache:
            self._cache_put(key, entry)
        if self.record_dir:
            (self.record_dir / f"{key}.json").write_text(json.dumps(entry, ensure_ascii=False, indent=1), encoding="utf-8")
        return parsed

    # ------------------------------------------------------------ provider cài
    def _call(self, messages: list[dict], max_tokens: int, temperature: float, json_mode: str) -> tuple[str, dict | None]:
        raise NotImplementedError

    # ------------------------------------------------------------ tiện ích
    @staticmethod
    def _messages(system: str, user: str, images: list[str] | None) -> list[dict]:
        content: Any = user
        if images:
            content = [{"type": "text", "text": user}] + [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in images]
        return [{"role": "system", "content": system}, {"role": "user", "content": content}]

    def _repair(self, text: str, max_tokens: int):
        msgs = [{"role": "system", "content": "Bạn nhận một chuỗi JSON bị lỗi cú pháp. Trả về DUY NHẤT JSON hợp lệ, giữ nguyên toàn bộ khoá và giá trị, không thêm bớt nội dung."},
                {"role": "user", "content": text or ""}]
        fixed, _ = self._call(msgs, max_tokens, 0.0, self.s.json_mode)
        return parse_json_loose(fixed)

    def _cache_get(self, key: str):
        if not self.cache_dir:
            return None
        p = self.cache_dir / f"{key}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))["response"]
        return None

    def _cache_put(self, key: str, entry: dict):
        if self.cache_dir:
            (self.cache_dir / f"{key}.json").write_text(json.dumps(entry, ensure_ascii=False, indent=1), encoding="utf-8")

    def _legacy_lookup(self, system, user, images, max_tokens, temperature):
        """Tái dùng cache của bước làm tay (work/common.py): hash sha256 của payload OpenAI."""
        if not self.s.legacy_cache_dir:
            return None
        payload = {"model": self.s.model, "temperature": temperature, "max_tokens": max_tokens,
                   "messages": self._messages(system, user, images), "chat_template_kwargs": {"enable_thinking": False}}
        if self.s.json_mode == "response_format":
            payload["response_format"] = {"type": "json_object"}
        h = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
        p = Path(self.s.legacy_cache_dir) / f"{h}.json"
        if p.exists():
            j = json.loads(p.read_text(encoding="utf-8"))
            return {"text": j.get("raw"), "usage": j.get("usage"), "parsed": j["parsed"]}
        return None
