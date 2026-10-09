"""Bước 1: zip HSBA -> phân loại từng PDF -> text -> KIE theo template TT32 -> JSON tài liệu + manifest.

Phân loại mặc định bằng luật (config.rules); có thể thay bằng classifier ngoài qua tham số `classifier(name, first_page_text) -> (form, reason)`.
Chốt chặn: tên khoá so với template (reconcile + warnings), tên người bệnh phải nhất quán giữa các phiếu (PATIENT_MISMATCH),
tỷ lệ ký tự lạ trong text (TEXT_SUSPECT) để phát hiện PDF giải mã hỏng.
"""
from __future__ import annotations

import json
import logging
import re
import zipfile
from pathlib import Path
from typing import Callable

from .pdf import pdf_pages_text, sha1_b64
from .util import dump, load, nfc

log = logging.getLogger("fhir_pipeline.extract")

SYSTEM = """Bạn là hệ thống trích xuất thông tin (KIE) cho hồ sơ bệnh án điện tử Việt Nam.
Nhiệm vụ: điền giá trị vào TEMPLATE JSON dựa trên TEXT của một phiếu (text đã được trích từ PDF theo thứ tự đọc; bảng có thể bị tách mỗi ô một dòng).
Quy tắc bắt buộc:
- Trả về DUY NHẤT một JSON object có đúng cấu trúc khoá của TEMPLATE (giữ nguyên mọi khoá, kể cả khoá lồng nhau). Không giải thích, không markdown.
- Mỗi giá trị trong TEMPLATE là MÔ TẢ cách điền; thay nó bằng giá trị thực đọc được. Giá trị kiểu chuỗi giữ NGUYÊN VĂN như in trên phiếu (không dịch, không sửa chính tả, không chuẩn hoá ngày/số, giữ số 0 ở đầu).
- Ô trống / không có trên phiếu -> null. Checkbox -> true/false. Mảng -> liệt kê đủ mọi phần tử theo thứ tự trên phiếu; mảng rỗng [] nếu không có.
- Bảng kết quả xét nghiệm: mỗi dòng chỉ số là một phần tử; các cột KẾT QUẢ/ĐVT/Giá trị BT/Ghi chú/Quy trình lấy theo đúng dòng; cờ H/L in trước giá trị là 'Đánh dấu'.
- Không bịa thông tin không có trên phiếu."""

KEY_SYNONYMS = {"Đơn vị tính": "Đơn vị", "Đơn vị": "Đơn vị tính", "Giá trị BT": "Giá trị bình thường", "Giá trị bình thường": "Giá trị BT",
                "Mã BN": "Mã bệnh nhân", "Mã người bệnh": "Mã bệnh nhân", "Họ và tên": "Họ tên", "Họ tên": "Họ và tên"}


def classify_by_rules(name: str, first_page_text: str, rules) -> tuple[str, str]:
    """-> (form_key, reason). form_key bắt đầu bằng 'skip:' nếu bỏ qua."""
    n = nfc(name).lower()
    for pat, key in rules.skip:
        if re.search(pat, n):
            return f"skip:{key}", "skip-list theo cấu hình"
    t = nfc(first_page_text[:600]).upper()
    for marker in rules.appendix_markers:
        if all(part in t for part in marker.split("+")):
            return "skip:phu-luc-huong-dan", "trang phụ lục hướng dẫn, không có dữ liệu người bệnh"
    for pat, key in rules.forms:
        if re.search(pat, n):
            return key, f"tên file khớp /{pat}/"
    return "skip:unknown", "không nhận dạng được"


def extract_one(form_key: str, pages: list[str], templates_dir: Path, llm, max_tokens: int = 24000) -> dict:
    template = load(Path(templates_dir) / f"{form_key}.template.json")
    text = "\n\n".join(f"===== TRANG {i+1} =====\n{p}" for i, p in enumerate(pages))
    user = f"TEMPLATE:\n{json.dumps(template, ensure_ascii=False, indent=1)}\n\nTEXT PHIẾU:\n{text}\n\nJSON kết quả:"
    return llm.complete_json(SYSTEM, user, max_tokens=max_tokens)


def reconcile_keys(template, data) -> list[str]:
    """Model đôi khi dùng tên khoá đồng nghĩa: đổi về đúng khoá template. (1) bảng đồng nghĩa; (2) 1 thiếu + 1 thừa cùng mức -> đổi tên."""
    fixes = []
    if isinstance(template, dict) and isinstance(data, dict):
        missing = [k for k in template if k not in data]
        extra = [k for k in data if k not in template]
        for k in list(extra):
            syn = KEY_SYNONYMS.get(k)
            if syn in missing:
                data[syn] = data.pop(k); fixes.append(f"RENAME {k}->{syn}"); missing.remove(syn); extra.remove(k)
        if len(missing) == 1 and len(extra) == 1:
            data[missing[0]] = data.pop(extra[0]); fixes.append(f"RENAME {extra[0]}->{missing[0]}")
        for k, v in template.items():
            if k in data:
                fixes += reconcile_keys(v, data[k])
    elif isinstance(template, list) and template and isinstance(data, list):
        for item in data:
            fixes += reconcile_keys(template[0], item)
    return fixes


def check_keys(template, data, path="") -> list[str]:
    warns = []
    if isinstance(template, dict):
        if not isinstance(data, dict):
            return [f"TYPE_MISMATCH {path or '/'}"]
        for k, v in template.items():
            if k not in data:
                warns.append(f"MISSING_KEY {path}/{k}")
            else:
                warns += check_keys(v, data[k], f"{path}/{k}")
        for k in data:
            if k not in template:
                warns.append(f"EXTRA_KEY {path}/{k}")
    elif isinstance(template, list) and template:
        if not isinstance(data, list):
            return [f"TYPE_MISMATCH {path}"]
        for i, item in enumerate(data):
            warns += check_keys(template[0], item, f"{path}[{i}]")
    return warns


def find_patient_name(data) -> str | None:
    if isinstance(data, dict):
        for k, v in data.items():
            kn = nfc(k).lower()
            if isinstance(v, str) and (kn.startswith("họ") or kn.startswith("tên bệnh nhân") or kn.startswith("tên người bệnh")) and "nhà" not in kn:
                return v
        for v in data.values():
            r = find_patient_name(v)
            if r:
                return r
    return None


def check_patient(expected: str | None, data) -> list[str]:
    got = find_patient_name(data)
    if expected and got and nfc(got).strip().lower() != nfc(expected).strip().lower():
        return [f"PATIENT_MISMATCH expected='{expected}' got='{got}'"]
    return []


def run_extract(zip_path: str | Path, out_dir: str | Path, cfg, llm, classifier: Callable | None = None, progress: Callable | None = None) -> dict:
    """zip -> out_dir/00_raw, 01_documents, manifest.json. Trả manifest."""
    zip_path, out_dir = Path(zip_path), Path(out_dir)
    raw, docs_dir = out_dir / "00_raw", out_dir / "01_documents"
    raw.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)
    tpl = cfg.path(cfg.paths.templates)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(raw)
    pdfs = sorted(raw.rglob("*.pdf"), key=lambda p: int(re.match(r"(\d+)", p.name).group(1)) if re.match(r"(\d+)", p.name) else 999)
    case = re.sub(r"^HSBA-", "", zip_path.stem)
    manifest = {"case": case, "source_zip": zip_path.name, "documents": []}
    expected_name = None
    classify = classifier or (lambda name, text: classify_by_rules(name, text, cfg.rules))
    for seq, pdf in enumerate(pdfs, 1):
        pages, info = pdf_pages_text(pdf)
        form, reason = classify(pdf.stem, pages[0] if pages else "")
        entry = {"seq": seq, "file": pdf.name, "pages": len(pages), "form": form, "reason": reason,
                 "size_bytes": pdf.stat().st_size, "sha1_b64": sha1_b64(pdf), "text_info": info}
        (raw / (pdf.stem + ".txt")).write_text("\n\n".join(pages), encoding="utf-8")
        if form.startswith("skip:"):
            manifest["documents"].append(entry)
            if progress:
                progress(seq, len(pdfs), pdf.name, form)
            continue
        if not (tpl / f"{form}.template.json").exists():
            entry.update({"form": f"skip:no-template:{form}", "reason": "không có template cho loại phiếu này"})
            manifest["documents"].append(entry)
            continue
        if progress:
            progress(seq, len(pdfs), pdf.name, form)
        warns = []
        if info["suspect_ratio"] > 0.05:
            warns.append(f"TEXT_SUSPECT ratio={info['suspect_ratio']} (PDF có thể cần OCR/vision)")
        data = extract_one(form, pages, tpl, llm, cfg.rules.max_tokens_extract)
        template = load(tpl / f"{form}.template.json")
        warns += reconcile_keys(template, data) + check_keys(template, data) + check_patient(expected_name, data)
        expected_name = expected_name or find_patient_name(data)
        out_name = f"{seq:02d}_{form}.data.json"
        dump(data, docs_dir / out_name)
        entry.update({"data_file": out_name, "warnings": warns})
        manifest["documents"].append(entry)
    dump(manifest, out_dir / "manifest.json")
    return manifest
