"""BƯỚC 1 (làm tay): zip HSBA -> phân loại từng PDF -> trích xuất JSON tài liệu theo template TT32.

Đầu ra: output/<case>/01_documents/<seq>_<form>.data.json + manifest.json
Cách làm: text PDF (pymupdf, giải mã font GID) + template.json -> LLM (OpenAI-compatible) trả JSON đúng khung template.
"""
from __future__ import annotations

import re
import sys
import unicodedata
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, dump, llm_json, load, pdf_pages_text, sha1_b64  # noqa: E402

CASE = sys.argv[1] if len(sys.argv) > 1 else "BN001736827"
ZIP = ROOT / "data" / "sample" / f"HSBA-{CASE}.zip"
OUT = ROOT / "output" / CASE
RAW = OUT / "00_raw"
DOCS = OUT / "01_documents"
TPL = ROOT / "config" / "templates"

# ---------------------------------------------------------------- 1a. phân loại theo tên file + nội dung
# (trong pipeline thật, bước này do ClassifyRAG đảm nhiệm; ở đây làm tay bằng luật)
SKIP_RULES = [
    (r"gi[aấ]y\s*v[aà]o\s*vi[eệ]n|giấy vv|_vv\b", "giay-vao-vien"),
    (r"_cls\b|\bcls\.pdf", "phieu-cls"),
    (r"t[eé]\s*ng[aã]", "phieu-danh-gia-nguy-co-te-nga"),
    (r"dinh\s*d[uưỡ]+ng", "phieu-sang-loc-dinh-duong"),
]
FORM_RULES = [
    (r"kh[aá]m\s*b[eệ]nh\s*v[aà]o\s*vi[eệ]n", "phieu-kham-benh-vao-vien"),
    (r"huy[eế]t\s*h[oọ]c", "ket-qua-xet-nghiem-huyet-hoc"),
    (r"sinh\s*h[oó]a", "ket-qua-xet-nghiem-sinh-hoa-mau"),
    (r"n[uư][oớ]c\s*ti[eể]u", "phieu-xet-nghiem-nuoc-dich"),
    (r"c[oộ]ng\s*h[uưở]+ng\s*t[uừ]", "phieu-chup-cong-huong-tu"),
    (r"si[eê]u\s*[aâ]m", "phieu-sieu-am-doppler"),
    (r"[dđ]i[eệ]n\s*n[aã]o", "phieu-dien-nao-do"),
    (r"theo\s*d[oõ]i\s*v[aà]\s*ch[aă]m\s*s[oó]c|ch[aă]m\s*s[oó]c", "phieu-cham-soc"),
    (r"h[oộ]i\s*ch[aẩ]n", "trich-bien-ban-hoi-chan"),
    (r"[dđ]i[eề]u\s*tr[iị]", "phieu-dieu-tri"),
    (r"t[oó]m\s*t[aắ]t", "ban-tom-tat-ho-so-benh-an"),
    (r"c[oô]ng\s*khai", "phieu-cong-khai-dich-vu"),
    (r"thanh\s*to[aá]n|b[aả]ng\s*k[eê]", "bang-ke-chi-phi-kcb"),
]


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def classify(name: str, first_page_text: str) -> tuple[str, str]:
    """-> (form_key, reason). form_key bắt đầu bằng 'skip:' nếu bỏ qua."""
    n = nfc(name).lower()
    for pat, key in SKIP_RULES:
        if re.search(pat, n):
            return f"skip:{key}", "skip-list theo yêu cầu"
    t = nfc(first_page_text[:600]).upper()
    if "BẢNG QUY ƯỚC MÃ HOÁ" in t or "HƯỚNG DẪN" in t and "GHI PHIẾU" in t:
        return "skip:phu-luc-huong-dan", "trang phụ lục hướng dẫn, không có dữ liệu người bệnh"
    for pat, key in FORM_RULES:
        if re.search(pat, n):
            return key, f"tên file khớp /{pat}/"
    return "skip:unknown", "không nhận dạng được"


SYSTEM = """Bạn là hệ thống trích xuất thông tin (KIE) cho hồ sơ bệnh án điện tử Việt Nam.
Nhiệm vụ: điền giá trị vào TEMPLATE JSON dựa trên TEXT của một phiếu (text đã được trích từ PDF theo thứ tự đọc; bảng có thể bị tách mỗi ô một dòng).
Quy tắc bắt buộc:
- Trả về DUY NHẤT một JSON object có đúng cấu trúc khoá của TEMPLATE (giữ nguyên mọi khoá, kể cả khoá lồng nhau). Không giải thích, không markdown.
- Mỗi giá trị trong TEMPLATE là MÔ TẢ cách điền; thay nó bằng giá trị thực đọc được. Giá trị kiểu chuỗi giữ NGUYÊN VĂN như in trên phiếu (không dịch, không sửa chính tả, không chuẩn hoá ngày/số, giữ số 0 ở đầu).
- Ô trống / không có trên phiếu -> null. Checkbox -> true/false. Mảng -> liệt kê đủ mọi phần tử theo thứ tự trên phiếu; mảng rỗng [] nếu không có.
- Bảng kết quả xét nghiệm: mỗi dòng chỉ số là một phần tử; các cột KẾT QUẢ/ĐVT/Giá trị BT/Ghi chú/Quy trình lấy theo đúng dòng; cờ H/L in trước giá trị là 'Đánh dấu'.
- Không bịa thông tin không có trên phiếu."""


def extract_one(form_key: str, pages: list[str]) -> dict:
    template = load(TPL / f"{form_key}.template.json")
    text = "\n\n".join(f"===== TRANG {i+1} =====\n{p}" for i, p in enumerate(pages))
    user = f"TEMPLATE:\n{__import__('json').dumps(template, ensure_ascii=False, indent=1)}\n\nTEXT PHIẾU:\n{text}\n\nJSON kết quả:"
    return llm_json(SYSTEM, user, thinking=False, max_tokens=24000)


KEY_SYNONYMS = {"Đơn vị tính": "Đơn vị", "Đơn vị": "Đơn vị tính", "Giá trị BT": "Giá trị bình thường", "Giá trị bình thường": "Giá trị BT",
                "Mã BN": "Mã bệnh nhân", "Mã người bệnh": "Mã bệnh nhân"}


def reconcile_keys(template, data) -> list[str]:
    """Model đôi khi dùng tên khoá đồng nghĩa (vd 'Đơn vị tính' thay 'Đơn vị'): đổi về đúng khoá template.
    Quy tắc: (1) bảng đồng nghĩa; (2) ở một mức có đúng 1 khoá thiếu và 1 khoá thừa -> đổi tên. Trả về danh sách sửa."""
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


def find_patient_name(data) -> str | None:
    """Tìm họ tên người bệnh ở bất kỳ mức nào của JSON tài liệu (khoá bắt đầu bằng 'Họ' hoặc 'Tên bệnh nhân')."""
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
    """Chốt chặn chống bịa: tên người bệnh trên phiếu phải trùng với phiếu đầu tiên của hồ sơ."""
    got = find_patient_name(data)
    if expected and got and nfc(got).strip().lower() != nfc(expected).strip().lower():
        return [f"PATIENT_MISMATCH expected='{expected}' got='{got}'"]
    return []


def check_keys(template, data, path="") -> list[str]:
    """Cảnh báo MISSING_KEYS / EXTRA_KEYS so với template (giống postprocess của pipeline gốc)."""
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


def main():
    RAW.mkdir(parents=True, exist_ok=True)
    DOCS.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ZIP) as z:
        z.extractall(RAW)
    pdfs = sorted(RAW.rglob("*.pdf"), key=lambda p: int(re.match(r"(\d+)", p.name).group(1)) if re.match(r"(\d+)", p.name) else 999)
    manifest = {"case": CASE, "source_zip": ZIP.name, "documents": []}
    expected_name = None
    for seq, pdf in enumerate(pdfs, 1):
        pages = pdf_pages_text(pdf)
        form, reason = classify(pdf.stem, pages[0] if pages else "")
        entry = {"seq": seq, "file": pdf.name, "pages": len(pages), "form": form, "reason": reason,
                 "size_bytes": pdf.stat().st_size, "sha1_b64": sha1_b64(pdf)}
        (RAW / (pdf.stem + ".txt")).write_text("\n\n".join(pages), encoding="utf-8")
        if form.startswith("skip:"):
            print(f"[{seq:02d}] SKIP   {pdf.name}  ({reason})")
            manifest["documents"].append(entry)
            continue
        # phiếu chăm sóc: chỉ trang có dữ liệu (trang hướng dẫn đã bị skip ở classify)
        print(f"[{seq:02d}] {form:<32} {pdf.name}  pages={len(pages)}", flush=True)
        data = extract_one(form, pages)
        fixes = reconcile_keys(load(TPL / f"{form}.template.json"), data)
        warns = fixes + check_keys(load(TPL / f"{form}.template.json"), data) + check_patient(expected_name, data)
        expected_name = expected_name or find_patient_name(data)
        out_name = f"{seq:02d}_{form}.data.json"
        dump(data, DOCS / out_name)
        entry.update({"data_file": out_name, "warnings": warns})
        if warns:
            print("      warnings:", warns[:8])
        manifest["documents"].append(entry)
    dump(manifest, OUT / "manifest.json")
    print("manifest ->", OUT / "manifest.json")


if __name__ == "__main__":
    main()
