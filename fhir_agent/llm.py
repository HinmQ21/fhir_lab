"""Lớp LLM cắm được: OpenAI-compatible (vLLM/GLM, OpenAI, Azure, Ollama, LM Studio, Gemini qua /openai) và Gemini gốc.

Harness chỉ cần MỘT thao tác: `client.json(task, system, user) -> (dict, meta)`.
Lớp nền lo phần chung cho mọi nhà cung cấp:
  * thử lại có backoff khi 429/5xx/timeout, rồi chuyển sang model dự phòng (`fallback_models`);
  * parse JSON "lỏng" (bỏ ```fence```, lời dẫn), nhờ model sửa JSON hỏng một lần;
  * cache theo hash yêu cầu (chạy lại không tốn token), ghi trace từng lần gọi (không ghi nội dung);
  * bảo vệ quyền riêng tư: nếu profile là cloud, mọi văn bản gửi đi đi qua PrivacyGuard (che PII) và kết quả được trả lại PII.
Provider chỉ cài `_call(system, user, max_tokens, temperature, model) -> (text, usage)`.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .env import scrub

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


class LLMError(RuntimeError):
    pass


class QuotaExceeded(LLMError):
    """Hết hạn mức thanh toán/ngày: thử lại vô ích -> chuyển ngay sang model dự phòng."""


class Retryable(LLMError):
    pass


_TRAILING_COMMA = re.compile(r",\s*([}\]])")


def parse_json_loose(text: str | None):
    """Lấy JSON object/array đầu tiên trong text (model đôi khi thêm lời dẫn / markdown)."""
    if not text:
        raise ValueError("phản hồi rỗng")
    m = _FENCE.search(text)
    if m:
        text = m.group(1)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Model "nghĩ ra chữ" trước khi trả JSON có thể chứa {...} nhỏ trong lời dẫn -> lấy khối cân ngoặc LỚN NHẤT parse được.
    # Không quét trạng thái chuỗi ngoài JSON (dấu " trong lời dẫn không cân), chỉ đếm ngoặc từ mỗi '{' mở đầu dòng/sau khoảng trắng.
    cands = []
    for start in [m.start() for m in re.finditer(r"[{\[]", text)][:4000]:
        if cands and start < cands[-1][1]:
            continue                        # nằm trong khối đã lấy
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
                    cands.append((start, i + 1))
                    break
    if not cands:
        raise ValueError("không có JSON trong phản hồi hoặc JSON không cân ngoặc (có thể bị cắt do max_tokens)")
    for a, b in sorted(cands, key=lambda c: c[0] - c[1]):
        chunk = text[a:b]
        for t in (chunk, _TRAILING_COMMA.sub(r"\1", chunk)):   # {"a": [1, 2,]} — lỗi hay gặp ở model nhỏ
            try:
                return json.loads(t)
            except json.JSONDecodeError:
                pass
    raise ValueError("JSON không cân ngoặc (có thể bị cắt do max_tokens)")


@dataclass
class Profile:
    """Một cấu hình model trong config/agent.yaml (khối providers.<name>)."""
    name: str
    kind: str = "openai_compat"            # openai_compat | gemini | mock
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    fallback_models: list[str] = field(default_factory=list)
    privacy: str = "none"                  # none (endpoint nội bộ) | pseudonymize (cloud: che PII) | block
    json_mode: Any = "response_format"     # response_format|true | none|false   (openai_compat)
    extra_body: dict = field(default_factory=dict)      # trộn vào payload OpenAI (vd chat_template_kwargs)
    generation_config: dict = field(default_factory=dict)  # trộn vào generationConfig Gemini (vd thinkingConfig)
    temperature: float = 0.0
    max_tokens: int = 16000
    timeout: float = 600.0
    retries: int = 5
    concurrency: int = 4
    price_in: float = 0.0                  # USD / 1M token vào (ước tính chi phí; 0 = không tính)
    price_out: float = 0.0

    @classmethod
    def from_dict(cls, name: str, d: dict) -> "Profile":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(name=name, **known)


class LLMClient:
    name = "base"

    def __init__(self, profile: Profile, cache_dir: Path | None = None, trace=None, guard=None):
        self.p = profile
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.trace = trace            # callable(dict) hoặc None
        self.guard = guard            # PrivacyGuard hoặc None
        self.lock = threading.Lock()
        self.stats = {"calls": 0, "cache_hits": 0, "retries": 0, "fallbacks": 0, "json_repairs": 0, "prompt_tokens": 0, "completion_tokens": 0, "thought_tokens": 0}

    # ------------------------------------------------------------------ công khai
    def json(self, task: str, system: str, user: str, *, max_tokens: int | None = None, cache: bool = True) -> tuple[Any, dict]:
        """Gọi model, trả (JSON đã parse — đã trả lại PII nếu có che, meta)."""
        if self.guard is not None and self.p.privacy == "block":
            raise LLMError(f"profile {self.p.name} bị chặn bởi chính sách quyền riêng tư (privacy=block)")
        if self.guard is not None and self.p.privacy == "pseudonymize":
            system, user = self.guard.mask(system), self.guard.mask(user)
        max_tokens = max_tokens or self.p.max_tokens
        key = hashlib.sha256(json.dumps([self.p.kind, self.p.model, self.p.base_url, system, user, max_tokens, self.p.temperature,
                                         self.p.extra_body, self.p.generation_config, self.p.json_mode], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:32]
        meta = {"task": task, "provider": self.p.name, "model": self.p.model, "cache_key": key}
        hit = self._cache_get(key) if cache else None
        if hit is not None:
            with self.lock:
                self.stats["cache_hits"] += 1
            meta.update(cache_hit=True, latency_s=0.0, usage=hit.get("usage"), model=hit.get("model", self.p.model))
            self._trace(meta)
            return self._unmask(hit["parsed"]), meta
        t0 = time.time()
        text, usage, model_used, attempts = self._call_with_retry(system, user, max_tokens)
        try:
            parsed = parse_json_loose(text)
        except ValueError as e:
            with self.lock:
                self.stats["json_repairs"] += 1
            if self.cache_dir:
                (self.cache_dir / f"failed_{key}.txt").write_text(text or "", encoding="utf-8")
            if "cắt" in str(e) and usage and usage.get("completion_tokens", 0) >= max_tokens * 0.95:
                raise LLMError(f"{task}: phản hồi bị cắt ở max_tokens={max_tokens} — tăng max_tokens hoặc chia nhỏ nhiệm vụ") from e
            fix_sys = "Bạn nhận một chuỗi JSON bị lỗi cú pháp. Trả về DUY NHẤT JSON hợp lệ, giữ nguyên toàn bộ khoá và giá trị, không thêm bớt nội dung."
            fixed, u2, _, _ = self._call_with_retry(fix_sys, text or "", max_tokens)
            parsed = parse_json_loose(fixed)
            usage = _add_usage(usage, u2)
        meta.update(cache_hit=False, latency_s=round(time.time() - t0, 2), usage=usage, model=model_used, attempts=attempts)
        with self.lock:
            self.stats["calls"] += 1
            for k_src, k_dst in (("prompt_tokens", "prompt_tokens"), ("completion_tokens", "completion_tokens"), ("thought_tokens", "thought_tokens")):
                self.stats[k_dst] += (usage or {}).get(k_src, 0) or 0
        if cache and (usage or {}).get("finish_reason") not in ("length", "MAX_TOKENS"):   # không cache phản hồi bị cắt
            self._cache_put(key, {"model": model_used, "usage": usage, "parsed": parsed, "raw": text})
        self._trace(meta)
        return self._unmask(parsed), meta

    def cost_usd(self) -> float:
        return round(self.stats["prompt_tokens"] / 1e6 * self.p.price_in + (self.stats["completion_tokens"] + self.stats["thought_tokens"]) / 1e6 * self.p.price_out, 4)

    # ------------------------------------------------------------------ nội bộ
    def _unmask(self, obj):
        if self.guard is not None and self.p.privacy == "pseudonymize":
            return self.guard.unmask(obj)
        return obj

    def _call_with_retry(self, system, user, max_tokens):
        models = [self.p.model] + [m for m in self.p.fallback_models if m != self.p.model]
        last = None
        attempts = 0
        for mi, model in enumerate(models):
            last_model = mi == len(models) - 1
            tries = self.p.retries if last_model else min(self.p.retries, 1)   # chuyển model dự phòng sớm (2 lần thử), model cuối thử đủ
            for k in range(tries + 1):
                attempts += 1
                try:
                    text, usage = self._call(system, user, max_tokens, self.p.temperature, model)
                    if mi:
                        with self.lock:
                            self.stats["fallbacks"] += 1
                    return text, usage, model, attempts
                except QuotaExceeded as e:
                    last = e
                    break
                except Retryable as e:
                    last = e
                    with self.lock:
                        self.stats["retries"] += 1
                    if k < tries:
                        time.sleep(min(60.0, 2.0 * 2 ** k) * (0.75 + random.random() / 2))
            # hết lượt thử với model này -> model dự phòng
        raise LLMError(f"{self.p.name}: thất bại sau {attempts} lần thử ({', '.join(models)}): {last}")

    def _call(self, system: str, user: str, max_tokens: int, temperature: float, model: str) -> tuple[str, dict]:
        raise NotImplementedError

    def _post(self, url: str, headers: dict, payload: dict) -> dict:
        try:
            r = httpx.post(url, headers=headers, json=payload, timeout=self.p.timeout)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise Retryable(f"{type(e).__name__}") from None
        if r.status_code == 429 and re.search(r"exceeded your current quota|billing|insufficient_quota", r.text, re.I):
            raise QuotaExceeded(f"HTTP 429 hết hạn mức (quota) của model: {scrub(r.text[:160])}")
        if r.status_code in RETRY_STATUS:
            raise Retryable(f"HTTP {r.status_code}: {scrub(r.text[:200])}")
        if r.status_code >= 400:
            raise LLMError(f"HTTP {r.status_code}: {scrub(r.text[:400])}")
        return r.json()

    def _cache_get(self, key):
        if not self.cache_dir:
            return None
        p = self.cache_dir / f"{key}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def _cache_put(self, key, entry):
        if self.cache_dir:
            (self.cache_dir / f"{key}.json").write_text(json.dumps(entry, ensure_ascii=False, indent=1), encoding="utf-8")

    def _trace(self, meta):
        if self.trace:
            self.trace(dict(meta, ts=time.strftime("%Y-%m-%dT%H:%M:%S")))


def _add_usage(a, b):
    out = dict(a or {})
    for k, v in (b or {}).items():
        if isinstance(v, (int, float)):
            out[k] = out.get(k, 0) + v
    return out


class OpenAICompatClient(LLMClient):
    """POST {base}/chat/completions. base_url có thể là gốc, …/v1 hoặc đầy đủ …/chat/completions."""
    name = "openai_compat"

    def __init__(self, profile: Profile, **kw):
        super().__init__(profile, **kw)
        b = (profile.base_url or "").rstrip("/")
        if not b:
            raise LLMError(f"{profile.name}: thiếu base_url")
        if not profile.api_key:
            raise LLMError(f"{profile.name}: thiếu api_key (kiểm tra biến môi trường trong .env)")
        if b.endswith("/chat/completions"):
            self.url = b
        elif re.search(r"/v\d+(beta)?(/openai)?$", b):
            self.url = b + "/chat/completions"
        else:
            self.url = b + "/v1/chat/completions"

    def _call(self, system, user, max_tokens, temperature, model):
        payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        payload.update(self.p.extra_body or {})
        if self.p.json_mode in ("response_format", True, "true"):
            payload["response_format"] = {"type": "json_object"}
        j = self._post(self.url, {"Authorization": f"Bearer {self.p.api_key}"}, payload)
        ch = (j.get("choices") or [{}])[0]
        text = (ch.get("message") or {}).get("content") or ""
        u = j.get("usage") or {}
        det = u.get("completion_tokens_details") or {}
        if ch.get("finish_reason") == "length" and not text.rstrip().endswith("}"):
            pass  # parse_json_loose sẽ báo bị cắt
        return text, {"prompt_tokens": u.get("prompt_tokens", 0), "completion_tokens": u.get("completion_tokens", 0),
                      "thought_tokens": det.get("reasoning_tokens", 0) or 0, "finish_reason": ch.get("finish_reason")}


class GeminiClient(LLMClient):
    """Gemini API gốc: POST …/v1beta/models/{model}:generateContent (responseMimeType=application/json)."""
    name = "gemini"

    def __init__(self, profile: Profile, **kw):
        super().__init__(profile, **kw)
        if not profile.api_key:
            raise LLMError(f"{profile.name}: thiếu api_key (GEMINI_API_KEY)")
        self.base = (profile.base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")

    def _call(self, system, user, max_tokens, temperature, model):
        gen = {"temperature": temperature, "maxOutputTokens": max_tokens, "responseMimeType": "application/json"}
        gen.update(self.p.generation_config or {})
        payload = {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": user}]}], "generationConfig": gen}
        j = self._post(f"{self.base}/models/{model}:generateContent", {"x-goog-api-key": self.p.api_key}, payload)
        cands = j.get("candidates") or []
        if not cands:
            fb = j.get("promptFeedback") or {}
            raise LLMError(f"Gemini không trả kết quả (blockReason={fb.get('blockReason')})")
        c0 = cands[0]
        text = "".join(p.get("text", "") for p in (c0.get("content") or {}).get("parts", []) if not p.get("thought"))
        u = j.get("usageMetadata") or {}
        return text, {"prompt_tokens": u.get("promptTokenCount", 0), "completion_tokens": u.get("candidatesTokenCount", 0),
                      "thought_tokens": u.get("thoughtsTokenCount", 0), "finish_reason": c0.get("finishReason")}


class AnthropicClient(LLMClient):
    """Claude Messages API gốc: POST {base}/v1/messages (x-api-key, anthropic-version). Không có response_format -> SYSTEM đã yêu cầu chỉ trả
    JSON và parse_json_loose xử lý phần thừa. stop_reason 'max_tokens' được chuẩn hoá thành finish_reason 'length'."""
    name = "anthropic"

    def __init__(self, profile: Profile, **kw):
        super().__init__(profile, **kw)
        if not profile.api_key:
            raise LLMError(f"{profile.name}: thiếu api_key (kiểm tra biến môi trường trong .env)")
        b = (profile.base_url or "https://api.anthropic.com").rstrip("/")
        self.url = b if b.endswith("/messages") else (b + "/messages" if b.endswith("/v1") else b + "/v1/messages")

    def _call(self, system, user, max_tokens, temperature, model):
        payload = {"model": model, "max_tokens": max_tokens, "system": system,   # temperature: model Claude mới không nhận
                   "messages": [{"role": "user", "content": user}]}
        payload.update(self.p.extra_body or {})
        j = self._post(self.url, {"x-api-key": self.p.api_key, "anthropic-version": "2023-06-01"}, payload)
        text = "".join(b.get("text", "") for b in j.get("content") or [] if b.get("type") == "text")
        u = j.get("usage") or {}
        stop = j.get("stop_reason")
        return text, {"prompt_tokens": (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0),
                      "completion_tokens": u.get("output_tokens", 0), "thought_tokens": 0,
                      "finish_reason": "length" if stop == "max_tokens" else stop}


class MockClient(LLMClient):
    """Trả lời từ thư mục fixture (key = task) — dùng cho test không cần mạng."""
    name = "mock"

    def __init__(self, profile: Profile, fixtures: Path | None = None, **kw):
        super().__init__(profile, **kw)
        self.fixtures = Path(fixtures or profile.base_url or ".")
        self._task = None

    def json(self, task, system, user, **kw):
        self._task = task
        p = self.fixtures / f"{task.replace(':', '_')}.json"
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        meta = {"task": task, "provider": "mock", "model": "mock", "cache_hit": False, "latency_s": 0, "usage": {}}
        self._trace(meta)
        return data, meta


class ReplayClient(LLMClient):
    """Phát lại phản hồi trích xuất của một lần chạy trước (<run>/01_llm/<task>.json) — chạy lại assemble/coding/cổng/build sau khi sửa code
    mà không tốn lời gọi LLM. Nhiệm vụ không có trong bản ghi (choose:*, repair:*) -> gọi `live` nếu có, không thì báo lỗi (bước đó bị bỏ qua)."""
    name = "replay"

    def __init__(self, profile: Profile, replay_dir: Path, live: LLMClient | None = None, **kw):
        super().__init__(profile, **kw)
        self.dir = Path(replay_dir) / "01_llm"
        self.live = live

    def json(self, task, system, user, **kw):
        base = task.split("#", 1)[0]
        p = self.dir / f"{base.replace(':', '_')}.json"
        if p.exists():
            rec = json.loads(p.read_text(encoding="utf-8"))
            data = rec.get("response")
            if isinstance(data, dict) and not data.get("_error"):
                with self.lock:
                    self.stats["cache_hits"] += 1
                meta = {"task": task, "provider": "replay", "model": (rec.get("meta") or {}).get("model"), "cache_hit": True, "latency_s": 0,
                        "usage": {k: v for k, v in ((rec.get("meta") or {}).get("usage") or {}).items() if k == "finish_reason"}}
                self._trace(meta)
                return data, meta
        if self.live is not None:
            out = self.live.json(task, system, user, **kw)
            for k, v in self.live.stats.items():
                self.stats[k] = v
            return out
        raise LLMError(f"replay: không có phản hồi ghi sẵn cho {task}")


def make_client(profile: Profile, **kw) -> LLMClient:
    if profile.kind == "openai_compat":
        return OpenAICompatClient(profile, **kw)
    if profile.kind == "gemini":
        return GeminiClient(profile, **kw)
    if profile.kind == "anthropic":
        return AnthropicClient(profile, **kw)
    if profile.kind == "mock":
        return MockClient(profile, **kw)
    raise LLMError(f"provider kind không hỗ trợ: {profile.kind}")
