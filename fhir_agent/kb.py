"""Cầu nối tới plugin vn-fhir: dùng chung scripts + references của skill `vn-fhir-convert` (một nguồn sự thật).

Harness không chép lại logic mã hoá / kiểm tra / dựng FHIR — nó import thẳng các script của skill:
  vnlib (tra ICD-10 VN, LOINC curated, CLS QĐ 1227, UCUM, CodeSystem VN Core), inventory, quality_check, build_fhir,
  validate_fhir, compare; và đọc references/*.md để đưa hướng dẫn theo loại phiếu vào prompt.
"""
from __future__ import annotations

import importlib
import re
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "vn-fhir" / "skills" / "vn-fhir-convert"
SCRIPTS = SKILL / "scripts"
REFS = SKILL / "references"


def load(skill_dir: str | Path | None = None, vendor: str | Path | None = None):
    """Import các module của skill; trả về namespace có V, inventory, qc, build, validate, compare."""
    global SKILL, SCRIPTS, REFS
    if skill_dir:
        SKILL = Path(skill_dir)
        SCRIPTS, REFS = SKILL / "scripts", SKILL / "references"
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    V = importlib.import_module("vnlib")
    V.find_vendor(str(vendor) if vendor else None)
    ns = type("KB", (), {})()
    ns.V = V
    ns.inventory = importlib.import_module("inventory")
    ns.qc = importlib.import_module("quality_check")
    ns.build = importlib.import_module("build_fhir")
    ns.validate = importlib.import_module("validate_fhir")
    ns.compare = importlib.import_module("compare")
    ns.validate.load_ig(V.vendor())
    return ns


@lru_cache(maxsize=None)
def doc_type_rows() -> dict[str, str]:
    """form key -> dòng hướng dẫn trong references/document-types.md (cột: mẫu, LOINC, resource, ghi chú)."""
    rows = {}
    for line in (REFS / "document-types.md").read_text(encoding="utf-8").splitlines():
        m = re.match(r"\|\s*`([^`]+)`(?:\s*/\s*`([^`]+)`)?\s*\|(.*)\|\s*$", line)
        if m:
            cells = [c.strip() for c in m.group(3).split("|")]
            text = f"Mẫu: {cells[0]}. Resource: {cells[2]}. Lưu ý: {cells[3]}" if len(cells) >= 4 else " | ".join(cells)
            rows[m.group(1)] = text
            if m.group(2):
                rows[m.group(2)] = text
    return rows


@lru_cache(maxsize=None)
def doc_section(title_prefix: str) -> str:
    """Một mục '## …' của document-types.md (vd 'Thứ tự ưu tiên', 'Thời điểm')."""
    txt = (REFS / "document-types.md").read_text(encoding="utf-8")
    m = re.search(rf"^## {re.escape(title_prefix)}.*?(?=^## |\Z)", txt, re.S | re.M)
    return m.group(0).strip() if m else ""


@lru_cache(maxsize=None)
def pitfalls() -> list[str]:
    out = []
    for line in (REFS / "pitfalls.md").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\d+\.\s+(.*)", line)
        if m:
            out.append(m.group(1).strip())
    return out


def pitfalls_for(keywords: list[str]) -> list[str]:
    ks = [k.lower() for k in keywords]
    return [p for p in pitfalls() if any(k in p.lower() for k in ks)]


def enum_text(V, cs_id: str, limit: int = 80) -> str:
    cs = V.cs_concepts(cs_id)
    items = [f"{c['code']}={(c['display'] or '').strip()}" for c in cs[:limit]]
    return f"{cs_id}: " + "; ".join(items)
