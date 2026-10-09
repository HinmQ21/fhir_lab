"""term_server.py — MCP server (stdio) bọc các hàm tra cứu của vnlib, để Claude gọi như tool thay vì CLI.

Cần `pip install mcp`. Không có SDK thì in hướng dẫn và thoát 0 (plugin vẫn dùng được qua scripts/term_lookup.py).
Tất cả tool chỉ đọc danh mục offline (vendor VN Core + bảng curated); duy nhất `loinc_check(online=True)` gọi NLM.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vnlib as V  # noqa: E402

try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # pragma: no cover
    sys.stderr.write("[vn-terminology] thiếu gói `mcp` (pip install mcp). Dùng CLI: python -I scripts/term_lookup.py ...\n")
    sys.exit(0)

ASSETS = Path(__file__).resolve().parent.parent / "assets"
mcp = FastMCP("vn-terminology", instructions="Tra cứu mã VN Core/ICD-10/LOINC/CLS/UCUM. Luôn chọn từ ứng viên trả về; không bịa mã. Ứng viên rỗng => để text và ghi unmapped.")


def _j(o):
    return json.dumps(o, ensure_ascii=False)


@mcp.tool()
def icd10_search(text: str, top: int = 8) -> str:
    """Ứng viên ICD-10 VN theo tên bệnh tiếng Việt (ratio khớp mờ, tên EN WHO)."""
    return _j(V.icd10_search(text, top))


@mcp.tool()
def icd10_code(code: str) -> str:
    """Tra một mã ICD-10 (VN + EN)."""
    return _j(V.icd10_lookup(code) or {"found": False})


@mcp.tool()
def cls_search(text: str, unit: str | None = None, appendix: str | None = None, top: int = 5) -> str:
    """Ứng viên mã dịch vụ CLS QĐ 1227 (vn-cls-cs) theo tên, nhạy đơn vị; kèm gợi ý LOINC từ ConceptMap."""
    return _j(V.cls_search(text, appendix, top, unit))


@mcp.tool()
def lab_loinc(text: str, unit: str | None = None, panel: str | None = None) -> str:
    """Ứng viên LOINC xét nghiệm (bảng curated đã kiểm NLM), xếp hạng theo tên + loại đơn vị + bệnh phẩm. panel: huyet_hoc|sinh_hoa|nuoc_tieu."""
    rows = V.lab_loinc_candidates(text, unit, panel)
    ver = V.read_json(ASSETS / "loinc_verified.json") if (ASSETS / "loinc_verified.json").exists() else {}
    for r in rows:
        r["loinc_status"] = (ver.get(r["code"]) or {}).get("status") if r["code"] in ver else "chưa kiểm"
    return _j(rows)


@mcp.tool()
def imaging_loinc(text: str) -> str:
    """Ứng viên LOINC + CLS cho CĐHA/TDCN theo tên dịch vụ."""
    return _j(V.imaging_candidates(text))


@mcp.tool()
def vital_sign(text: str) -> str:
    """LOINC + đơn vị cố định (fixedString) + profile VN Core cho sinh hiệu (mạch, nhiệt độ, huyết áp, nhịp thở, SpO2, cân nặng, chiều cao, BMI)."""
    key = V.VITAL_ALIASES.get(V.norm(text)) or next((k for al, k in V.VITAL_ALIASES.items() if V.norm(al) in V.norm(text)), None)
    if not key:
        return _j({"found": False})
    loinc, en, vi, uc, label, prof = V.VITALS[key]
    return _j({"key": key, "loinc": loinc, "display": en, "display_vi": vi, "ucum": uc, "unit_label_fixed": label, "profile": prof})


@mcp.tool()
def codesystem_search(cs_id: str, text: str, top: int = 8) -> str:
    """Tìm mã trong một CodeSystem VN Core (ví dụ vn-department-cs, vn-specialty-cs, vn-qualification-cs, vn-province-cs, vn-ward-cs)."""
    return _j(V.cs_search(cs_id, text, top))


@mcp.tool()
def codesystem_display(cs_id: str, code: str) -> str:
    """Hiển thị (display) của một mã trong CodeSystem VN Core; null nếu không tồn tại."""
    return _j({"code": code, "display": V.cs_display(cs_id, code)})


@mcp.tool()
def valueset_contains(vs_id: str, code: str) -> str:
    """Mã có thuộc ValueSet VN Core (expansion offline) không."""
    m = V.vs_members(vs_id)
    return _j({"valueset": vs_id, "code": code, "member": None if m is None else any(c == code for _, c in m)})


@mcp.tool()
def ucum(unit: str) -> str:
    """Đơn vị in trên phiếu -> mã UCUM + nhãn chuẩn."""
    code, label = V.ucum(unit)
    return _j({"unit_raw": unit, "ucum": code, "unit": label})


@mcp.tool()
def loinc_check(code: str, online: bool = False) -> str:
    """Kiểm tra mã LOINC: check digit, bảng đã kiểm (null = không tồn tại, DEPRECATED), tuỳ chọn tra NLM."""
    ver = V.read_json(ASSETS / "loinc_verified.json") if (ASSETS / "loinc_verified.json").exists() else {}
    r = {"code": code, "check_digit_ok": V.loinc_check_digit(code), "verified_entry": ver.get(code, "n/a")}
    if online:
        try:
            r["nlm"] = V.loinc_online(code)
        except Exception as e:
            r["nlm_error"] = str(e)
    return _j(r)


@mcp.tool()
def parse_person(label: str) -> str:
    """Tách nhãn ký ('BSCKII Hà Xuân Tài') -> tên, họ/tên, mã chức danh vn-qualification-cs."""
    p = V.parse_person(label)
    fam, giv = V.split_name(p["name"])
    return _j(dict(p, family=fam, given=giv))


@mcp.tool()
def vn_datetime(text: str) -> str:
    """Chuỗi giờ/ngày kiểu Việt Nam ('08h34 02/02/2026', '02/02/2026') -> ISO 8601 +07:00."""
    return _j({"text": text, "iso": V.vn_datetime(text)})


@mcp.tool()
def doc_type(form: str) -> str:
    """LOINC DocumentReference.type cho một loại phiếu (khoá form của vnlib.DOC_TYPES)."""
    t = V.DOC_TYPES.get(form)
    return _j({"form": form, "loinc": t[0] if t else None, "display": t[1] if t else None, "known_forms": sorted(V.DOC_TYPES) if not t else None})


if __name__ == "__main__":
    V.find_vendor(None)
    mcp.run()
