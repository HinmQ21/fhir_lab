"""Bước 1 — kiểm kê: zip/thư mục PDF (dùng inventory.py của skill) hoặc thư mục .txt (ca tổng hợp / văn bản đã OCR).

Kết quả: manifest (như inventory.py), văn bản từng phiếu, và khối `documents[]` của clinical v2 dựng xác định (không cần LLM).
"""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path


def run(kb, src: Path, out: Path, fonts: list[str] | None = None) -> tuple[dict, dict[str, str], list[dict]]:
    out.mkdir(parents=True, exist_ok=True)
    txts = sorted(src.glob("*.txt"), key=kb.inventory.natural_key) if src.is_dir() else []
    if src.is_dir() and txts and not list(src.rglob("*.pdf")):
        manifest = _from_txt(kb, src, txts, out)
    else:
        manifest = kb.inventory.run(src, out)
        _decode_gid_fonts(src, manifest, out, fonts)
    texts: dict[str, str] = {}
    docs = []
    raw = out / "00_raw"
    for d in manifest["documents"]:
        did = d["suggested_id"]
        f = raw / f"{d['seq']:02d}_{d['form']}.txt"
        text = f.read_text(encoding="utf-8") if f.exists() else ""
        texts[did] = "" if d.get("needs_ocr") else text
        dt = kb.V.DOC_TYPES.get(d["form"], kb.V.DOC_TYPES["default"])
        header = next((ln for ln in d.get("header_lines", []) if len(ln) > 8 and re.search(r"PHI[ẾE]U|GI[ẤA]Y|B[ẢA]NG|B[ẢA]N|K[ẾE]T QU[ẢA]|T[ỜO]", ln)), None)
        docs.append({"id": did, "seq": d["seq"], "file": d["file"], "form": d["form"] if d["form"] in kb.V.DOC_TYPES else d["form"],
                     "title": (header or Path(d["file"]).stem)[:200], "type": {"code": dt[0], "display": dt[1]}, "date": None,
                     "pages": d.get("pages"), "size_bytes": d.get("size_bytes"), "sha1_b64": d.get("sha1_b64"),
                     "content_type": "text/plain" if d["file"].endswith(".txt") else "application/pdf", "extracted": not d.get("needs_ocr"),
                     "note": "Bản scan không có lớp văn bản — cần OCR; không trích xuất được nội dung" if d.get("needs_ocr") else None})
    return manifest, texts, docs


def _decode_gid_fonts(src: Path, manifest: dict, out: Path, fonts):
    """Phiếu in bằng font không có ToUnicode -> giải mã lại text (ghi đè 00_raw), đánh dấu text_decoded trong manifest."""
    import zipfile
    from .pdftext import decode_pdf

    def pdf_bytes(name):
        if src.is_file() and src.suffix.lower() == ".zip":
            with zipfile.ZipFile(src) as z:
                m = next((m for m in z.infolist() if Path(m.filename).name == name), None)
                return z.read(m) if m else None
        p = next(iter(src.rglob(name)), None) if src.is_dir() else (src if src.name == name else None)
        return p.read_bytes() if p else None

    for d in manifest["documents"]:
        if d.get("needs_ocr"):
            continue
        data = pdf_bytes(d["file"])
        if not data:
            continue
        try:
            pages, n = decode_pdf(data, fonts)
        except Exception:  # noqa: BLE001  — thiếu fontTools / PDF lạ: giữ text gốc
            continue
        if pages:
            (out / "00_raw" / f"{d['seq']:02d}_{d['form']}.txt").write_text(
                "\n".join(f"=== page {k} ===\n{p}" for k, p in enumerate(pages, 1)), encoding="utf-8")
            d["text_decoded"] = n
    manifest["gid_font_decoded"] = [d["seq"] for d in manifest["documents"] if d.get("text_decoded")]


def _from_txt(kb, src: Path, files: list[Path], out: Path) -> dict:
    raw = out / "00_raw"
    raw.mkdir(parents=True, exist_ok=True)
    docs = []
    for i, f in enumerate(files, 1):
        text = f.read_text(encoding="utf-8")
        known = sorted((k for k in kb.V.DOC_TYPES if k != "default" and k in f.stem), key=len, reverse=True)
        form, why = (known[0], "tên file chứa mã form") if known else kb.inventory.classify(f.name, text[:1500])
        data = text.encode("utf-8")
        (raw / f"{i:02d}_{form}.txt").write_text(f"=== page 1 ===\n{text}", encoding="utf-8")
        docs.append({"seq": i, "file": f.name, "form": form, "form_reason": why, "pages": 1, "size_bytes": len(data),
                     "sha1_b64": base64.b64encode(hashlib.sha1(data).digest()).decode(), "text_chars": len(re.sub(r"\s", "", text)),
                     "needs_ocr": False, "header_lines": [ln.strip() for ln in text.splitlines() if ln.strip()][:12], "patient_hints": {},
                     "loinc_doc_type": kb.V.DOC_TYPES.get(form, kb.V.DOC_TYPES["default"])[0], "suggested_id": f"doc-{i:02d}"})
    manifest = {"source": str(src), "documents": docs, "patient_hint_values": {}, "inconsistent_hints": {}, "needs_ocr": [], "unclassified": [d["seq"] for d in docs if d["form"] == "default"]}
    kb.V.write_json(manifest, out / "manifest.json")
    return manifest
