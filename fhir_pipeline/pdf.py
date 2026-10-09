"""Text PDF theo trang (pymupdf) + giải mã font nhúng không có ToUnicode.

Bài học từ lab: một số phiếu CĐHA của HIS in bằng font subset "ABCDEE+Times New Roman" (Type0/Identity-H, không ToUnicode)
-> text ra chuỗi GID. Font subset giữ nguyên GID gốc nên ánh xạ GID->Unicode qua cmap của Times New Roman hệ thống.
Chỉ giải mã font KHÔNG có ToUnicode (kiểm tra xref), vì get_text('dict') trả span['font'] đã bỏ tiền tố subset.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import re
from pathlib import Path

import pymupdf

log = logging.getLogger("fhir_pipeline.pdf")

_G2U: dict[str, dict[int, int]] = {}
_FONTS: list[str] = []
_SUSPECT = re.compile(r"[Ѐ-ԯ֐-׿]")  # ký tự Cyrillic/Hebrew = dấu hiệu text GID chưa giải mã


def configure_fonts(paths: list[str]):
    global _FONTS
    _FONTS = [p for p in paths if Path(p).exists()]
    if not _FONTS:
        log.warning("không tìm thấy font Times New Roman để giải mã PDF font GID: %s", paths)


def _glyph_map() -> dict[int, int] | None:
    if not _FONTS:
        return None
    f = _FONTS[0]
    if f not in _G2U:
        from fontTools.ttLib import TTFont
        tt = TTFont(f)
        g2u: dict[int, int] = {}
        for u, gname in tt.getBestCmap().items():
            g2u.setdefault(tt.getGlyphID(gname), u)
        _G2U[f] = g2u
    return _G2U[f]


def _gid_fonts(page) -> set[str]:
    names = set()
    for f in page.get_fonts(full=True):
        xref, name, enc = f[0], f[3], f[5]
        if "+" in name and enc == "Identity-H":
            has_tu = page.parent.xref_get_key(xref, "ToUnicode")[0] not in ("null", None)
            if not has_tu:
                names.add(name.split("+", 1)[1])
    return names


def _decode_span(text: str, g2u: dict[int, int]) -> str:
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


def pdf_pages_text(pdf_path: str | Path) -> tuple[list[str], dict]:
    """-> (text từng trang, info{gid_fonts_decoded, suspect_ratio}). suspect_ratio cao = text vẫn hỏng (cần OCR/vision)."""
    doc = pymupdf.open(str(pdf_path))
    pages, decoded, total, suspect = [], 0, 0, 0
    for page in doc:
        d = page.get_text("dict", sort=True)
        gid_fonts = _gid_fonts(page)
        g2u = _glyph_map() if gid_fonts else None
        lines = []
        for b in d["blocks"]:
            for l in b.get("lines", []):
                parts = []
                for sp in l["spans"]:
                    t = sp["text"]
                    if gid_fonts and sp.get("font", "") in gid_fonts:
                        if g2u:
                            t = _decode_span(t, g2u)
                            decoded += 1
                    parts.append(t)
                s = "".join(parts).rstrip()
                if s.strip():
                    lines.append(s)
        txt = "\n".join(lines)
        total += len(txt)
        suspect += len(_SUSPECT.findall(txt))
        pages.append(txt)
    return pages, {"gid_spans_decoded": decoded, "suspect_ratio": round(suspect / total, 4) if total else 0.0}


def pdf_page_png_b64(pdf_path: str | Path, page_no: int = 0, dpi: int = 110) -> str:
    doc = pymupdf.open(str(pdf_path))
    pix = doc[page_no].get_pixmap(dpi=dpi)
    return base64.b64encode(pix.tobytes("png")).decode()


def sha1_b64(path: str | Path) -> str:
    return base64.b64encode(hashlib.sha1(Path(path).read_bytes()).digest()).decode()
