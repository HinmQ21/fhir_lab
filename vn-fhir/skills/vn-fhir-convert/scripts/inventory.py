"""inventory.py — kiểm kê một bộ hồ sơ bệnh án (zip hoặc thư mục PDF) trước khi trích xuất.

Việc xác định: thứ tự tài liệu, loại phiếu (form), có lớp văn bản hay không (cần OCR?), băm SHA-1, số trang,
và các dòng tiêu đề đầu trang để đối chiếu nhanh danh tính người bệnh giữa các phiếu.

Kết quả (out_dir):
  manifest.json      — danh sách tài liệu (seq, file, form, pages, size_bytes, sha1_b64, text_chars, needs_ocr, header_lines, patient_hints)
  inventory.md       — bảng tóm tắt để đọc nhanh
  00_raw/NN_<form>.txt — văn bản từng tài liệu (trang cách nhau bằng dòng "=== page N ===")

Cách chạy:  python inventory.py <zip|dir> <out_dir> [--no-text]
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vnlib as V  # noqa: E402

# (regex trên tên file + 1500 ký tự đầu trang 1, form key của vnlib.DOC_TYPES). Thứ tự quan trọng: mẫu đặc hiệu trước.
FORM_RULES = [
    (r"t[oó]m\s*t[aắ]t\s*h[oồ]\s*s[oơ]", "ban-tom-tat-ho-so-benh-an"),
    (r"b[aả]ng\s*k[eê]\s*chi\s*ph[ií]|thanh\s*to[aá]n\s*(ra\s*vi[eệ]n|chi\s*ph[ií])|m[aẫ]u\s*01/?kbcb", "bang-ke-chi-phi-kcb"),
    (r"c[oô]ng\s*khai\s*(d[iị]ch\s*v[uụ]|thu[oố]c)", "phieu-cong-khai-dich-vu"),
    (r"h[oộ]i\s*ch[aẩ]n", "trich-bien-ban-hoi-chan"),
    (r"kh[aá]m\s*b[eệ]nh\s*v[aà]o\s*vi[eệ]n|phi[eế]u\s*kh[aá]m\s*(b[eệ]nh|v[aà]o)", "phieu-kham-benh-vao-vien"),
    (r"gi[aấ]y\s*v[aà]o\s*vi[eệ]n", "giay-vao-vien"),
    (r"gi[aấ]y\s*ra\s*vi[eệ]n", "giay-ra-vien"),
    (r"chuy[eể]n\s*tuy[eế]n|chuy[eể]n\s*vi[eệ]n", "giay-chuyen-tuyen"),
    (r"t[eé]\s*ng[aã]", "phieu-danh-gia-nguy-co-te-nga"),
    (r"dinh\s*d[uư][oỡ]ng", "phieu-sang-loc-dinh-duong"),
    (r"huy[eế]t\s*h[oọ]c|t[eế]\s*b[aà]o\s*m[aá]u|c[oô]ng\s*th[uứ]c\s*m[aá]u|[dđ][oô]ng\s*m[aá]u", "ket-qua-xet-nghiem-huyet-hoc"),
    (r"sinh\s*h[oó]a|h[oó]a\s*sinh", "ket-qua-xet-nghiem-sinh-hoa-mau"),
    (r"n[uư][oớ]c\s*ti[eể]u|d[iị]ch\s*(n[aã]o\s*t[uủ]y|m[aà]ng)", "phieu-xet-nghiem-nuoc-dich"),
    (r"vi\s*sinh|vi\s*khu[aẩ]n|kh[aá]ng\s*sinh\s*[dđ][oồ]", "ket-qua-xet-nghiem-vi-sinh"),
    (r"gi[aả]i\s*ph[aẫ]u\s*b[eệ]nh|t[eế]\s*b[aà]o\s*h[oọ]c|m[oô]\s*b[eệ]nh\s*h[oọ]c", "ket-qua-giai-phau-benh"),
    (r"c[oộ]ng\s*h[uưở]+ng\s*t[uừ]|\bMRI\b|\bCHT\b", "phieu-chup-cong-huong-tu"),
    (r"c[aắ]t\s*l[oớ]p|\bCT\s*scan|\bCLVT\b", "phieu-chup-cat-lop-vi-tinh"),
    (r"x[\s\-]*quang|\bXQ\b", "phieu-chup-x-quang"),
    (r"si[eê]u\s*[aâ]m", "phieu-sieu-am-doppler"),
    (r"[dđ]i[eệ]n\s*n[aã]o", "phieu-dien-nao-do"),
    (r"[dđ]i[eệ]n\s*tim|\bECG\b|\b[DĐ]T[DĐ]\b", "phieu-dien-tim"),
    (r"n[oộ]i\s*soi", "phieu-noi-soi"),
    (r"ph[aẫ]u\s*thu[aậ]t|th[uủ]\s*thu[aậ]t", "phieu-phau-thuat-thu-thuat"),
    (r"g[aâ]y\s*m[eê]", "phieu-gay-me-hoi-suc"),
    (r"truy[eề]n\s*m[aá]u", "phieu-truyen-mau"),
    (r"theo\s*d[oõ]i\s*v[aà]\s*ch[aă]m\s*s[oó]c|ch[aă]m\s*s[oó]c|ph[aâ]n\s*c[aấ]p\s*ch[aă]m", "phieu-cham-soc"),
    (r"t[oờ]\s*[dđ]i[eề]u\s*tr[iị]|phi[eế]u\s*[dđ]i[eề]u\s*tr[iị]", "phieu-dieu-tri"),
    (r"c[aậ]n\s*l[aâ]m\s*s[aà]ng|\bCLS\b", "phieu-cls"),
    (r"b[eệ]nh\s*[aá]n", "benh-an"),
]
PATIENT_HINTS = [("ho_ten", r"(?:H[oọ]\s*(?:v[aà])?\s*t[eê]n(?:\s*ng[uư][oờ]i\s*b[eệ]nh|\s*BN)?|T[eê]n\s*(?:ng[uư][oờ]i\s*b[eệ]nh|BN))\s*[:\.]?\s*(?!NG[UƯ][OỜ]I)((?:[A-ZÀ-Ỹ]{2,}\s+){1,4}[A-ZÀ-Ỹ]{2,})(?!\w)"),
                 ("ma_bn", r"(?:M[aã]\s*(?:BN|b[eệ]nh\s*nh[aâ]n|ng[uư][oờ]i\s*b[eệ]nh))\s*[:\.]?\s*([A-Z]{0,3}\d{6,12})"),
                 ("ma_benh_an", r"(?:M[aã]\s*b[eệ]nh\s*[aá]n|S[oố]\s*b[eệ]nh\s*[aá]n)\s*[:\.]?\s*([A-Z]{0,3}\d{5,12})"),
                 ("tuoi", r"(?:Tu[oổ]i|N[aă]m\s*sinh)\s*[:\.]?\s*(\d{2,4})(?!\d|/)"),
                 ("gioi", r"Gi[oớ]i\s*(?:t[ií]nh)?\s*[:\.]?\s*(Nam|N[uữ])"),
                 ("bhyt", r"\b([A-Z]{2}\d{13})\b"),
                 ("cccd", r"\b(0\d{11})\b")]


def pdf_text(path: Path) -> list[str]:
    try:
        import pypdf
        r = pypdf.PdfReader(str(path))
        return [(p.extract_text() or "") for p in r.pages]
    except Exception:
        try:
            import pymupdf  # type: ignore
            doc = pymupdf.open(str(path))
            return [pg.get_text() for pg in doc]
        except Exception as e:  # pragma: no cover
            return [f"<<lỗi đọc PDF: {e}>>"]


def classify(name: str, head: str) -> tuple[str, str]:
    for pat, form in FORM_RULES:
        if re.search(pat, name, re.I):
            return form, f"tên file ~ /{pat}/"
    for pat, form in FORM_RULES:
        if re.search(pat, head, re.I):
            return form, f"trang 1 ~ /{pat}/"
    return "default", "không khớp mẫu nào"


def natural_key(p: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


def run(src: Path, out: Path, with_text: bool = True) -> dict:
    tmp = None
    if src.is_file() and src.suffix.lower() == ".zip":
        tmp = Path(tempfile.mkdtemp(prefix="vnfhir_inv_"))
        with zipfile.ZipFile(src) as z:
            for m in z.infolist():
                if m.filename.lower().endswith(".pdf") and not m.filename.startswith("__MACOSX"):
                    z.extract(m, tmp)
        pdf_dir = tmp
    else:
        pdf_dir = src
    pdfs = sorted(pdf_dir.rglob("*.pdf"), key=natural_key)
    out.mkdir(parents=True, exist_ok=True)
    raw = out / "00_raw"
    if with_text:
        raw.mkdir(exist_ok=True)
    docs = []
    for i, f in enumerate(pdfs, 1):
        data = f.read_bytes()
        pages = pdf_text(f)
        text = "\n".join(pages)
        chars = len(re.sub(r"\s", "", text))
        head = text[:1500]
        form, why = classify(f.name, head)
        hints = {}
        for key, pat in PATIENT_HINTS:
            m = re.search(pat, text[:4000], re.I if key != "ho_ten" else 0)
            if m:
                val = m.group(1).strip()
                if key == "ho_ten" and (re.match(r"^(BS|BSCKI|BSCKII|THS|TS|KTV|ĐD|DD)\b", val) or len(val.split()) < 2):
                    continue  # tên bác sĩ / nhãn, không phải người bệnh
                if key == "tuoi" and not (0 < int(val) < 125 or 1900 < int(val) < 2100):
                    continue
                hints[key] = val
        header = [ln.strip() for ln in text.splitlines() if ln.strip()][:12]
        d = {"seq": i, "file": f.name, "form": form, "form_reason": why, "pages": len(pages), "size_bytes": len(data), "sha1_b64": base64.b64encode(hashlib.sha1(data).digest()).decode(),
             "text_chars": chars, "needs_ocr": chars < 40 * max(1, len(pages)), "header_lines": header, "patient_hints": hints,
             "loinc_doc_type": V.DOC_TYPES.get(form, V.DOC_TYPES["default"])[0], "suggested_id": f"doc-{i:02d}"}
        if with_text:
            (raw / f"{i:02d}_{form}.txt").write_text("\n".join(f"=== page {k} ===\n{p}" for k, p in enumerate(pages, 1)), encoding="utf-8")
        docs.append(d)
    # consistency of patient hints across documents
    seen: dict[str, set] = {}
    for d in docs:
        for k, v in d["patient_hints"].items():
            seen.setdefault(k, set()).add(V.nfc(v).upper() if k == "ho_ten" else v)
    inconsistent = {k: sorted(v) for k, v in seen.items() if len(v) > 1}
    manifest = {"source": str(src), "documents": docs, "patient_hint_values": {k: sorted(v) for k, v in seen.items()}, "inconsistent_hints": inconsistent,
                "needs_ocr": [d["seq"] for d in docs if d["needs_ocr"]], "unclassified": [d["seq"] for d in docs if d["form"] == "default"]}
    V.write_json(manifest, out / "manifest.json")
    lines = [f"# Kiểm kê hồ sơ — {src.name}", "", f"{len(docs)} tài liệu PDF; cần OCR: {manifest['needs_ocr'] or 'không'}; chưa phân loại: {manifest['unclassified'] or 'không'}", "",
             "| seq | file | form | trang | ký tự | OCR? | gợi ý BN |", "|---|---|---|---|---|---|---|"]
    for d in docs:
        lines.append(f"| {d['seq']:02d} | {d['file']} | {d['form']} | {d['pages']} | {d['text_chars']} | {'CÓ' if d['needs_ocr'] else ''} | {', '.join(f'{k}={v}' for k, v in d['patient_hints'].items())} |")
    if inconsistent:
        lines += ["", "## Gợi ý danh tính KHÔNG nhất quán giữa các phiếu (kiểm tra trước khi trích xuất)"] + [f"- {k}: {v}" for k, v in inconsistent.items()]
    (out / "inventory.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if tmp:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("out_dir")
    ap.add_argument("--no-text", action="store_true")
    a = ap.parse_args(argv)
    m = run(Path(a.src), Path(a.out_dir), not a.no_text)
    print(f"[inventory] {len(m['documents'])} tài liệu -> {a.out_dir}/manifest.json; cần OCR: {m['needs_ocr'] or 'không'}; chưa phân loại: {m['unclassified'] or 'không'}; mâu thuẫn danh tính: {list(m['inconsistent_hints']) or 'không'}")


if __name__ == "__main__":
    main()
