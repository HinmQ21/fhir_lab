"""Tiện ích dùng chung cho các bước làm tay (work/step*.py).

- load_env(): đọc endpoint LLM (OpenAI-compatible, vLLM) từ .env của nuextract_eval hoặc biến môi trường.
- llm_json(): gọi chat/completions, tắt thinking, parse JSON an toàn.
- pdf_text(): trích text theo trang bằng pymupdf, tự giải mã font Identity-H không có ToUnicode
  (các phiếu CĐHA của HIS Phú Thọ in bằng "ABCDEE+Times New Roman" -> text ra chuỗi GID).
"""
from __future__ import annotations

import json
import os
import re
import hashlib
from pathlib import Path

import httpx
import pymupdf

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "output" / "_llm_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- LLM endpoint

def load_env() -> tuple[str, str, str]:
    """(base_url, api_key, model). Ưu tiên biến môi trường LLM_BASE_URL/LLM_API_KEY/LLM_MODEL."""
    base = os.environ.get("LLM_BASE_URL")
    key = os.environ.get("LLM_API_KEY")
    model = os.environ.get("LLM_MODEL", "glm53-flash")
    if not base or not key:
        env = Path(r"D:\VTS_Workspace\local_lab\nuextract_eval\.env")
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip()
                if k == "GLM5.3FL_URL_BASE" and not base:
                    base = v
                if k == "GLM5.3FL_API_KEY" and not key:
                    key = v
    if not base or not key:
        raise RuntimeError("Chưa cấu hình LLM_BASE_URL / LLM_API_KEY")
    return base.rstrip("/"), key, model


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json_loose(text: str):
    """Lấy JSON object/array đầu tiên trong text (model đôi khi thêm lời dẫn)."""
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
    # tìm dấu mở đầu tiên và cắt đến dấu đóng tương ứng
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError(f"no JSON in response: {text[:200]}")
    depth = 0
    in_str = False
    esc = False
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
                return json.loads(text[start : i + 1])
    raise ValueError("unbalanced JSON in response")


def llm_json(system: str, user, *, thinking: bool = False, max_tokens: int = 16000,
             temperature: float = 0.0, cache: bool = True, images: list[str] | None = None,
             timeout: float = 900.0, guided_json: bool = True):
    """Gọi LLM, trả về JSON đã parse. `user` là str hoặc list content (multimodal)."""
    base, key, model = load_env()
    content = user
    if images:
        content = [{"type": "text", "text": user}] + [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in images
        ]
    payload = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
        "chat_template_kwargs": {"enable_thinking": bool(thinking)},
    }
    if guided_json and not thinking:
        # vLLM structured output: ép content là JSON ngay từ token đầu (tránh model "suy nghĩ" trong content)
        payload["response_format"] = {"type": "json_object"}
    h = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
    cpath = CACHE_DIR / f"{h}.json"
    if cache and cpath.exists():
        return json.loads(cpath.read_text(encoding="utf-8"))["parsed"]
    r = httpx.post(f"{base}/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                   json=payload, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    msg = j["choices"][0]["message"]
    text = msg.get("content") or ""
    try:
        parsed = parse_json_loose(text)
    except Exception as e:  # lưu raw để chẩn đoán rồi thử sửa JSON bằng chính model
        (CACHE_DIR / f"failed_{h}.txt").write_text(text, encoding="utf-8")
        parsed = _repair_json(text, base, key, model, timeout)
    if cache:
        cpath.write_text(json.dumps({"payload_hash": h, "usage": j.get("usage"), "raw": text,
                                     "reasoning": msg.get("reasoning"), "parsed": parsed},
                                    ensure_ascii=False, indent=1), encoding="utf-8")
    return parsed


def _repair_json(text: str, base: str, key: str, model: str, timeout: float):
    """Model trả JSON hỏng (thiếu dấu, cắt giữa chừng...) -> nhờ model sửa lại, chỉ sửa cú pháp."""
    r = httpx.post(f"{base}/v1/chat/completions", headers={"Authorization": f"Bearer {key}"}, timeout=timeout, json={
        "model": model, "temperature": 0, "max_tokens": 24000, "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "system", "content": "Bạn nhận một chuỗi JSON bị lỗi cú pháp. Trả về DUY NHẤT JSON hợp lệ, giữ nguyên toàn bộ khoá và giá trị, không thêm bớt nội dung."},
                     {"role": "user", "content": text}]})
    r.raise_for_status()
    return parse_json_loose(r.json()["choices"][0]["message"].get("content") or "")


# ---------------------------------------------------------------- PDF text

_G2U: dict[int, int] | None = None


def _glyph_map() -> dict[int, int]:
    """GID -> unicode của Times New Roman hệ thống (font nhúng là subset giữ nguyên GID gốc)."""
    global _G2U
    if _G2U is None:
        from fontTools.ttLib import TTFont
        tt = TTFont(r"C:\Windows\Fonts\times.ttf")
        g2u: dict[int, int] = {}
        for u, gname in tt.getBestCmap().items():
            g2u.setdefault(tt.getGlyphID(gname), u)
        _G2U = g2u
    return _G2U


def _gid_fonts(page) -> set[str]:
    """Tên font (không có tiền tố subset) của các font Type0/Identity-H nhúng dạng 'ABCDEE+...'.
    Lưu ý: get_text('dict') trả span['font'] ĐÃ BỎ tiền tố 'ABCDEE+', nên phải so theo get_fonts()."""
    names = set()
    for f in page.get_fonts(full=True):
        xref, name, enc = f[0], f[3], f[5]
        if "+" in name and enc == "Identity-H":
            # chỉ font KHÔNG có ToUnicode mới ra chuỗi GID; font subset bình thường (FAAAAH+TimesNewRomanPSMT) vẫn decode đúng
            has_tu = page.parent.xref_get_key(xref, "ToUnicode")[0] not in ("null", None)
            if not has_tu:
                names.add(name.split("+", 1)[1])
    return names


def _is_gid_font(span: dict, gid_fonts: set[str]) -> bool:
    return span.get("font", "") in gid_fonts


def _decode_span(text: str) -> str:
    g2u = _glyph_map()
    out = []
    for ch in text:
        o = ord(ch)
        if o == 3:
            out.append(" ")
        elif o in g2u:
            out.append(chr(g2u[o]))
        else:
            out.append(ch)
    return "".join(out)


def pdf_pages_text(pdf_path: str | Path) -> list[str]:
    """Text từng trang theo thứ tự đọc (block/line), đã giải mã font GID nếu gặp."""
    doc = pymupdf.open(str(pdf_path))
    pages = []
    for page in doc:
        d = page.get_text("dict", sort=True)
        gid_fonts = _gid_fonts(page)
        lines = []
        for b in d["blocks"]:
            for l in b.get("lines", []):
                parts = []
                for sp in l["spans"]:
                    t = sp["text"]
                    if gid_fonts and _is_gid_font(sp, gid_fonts):
                        t = _decode_span(t)
                    parts.append(t)
                s = "".join(parts).rstrip()
                if s.strip():
                    lines.append(s)
        pages.append("\n".join(lines))
    return pages


def pdf_page_png_b64(pdf_path: str | Path, page_no: int = 0, dpi: int = 110) -> str:
    import base64
    doc = pymupdf.open(str(pdf_path))
    pix = doc[page_no].get_pixmap(dpi=dpi)
    return base64.b64encode(pix.tobytes("png")).decode()


def sha1_b64(path: str | Path) -> str:
    import base64
    return base64.b64encode(hashlib.sha1(Path(path).read_bytes()).digest()).decode()


def dump(obj, path: str | Path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def load(path: str | Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
