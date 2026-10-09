"""Giải mã text PDF in bằng font subset Type0/Identity-H KHÔNG có ToUnicode (một số phiếu CĐHA/TDCN của HIS).

pymupdf trả chuỗi GID ("%6&.,,ĈjP7Kӏ…" thay vì "BSCKII Đàm Thị…") -> LLM đoán sai họ tên, kết luận. Font subset giữ GID gốc của
Times New Roman nên ánh xạ GID -> Unicode bằng cmap của font hệ thống CÙNG KIỂU (thường/đậm/nghiêng/đậm nghiêng có bảng glyph riêng).
Chỉ giải mã span thuộc font không có ToUnicode.
"""
from __future__ import annotations

from pathlib import Path

FONT_DIRS = [r"C:\Windows\Fonts", "/usr/share/fonts/truetype/msttcorefonts", "/Library/Fonts"]
STYLE_FILES = {"regular": ["times.ttf", "Times_New_Roman.ttf", "Times New Roman.ttf"],
               "bold": ["timesbd.ttf", "Times_New_Roman_Bold.ttf", "Times New Roman Bold.ttf"],
               "italic": ["timesi.ttf", "Times_New_Roman_Italic.ttf", "Times New Roman Italic.ttf"],
               "bolditalic": ["timesbi.ttf", "Times_New_Roman_Bold_Italic.ttf", "Times New Roman Bold Italic.ttf"]}
_G2U: dict[str, dict[int, int]] = {}


def _style(font_name: str) -> str:
    n = font_name.lower().replace(" ", "")
    b, i = "bold" in n, "italic" in n or "oblique" in n
    return "bolditalic" if b and i else "bold" if b else "italic" if i else "regular"


def _glyph_map(style: str, font_dirs: list[str]) -> dict[int, int] | None:
    f = next((Path(d) / n for d in font_dirs for n in STYLE_FILES[style] if (Path(d) / n).exists()), None)
    if f is None and style != "regular":
        return _glyph_map("regular", font_dirs)
    if f is None:
        return None
    key = str(f)
    if key not in _G2U:
        from fontTools.ttLib import TTFont
        tt = TTFont(key)
        g2u: dict[int, int] = {}
        for u, gname in tt.getBestCmap().items():
            g2u.setdefault(tt.getGlyphID(gname), u)
        _G2U[key] = g2u
    return _G2U[key]


def _gid_fonts(page) -> list[str]:
    names = []
    for f in page.get_fonts(full=True):
        xref, name, enc = f[0], f[3], f[5]
        if "+" in name and enc == "Identity-H" and page.parent.xref_get_key(xref, "ToUnicode")[0] in ("null", None):
            names.append(name.split("+", 1)[1])
    return names


def _decode(t: str, g2u: dict[int, int], before: str = "") -> str:
    """GID 3 = dấu cách thật. Ký tự 32 trong span GID là dấu cách pymupdf tự chèn (không phải GID 32 '='): sau chữ có dấu (glyph dựng sẵn
    có độ rộng lệch) nó nằm GIỮA từ ("Ngườ i") -> bỏ; còn lại -> dấu cách."""
    out = [before] if before else []
    for c in t:
        o = ord(c)
        if o == 3:
            out.append(" ")
        elif o == 32:
            prev = out[-1] if out else ""
            if not (prev.isalpha() and ord(prev) > 127):
                out.append(" ")
        elif o in g2u:
            out.append(chr(g2u[o]))
        else:
            out.append(c)
    return "".join(out)[len(before):]


def decode_pdf(data: bytes, font_dirs: list[str] | None = None) -> tuple[list[str] | None, int]:
    """-> (text từng trang đã giải mã, số span đã giải mã). (None, 0) nếu PDF không có font GID (giữ text của inventory)."""
    import pymupdf
    doc = pymupdf.open(stream=data, filetype="pdf")
    if not any(_gid_fonts(p) for p in doc):
        return None, 0
    dirs = font_dirs or FONT_DIRS
    if _glyph_map("regular", dirs) is None:
        return None, 0
    pages, n = [], 0
    for page in doc:
        gid = _gid_fonts(page)
        lines = []
        for b in page.get_text("dict", sort=True)["blocks"]:
            for ln in b.get("lines", []):
                parts = []
                for sp in ln["spans"]:
                    t, fn = sp["text"], sp.get("font", "")
                    full = next((g for g in gid if fn and (g == fn or g.startswith(fn))), None)   # pymupdf có thể cắt ngắn tên font
                    if full:
                        g2u = _glyph_map(_style(full), dirs)
                        t = _decode(t, g2u, (parts[-1][-1:] if parts and parts[-1] else ""))
                        n += 1
                    parts.append(t)
                s = "".join(parts).rstrip()
                if s.strip():
                    lines.append(s)
        pages.append("\n".join(lines))
    return pages, n
