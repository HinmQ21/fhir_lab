"""vnlib — thư viện dùng chung cho các script của skill vn-fhir-convert (không phụ thuộc gói ngoài).

Nội dung:
  - tìm thư mục vendor (VN Core IG 0.10.0 + từ điển ICD-10) : find_vendor()
  - hằng số hệ thống mã (SYS), helper PROF/CS/SID, chuẩn hoá chuỗi (nfc/norm/slug)
  - ICD-10 VN: icd10_lookup(code), icd10_search(text)
  - Danh mục CLS (QĐ 1227) + ConceptMap CLS->LOINC: cls_search(name, appendix, top)
  - CodeSystem VN Core: cs_concepts(id), cs_display(id, code), vs_members(id)
  - LOINC: loinc_check_digit(code), bảng curated LAB_LOINC/VITALS/IMAGING/DOC_TYPES/SECTIONS (đã đối chiếu NLM 2026-10-08),
           lab_loinc_candidates(name, unit_raw), imaging_candidates(name), loinc_online(code) (tuỳ chọn, cần mạng)
  - UCUM: ucum(unit_raw)
  - Ngày giờ Việt Nam -> ISO 8601 (+07:00): vn_datetime(text); số: to_number; khoảng tham chiếu: parse_ref_range
  - Họ tên & học hàm/học vị: split_name, parse_person
"""
from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _stream in (sys.stdout, sys.stderr):  # console Windows mặc định cp1252 -> in tiếng Việt lỗi; ép UTF-8 (kể cả khi chạy python -I)
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
VN = "http://fhir.hl7.org.vn/core"
TH = "http://terminology.hl7.org/CodeSystem/"
SYS = {
    "loinc": "http://loinc.org", "ucum": "http://unitsofmeasure.org", "snomed": "http://snomed.info/sct",
    "icd10vn": f"{VN}/CodeSystem/vn-icd10-cs", "icd10who": "http://hl7.org/fhir/sid/icd-10", "cls": f"{VN}/CodeSystem/vn-cls-cs",
    "atc": "http://www.whocc.no/atc", "rxnorm": "http://www.nlm.nih.gov/research/umls/rxnorm",
    "v2-0487": TH + "v2-0487", "v2-0074": TH + "v2-0074", "v2-0203": TH + "v2-0203",
}
PROF = lambda p: f"{VN}/StructureDefinition/{p}"
CS = lambda c: c if c.startswith("http") else f"{VN}/CodeSystem/{c}"
SID = lambda s: f"{VN}/sid/{s}"


def system_url(s: str) -> str:
    """'loinc' | 'vn-ethnicity-cs' | 'http://...' -> URL đầy đủ."""
    if not s:
        return s
    if s in SYS:
        return SYS[s]
    if s.startswith("http") or s.startswith("urn:"):
        return s
    return CS(s)


# ------------------------------------------------------------------ vendor
_VENDOR: Path | None = None


def find_vendor(explicit: str | None = None) -> Path:
    """Thứ tự: tham số --vendor > env VN_FHIR_VENDOR > đi lên từ cwd tìm vendor/vncore > cạnh plugin."""
    global _VENDOR
    cands = []
    if explicit:
        cands.append(Path(explicit))
    if os.environ.get("VN_FHIR_VENDOR"):
        cands.append(Path(os.environ["VN_FHIR_VENDOR"]))
    p = Path.cwd()
    for _ in range(6):
        cands.append(p / "vendor")
        p = p.parent
    cands.append(HERE.parents[3] / "vendor")
    for c in cands:
        if (c / "vncore" / "package").is_dir():
            _VENDOR = c.resolve()
            return _VENDOR
    raise SystemExit("Không tìm thấy vendor/vncore/package (VN Core IG). Dùng --vendor hoặc env VN_FHIR_VENDOR.")


def vendor() -> Path:
    return _VENDOR or find_vendor()


def read_json(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def write_json(obj, p):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ chuỗi
def nfc(s):
    return unicodedata.normalize("NFC", s) if isinstance(s, str) else s


def norm(s: str) -> str:
    s = nfc(s or "").lower()
    s = re.sub(r"[\*\(\)\[\]\{\}:;,\.]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def slug(s: str) -> str:
    s = nfc(s or "").replace("Đ", "D").replace("đ", "d")
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "x"


# ------------------------------------------------------------------ ICD-10 VN
@lru_cache(maxsize=1)
def _icd():
    d = read_json(vendor() / "icd10" / "icd10_vn.json")
    # vài mã 3 ký tự thiếu trong file nguồn (QĐ 4469) nhưng có trong ICD-10 WHO
    d.setdefault("R51", {"vi": "Đau đầu", "en": "Headache", "chapter": "Các triệu chứng, dấu hiệu và những biểu hiện lâm sàng bất thường"})
    d.setdefault("R42", {"vi": "Chóng mặt và choáng váng", "en": "Dizziness and giddiness", "chapter": "Các triệu chứng, dấu hiệu và những biểu hiện lâm sàng bất thường"})
    return d


def icd10_lookup(code: str) -> dict | None:
    if not code:
        return None
    c = code.strip().upper()
    return _icd().get(c) or _icd().get(c.replace(".", ""))


@lru_cache(maxsize=1)
def _icd_who():
    """Bổ sung ICD-10 WHO 2019 (tiếng Anh) cho các chương file VN thiếu (R, V–Z, một số mã cũ). Tạo bằng simple-icd-10."""
    f = vendor() / "icd10" / "icd10_who2019_supplement.json"
    if not f.exists():
        return {}
    d = read_json(f)
    d.pop("_meta", None)
    return d


def icd10_dotted(code: str | None) -> str | None:
    """Chuẩn hoá 'S060' -> 'S06.0', ' k61.2 ' -> 'K61.2'. Trả None nếu không đúng dạng ICD-10."""
    if not code:
        return None
    c = re.sub(r"\s+", "", str(code)).upper()
    if re.fullmatch(r"[A-Z][0-9]{2}[0-9A-Z]{1,2}", c):
        c = c[:3] + "." + c[3:]
    return c if re.fullmatch(r"[A-Z][0-9]{2}(\.[0-9A-Z]{1,2})?", c) else None


def icd10_verify(code: str | None) -> tuple[str | None, str]:
    """-> (mã chấm chuẩn, trạng thái): vn | who2019 | parent-only (mã con không có trong 2 nguồn, mã 3 ký tự có) | invalid."""
    c = icd10_dotted(code)
    if not c:
        return None, "invalid"
    if icd10_lookup(c):
        return c, "vn"
    if c in _icd_who():
        return c, "who2019"
    if icd10_lookup(c[:3]) or c[:3] in _icd_who():
        return c, "parent-only"
    return c, "invalid"


def icd10_search(text: str, top: int = 8) -> list[dict]:
    """Ứng viên ICD-10 theo tên bệnh tiếng Việt (so khớp mờ). Luôn trả danh sách để người/LLM CHỌN, không tự bịa."""
    q = norm(text)
    out = []
    table = _icd()
    for code, info in table.items():
        if "." not in code and len(code) == 4 and f"{code[:3]}.{code[3:]}" in table:
            continue  # bản không dấu chấm trùng với bản có dấu chấm -> chỉ giữ dạng chuẩn M50.2
        vi = norm(info.get("vi", ""))
        r = SequenceMatcher(None, q, vi).ratio()
        if q and (q in vi or vi in q):
            r = max(r, 0.85)
        if r >= 0.5:
            out.append({"code": code, "display": info.get("vi"), "display_en": info.get("en"), "ratio": round(r, 3)})
    # bổ sung WHO 2019 (chỉ tên tiếng Anh: chương R, V–Z…) — truy vấn bằng tiếng Anh để thấy, vd "abdominal pain"
    for code, info in _icd_who().items():
        en = norm(info.get("en", ""))
        r = SequenceMatcher(None, q, en).ratio()
        if q and len(q) >= 4 and q in en:
            r = max(r, 0.8)
        if r >= 0.5:
            out.append({"code": code, "display": None, "display_en": info.get("en"), "ratio": round(r, 3), "source_table": "who2019"})
    out.sort(key=lambda x: -x["ratio"])
    return out[:top]


ICD_RE = re.compile(r"\b([A-TV-Z][0-9]{2}(?:\.[0-9]{1,2})?)\b")


# ------------------------------------------------------------------ CodeSystem / ValueSet VN Core
def _cs_file(cs_id: str) -> Path | None:
    for d in ("package", "terminology"):
        f = vendor() / "vncore" / d / f"CodeSystem-{cs_id}.json"
        if f.exists():
            return f
    return None


def _flat(concepts, acc, parent=None):
    for c in concepts or []:
        acc.append({"code": c["code"], "display": c.get("display"), "parent": parent,
                    "props": {p["code"]: (p.get("valueString") or p.get("valueCode") or p.get("valueCoding", {}).get("code")) for p in c.get("property", [])}})
        _flat(c.get("concept"), acc, c["code"])


@lru_cache(maxsize=None)
def cs_concepts(cs_id: str) -> tuple:
    f = _cs_file(cs_id)
    if not f:
        return ()
    acc: list = []
    _flat(read_json(f).get("concept"), acc)
    return tuple(acc)


def cs_display(cs_id: str, code: str) -> str | None:
    for c in cs_concepts(cs_id):
        if c["code"] == code:
            return c["display"]
    return None


def cs_search(cs_id: str, text: str, top: int = 8) -> list[dict]:
    q = norm(text)
    out = []
    for c in cs_concepts(cs_id):
        d = norm(c["display"] or "")
        r = SequenceMatcher(None, q, d).ratio()
        if q and (q in d or d in q):
            r = max(r, 0.85)
        if r >= 0.45:
            out.append({**c, "ratio": round(r, 3)})
    out.sort(key=lambda x: -x["ratio"])
    return out[:top]


@lru_cache(maxsize=1)
def _expansions():
    f = vendor() / "vncore" / "expansions" / "expansions-subset.json"
    return read_json(f) if f.exists() else {}


def vs_members(vs_id: str) -> set[tuple[str, str]] | None:
    """Tập (system, code) của ValueSet VN Core nếu tính được offline."""
    f = vendor() / "vncore" / "package" / f"ValueSet-{vs_id}.json"
    if not f.exists():
        return None
    vs = read_json(f)
    if "expansion" in vs:
        return {(c["system"], c["code"]) for c in vs["expansion"].get("contains", [])}
    acc: set = set()
    for inc in vs.get("compose", {}).get("include", []):
        if "concept" in inc:
            acc |= {(inc["system"], c["code"]) for c in inc["concept"]}
        elif "filter" in inc or "valueSet" in inc:
            return None
        else:
            cs_id = inc.get("system", "").rsplit("/", 1)[-1]
            cons = cs_concepts(cs_id)
            if not cons:
                exp = _expansions().get(vs_id)
                return {(c["system"], c["code"]) for c in exp.get("contains", [])} if exp else None
            acc |= {(inc["system"], c["code"]) for c in cons}
    return acc


# ------------------------------------------------------------------ CLS (QĐ 1227) -> LOINC
@lru_cache(maxsize=1)
def _cls_rows():
    cs = read_json(vendor() / "vncore" / "terminology" / "CodeSystem-vn-cls-cs.json")
    cm = read_json(vendor() / "vncore" / "terminology" / "ConceptMap-vn-cm-cls-to-loinc.json")
    targets = {}
    for g in cm["group"]:
        for e in g["element"]:
            targets[e["code"]] = [{"code": t["code"], "display": t.get("display"), "equivalence": t.get("equivalence")} for t in e.get("target", [])]
    rows = []
    for c in cs["concept"]:
        props = {p["code"]: (p.get("valueString") or p.get("valueCode")) for p in c.get("property", [])}
        rows.append({"code": c["code"], "display": c.get("display", ""), "norm": norm(c.get("display", "")), "appendix": props.get("appendix"),
                     "ucum": props.get("ucum-unit"), "ma_kt_tt23": props.get("ma-kt-tt23"), "loinc": targets.get(c["code"], [])})
    return rows


APPENDIX = {"01": "Huyết học", "02": "Hoá sinh", "03": "Vi sinh", "04": "Giải phẫu bệnh", "05": "Chẩn đoán hình ảnh"}


def cls_search(name: str, appendix: str | None = None, top: int = 5, unit_raw: str | None = None) -> list[dict]:
    """Ứng viên mã CLS (QĐ 1227) theo tên dịch vụ. appendix '01' huyết học, '02' hoá sinh, '03' vi sinh, '04' GPB, '05' CĐHA.
    Nếu có unit_raw, ứng viên có ucum-unit khớp được cộng điểm (phân biệt mg/dL vs mmol/L)."""
    q = norm(name)
    uc = ucum(unit_raw)[0] if unit_raw else None
    scored = []
    for r in _cls_rows():
        if appendix and r["appendix"] != appendix:
            continue
        ratio = SequenceMatcher(None, q, r["norm"]).ratio()
        if q and (q in r["norm"] or r["norm"] in q):
            ratio = max(ratio, 0.9)
        if uc and r["ucum"]:
            ratio += 0.05 if uc in [u.strip() for u in r["ucum"].split(";")] else -0.05
        scored.append((ratio, r))
    scored.sort(key=lambda x: -x[0])
    return [{**r, "ratio": round(s, 3)} for s, r in scored[:top]]


# ------------------------------------------------------------------ UCUM
UCUM = {
    "g/l": ("g/L", "g/L"), "g/dl": ("g/dL", "g/dL"), "G/L": ("10*9/L", "10^9/L"), "T/L": ("10*12/L", "10^12/L"), "k/ul": ("10*3/uL", "10^3/µL"),
    "%": ("%", "%"), "fl": ("fL", "fL"), "pg": ("pg", "pg"), "mmol/l": ("mmol/L", "mmol/L"), "µmol/l": ("umol/L", "µmol/L"), "umol/l": ("umol/L", "µmol/L"),
    "u/l": ("U/L", "U/L"), "ui/l": ("U/L", "U/L"), "mg/l": ("mg/L", "mg/L"), "mg/dl": ("mg/dL", "mg/dL"), "ng/ml": ("ng/mL", "ng/mL"), "mg/mmol": ("mg/mmol", "mg/mmol"),
    "/µl": ("/uL", "/µL"), "/ul": ("/uL", "/µL"), "ml/phút": ("mL/min", "mL/min"), "ml/phút/1.73m2": ("mL/min/{1.73_m2}", "mL/min/1.73m2"),
    "lần/phút": ("/min", "lần/phút"), "lần/ph": ("/min", "lần/phút"), "l/p": ("/min", "lần/phút"), "ck/p": ("/min", "lần/phút"), "nhịp/phút": ("/min", "lần/phút"),
    "ºc": ("Cel", "°C"), "°c": ("Cel", "°C"), "oc": ("Cel", "°C"), "độ c": ("Cel", "°C"), "mmhg": ("mm[Hg]", "mmHg"),
    "kg": ("kg", "kg"), "cm": ("cm", "cm"), "kg/m2": ("kg/m2", "kg/m2"), "viên": ("{tbl}", "viên"), "ống": ("{vial}", "ống"), "lọ": ("{vial}", "lọ"), "gói": ("{pck}", "gói"),
    "ml": ("mL", "mL"), "mg": ("mg", "mg"), "g": ("g", "g"), "mcg": ("ug", "µg"), "µg": ("ug", "µg"), "ui": ("[iU]", "IU"), "iu": ("[iU]", "IU"), "ph": ("[pH]", "pH"),
    "ngày": ("d", "ngày"), "lần": ("1", "lần"), "chiếc": ("1", "chiếc"),
}


def ucum(unit: str | None) -> tuple[str | None, str | None]:
    """-> (mã UCUM, nhãn hiển thị). (None, unit) nếu không biết."""
    if not unit:
        return None, None
    u = unit.strip()
    if u in UCUM:
        return UCUM[u]
    k = u.lower()
    if k in UCUM:
        return UCUM[k]
    return None, u


# ------------------------------------------------------------------ LOINC
def loinc_check_digit(code: str) -> bool:
    """Kiểm tra chữ số kiểm tra LOINC (mod 10, 'Luhn-like'). '6690-2' -> True."""
    m = re.fullmatch(r"(\d{1,7})-(\d)", (code or "").strip())
    if not m:
        return False
    digits, chk = m.group(1), int(m.group(2))
    rev = digits[::-1]
    odd = rev[0::2][::-1]  # vị trí lẻ tính từ phải, ghép lại theo thứ tự trái->phải
    even = rev[1::2]
    prod = str(int(odd) * 2) if odd else "0"
    total = sum(int(x) for x in prod) + sum(int(x) for x in even)
    return (10 - total % 10) % 10 == chk


# Bảng curated — đã kiểm tra tồn tại/tên/thuộc tính trên NLM Clinical Tables (LOINC) ngày 2026-10-08.
# Mỗi dòng: aliases (đã norm), unit_kind ('count','frac','molar','mass','enz','pres','other'), loinc, display EN, display VI
LAB_LOINC = [
    # --- Huyết học (máy đếm laser)
    (["số lượng bạch cầu", "wbc", "bạch cầu"], "count", "6690-2", "Leukocytes [#/volume] in Blood by Automated count", "Số lượng bạch cầu"),
    (["trung tính g/l", "trung tính", "neut#", "neu#", "bạch cầu trung tính"], "count", "751-8", "Neutrophils [#/volume] in Blood by Automated count", "Bạch cầu trung tính"),
    (["lympho g/l", "lympho", "lym#", "bạch cầu lympho"], "count", "731-0", "Lymphocytes [#/volume] in Blood by Automated count", "Bạch cầu lympho"),
    (["mono g/l", "mono", "mon#", "bạch cầu mono"], "count", "742-7", "Monocytes [#/volume] in Blood by Automated count", "Bạch cầu mono"),
    (["ưa axít g/l", "ưa acid g/l", "ưa axít", "ưa acid", "eos#", "bạch cầu ưa acid"], "count", "711-2", "Eosinophils [#/volume] in Blood by Automated count", "Bạch cầu ưa acid"),
    (["ưa bazơ g/l", "ưa bazơ", "baso#", "bạch cầu ưa bazơ"], "count", "704-7", "Basophils [#/volume] in Blood by Automated count", "Bạch cầu ưa bazơ"),
    (["sl bc hạt chưa trưởng thành", "ig#", "bạch cầu hạt chưa trưởng thành"], "count", "53115-2", "Immature granulocytes [#/volume] in Blood by Automated count", "Bạch cầu hạt chưa trưởng thành"),
    (["trung tính %", "neut%", "neu%", "tỷ lệ bạch cầu trung tính"], "frac", "770-8", "Neutrophils/100 leukocytes in Blood by Automated count", "Tỷ lệ bạch cầu trung tính"),
    (["lympho %", "lym%", "tỷ lệ bạch cầu lympho"], "frac", "736-9", "Lymphocytes/100 leukocytes in Blood by Automated count", "Tỷ lệ bạch cầu lympho"),
    (["mono %", "mon%", "tỷ lệ bạch cầu mono"], "frac", "5905-5", "Monocytes/100 leukocytes in Blood by Automated count", "Tỷ lệ bạch cầu mono"),
    (["ưa acid %", "ưa axít %", "eos%", "tỷ lệ bạch cầu ưa acid"], "frac", "713-8", "Eosinophils/100 leukocytes in Blood by Automated count", "Tỷ lệ bạch cầu ưa acid"),
    (["ưa bazơ %", "baso%", "tỷ lệ bạch cầu ưa bazơ"], "frac", "706-2", "Basophils/100 leukocytes in Blood by Automated count", "Tỷ lệ bạch cầu ưa bazơ"),
    (["tỷ lệ bc hạt chưa trưởng thành", "ig%"], "frac", "71695-1", "Immature granulocytes/100 leukocytes in Blood by Automated count", "Tỷ lệ bạch cầu hạt chưa trưởng thành"),
    (["số lượng hồng cầu", "rbc", "hồng cầu"], "count", "789-8", "Erythrocytes [#/volume] in Blood by Automated count", "Số lượng hồng cầu"),
    (["huyết sắc tố", "hgb", "hb", "hemoglobin"], "mass", "718-7", "Hemoglobin [Mass/volume] in Blood", "Huyết sắc tố"),
    (["hematocrit", "hct"], "frac", "4544-3", "Hematocrit [Volume Fraction] of Blood by Automated count", "Hematocrit"),
    (["thể tích trung bình hồng cầu mcv", "mcv"], "other", "787-2", "MCV [Entitic volume] by Automated count", "Thể tích trung bình hồng cầu"),
    (["lượng hst trung bình hồng cầu mch", "mch"], "other", "785-6", "MCH [Entitic mass] by Automated count", "Lượng huyết sắc tố trung bình hồng cầu"),
    (["nồng độ hst trung bình hồng cầu mchc", "mchc"], "mass", "786-4", "MCHC [Mass/volume] by Automated count", "Nồng độ huyết sắc tố trung bình hồng cầu"),
    (["rdw-cv dải phân bố hc-hệ số biến thiên", "rdw-cv", "rdw"], "frac", "788-0", "Erythrocyte distribution width [Ratio] by Automated count", "RDW-CV"),
    (["rdw-sd dải phân bố hc-độ lệch chuẩn", "rdw-sd"], "other", "21000-5", "Erythrocyte distribution width [Entitic volume] by Automated count (deprecated, không có mã thay thế)", "RDW-SD"),
    (["nrbc sl hồng cầu có nhân", "nrbc#", "hồng cầu có nhân"], "count", "771-6", "Nucleated erythrocytes [#/volume] in Blood by Automated count", "Số lượng hồng cầu có nhân"),
    (["nrbc tỷ lệ hồng cầu có nhân", "nrbc%"], "frac", "58413-6", "Nucleated erythrocytes/Leukocytes [Ratio] in Blood by Automated count", "Tỷ lệ hồng cầu có nhân"),
    (["số lượng tiểu cầu", "plt", "tiểu cầu"], "count", "777-3", "Platelets [#/volume] in Blood by Automated count", "Số lượng tiểu cầu"),
    (["mpv thể tích trung bình tc", "thể tích trung bình tiểu cầu mpv", "mpv"], "other", "32623-1", "Platelet [Entitic mean volume] in Blood by Automated count", "Thể tích trung bình tiểu cầu"),
    (["dải phân bố tiểu cầu pdw", "pdw"], "other", "32207-3", "Platelet distribution width [Entitic volume] in Blood by Automated count", "Dải phân bố tiểu cầu"),
    (["thể tích khối tiểu cầu pct", "pct"], "frac", "51637-7", "Plateletcrit [Volume Fraction] in Blood", "Thể tích khối tiểu cầu"),
    (["các tế bào khác", "tb khác"], "count", "58443-3", "Other cells [#/volume] in Blood by Automated count", "Các tế bào khác"),
    # --- Hoá sinh máu: đơn vị mol/L như phiếu Việt Nam; dòng 'mass' dùng khi đơn vị là mg/dL
    (["glucose", "glucose máu", "đường máu"], "molar", "14749-6", "Glucose [Moles/volume] in Serum or Plasma", "Glucose máu"),
    (["glucose", "glucose máu"], "mass", "2345-7", "Glucose [Mass/volume] in Serum or Plasma", "Glucose máu"),
    (["creatinin", "creatinine", "creatinin máu"], "molar", "14682-9", "Creatinine [Moles/volume] in Serum or Plasma", "Creatinin máu"),
    (["creatinin", "creatinine"], "mass", "2160-0", "Creatinine [Mass/volume] in Serum or Plasma", "Creatinin máu"),
    (["ure", "urea", "ure máu"], "molar", "22664-7", "Urea [Moles/volume] in Serum or Plasma", "Ure máu"),
    (["protein toàn phần", "protein tp"], "mass", "2885-2", "Protein [Mass/volume] in Serum or Plasma", "Protein toàn phần"),
    (["albumin", "albumin máu"], "mass", "1751-7", "Albumin [Mass/volume] in Serum or Plasma", "Albumin máu"),
    (["cholesterol toàn phần", "cholesterol", "cholesterol tp"], "molar", "14647-2", "Cholesterol [Moles/volume] in Serum or Plasma", "Cholesterol toàn phần"),
    (["cholesterol toàn phần", "cholesterol"], "mass", "2093-3", "Cholesterol [Mass/volume] in Serum or Plasma", "Cholesterol toàn phần"),
    (["triglycerid", "triglyceride"], "molar", "14927-8", "Triglyceride [Moles/volume] in Serum or Plasma", "Triglycerid"),
    (["ldl c", "ldl-c", "ldl", "ldl cholesterol"], "molar", "22748-8", "Cholesterol in LDL [Moles/volume] in Serum or Plasma", "LDL-Cholesterol"),
    (["hdl c", "hdl-c", "hdl", "hdl cholesterol"], "molar", "14646-4", "Cholesterol in HDL [Moles/volume] in Serum or Plasma", "HDL-Cholesterol"),
    (["sgot ast", "ast got", "ast", "sgot"], "enz", "1920-8", "Aspartate aminotransferase [Enzymatic activity/volume] in Serum or Plasma", "AST (SGOT)"),
    (["sgpt alt", "alt gpt", "alt", "sgpt"], "enz", "1742-6", "Alanine aminotransferase [Enzymatic activity/volume] in Serum or Plasma", "ALT (SGPT)"),
    (["calci toàn phần", "canxi toàn phần", "calci", "canxi", "ca"], "molar", "2000-8", "Calcium [Moles/volume] in Serum or Plasma", "Calci toàn phần"),
    (["calci toàn phần", "canxi toàn phần", "calci"], "mass", "17861-6", "Calcium [Mass/volume] in Serum or Plasma", "Calci toàn phần"),
    (["na+", "na", "natri"], "molar", "2951-2", "Sodium [Moles/volume] in Serum or Plasma", "Natri máu"),
    (["k+", "k", "kali"], "molar", "2823-3", "Potassium [Moles/volume] in Serum or Plasma", "Kali máu"),
    (["cl-", "cl", "clo"], "molar", "2075-0", "Chloride [Moles/volume] in Serum or Plasma", "Clo máu"),
    (["acid uric", "uric"], "molar", "14933-6", "Urate [Moles/volume] in Serum or Plasma", "Acid uric"),
    (["crp"], "mass", "1988-5", "C reactive protein [Mass/volume] in Serum or Plasma", "CRP"),
    (["bilirubin toàn phần"], "molar", "14631-6", "Bilirubin.total [Moles/volume] in Serum or Plasma", "Bilirubin toàn phần"),
    # --- Nước tiểu (test strip). Tên trên phiếu trùng tên XN máu -> chỉ chọn khi panel là nước tiểu (lab_loinc_candidates xử lý)
    (["glucose niệu", "u:glucose"], "pres", "25428-4", "Glucose [Presence] in Urine by Test strip", "Glucose niệu"),
    (["bilirubin", "bilirubin niệu", "u:bilirubin"], "pres", "5770-3", "Bilirubin.total [Presence] in Urine by Test strip", "Bilirubin niệu"),
    (["thể cetonic", "ketone", "u:thể cetonic"], "pres", "2514-8", "Ketones [Presence] in Urine by Test strip", "Thể ceton niệu"),
    (["tỷ trọng", "u:tỷ trọng"], "other", "5811-5", "Specific gravity of Urine by Test strip", "Tỷ trọng nước tiểu"),
    (["hồng cầu niệu", "u:hồng cầu", "blood"], "pres", "5794-3", "Hemoglobin [Presence] in Urine by Test strip", "Hồng cầu niệu"),
    (["ph", "u:ph"], "other", "5803-2", "pH of Urine by Test strip", "pH nước tiểu"),
    (["protein niệu", "u:protein"], "pres", "20454-5", "Protein [Presence] in Urine by Test strip", "Protein niệu"),
    (["urobilinogen", "u:urobilinogen"], "molar", "34928-2", "Urobilinogen [Moles/volume] in Urine by Test strip", "Urobilinogen niệu"),
    (["urobilinogen", "u:urobilinogen"], "pres", "5818-0", "Urobilinogen [Presence] in Urine by Test strip", "Urobilinogen niệu"),
    (["nitrit", "nitrite", "u:nitrit"], "pres", "5802-4", "Nitrite [Presence] in Urine by Test strip", "Nitrit niệu"),
    (["bạch cầu niệu", "u:bạch cầu", "leukocyte"], "count", "20408-1", "Leukocytes [#/volume] in Urine by Test strip", "Bạch cầu niệu"),
    (["bạch cầu niệu", "u:bạch cầu"], "pres", "5799-2", "Leukocyte esterase [Presence] in Urine by Test strip", "Bạch cầu niệu (esterase)"),
    (["col", "màu sắc", "color", "u:col"], "other", "5778-6", "Color of Urine", "Màu sắc nước tiểu"),
    (["clarity độ đục", "clarity", "độ đục", "u:clarity độ đục"], "other", "32167-9", "Clarity of Urine", "Độ đục nước tiểu"),
    (["creatinin niệu", "u:creatinin niệu"], "molar", "30002-0", "Creatinine [Moles/volume] in Urine by Test strip", "Creatinin niệu (test strip)"),
    (["albumin niệu", "microalbumin", "u:albumin niệu"], "mass", "14957-5", "Microalbumin [Mass/volume] in Urine", "Albumin niệu"),
    (["a/c", "albumin/creatinin", "u:a/c"], "other", "20621-9", "Albumin/Creatinine [Mass Ratio] in Urine by Test strip", "Tỷ số Albumin/Creatinin niệu"),
]
UNIT_KIND = {"10*9/L": "count", "10*12/L": "count", "10*3/uL": "count", "/uL": "count", "%": "frac", "mmol/L": "molar", "umol/L": "molar", "g/L": "mass", "g/dL": "mass",
             "mg/L": "mass", "mg/dL": "mass", "U/L": "enz", "fL": "other", "pg": "other", "[pH]": "other", "mg/mmol": "other"}
LAB_PANELS = {
    "tổng phân tích tế bào máu": ("57021-8", "CBC W Auto Differential panel - Blood", "1000147"),
    "điện giải đồ": ("24326-1", "Electrolytes 1998 panel - Serum or Plasma", "1010079"),
    "tổng phân tích nước tiểu": ("50556-0", "Urinalysis dipstick panel - Urine by Automated test strip", "1010290"),
    "xn nước tiểu": ("50556-0", "Urinalysis dipstick panel - Urine by Automated test strip", "1010290"),
}
URINE_HINT = re.compile(r"nước tiểu|niệu|urine", re.I)


def lab_loinc_candidates(name: str, unit_raw: str | None = None, panel: str | None = None) -> list[dict]:
    """Ứng viên LOINC curated cho một chỉ số XN, xếp hạng theo tên + loại đơn vị + loại phiếu (máu/nước tiểu).
    Trả [] nếu không có -> phải tra cls_search / hỏi người dùng, KHÔNG tự bịa mã."""
    n = norm(name).replace("*", "").strip()
    uc = ucum(unit_raw)[0]
    kind = UNIT_KIND.get(uc) if uc else None
    is_urine = bool(panel and URINE_HINT.search(panel))
    out = []
    for aliases, ukind, code, disp, vi in LAB_LOINC:
        urine_row = any(a.startswith("u:") for a in aliases) or "Urine" in disp
        score = 0.0
        for a in aliases:
            a2 = a[2:] if a.startswith("u:") else a
            if n == a2:
                score = max(score, 1.0)
            elif n.startswith(a2 + " ") or a2.startswith(n + " ") and len(n) > 3:
                score = max(score, 0.8)
        if score == 0:
            continue
        if is_urine != urine_row:
            score -= 0.5 if is_urine else 0.6
        if kind and ukind != "other":
            score += 0.15 if kind == ukind else -0.3
        elif ukind == "pres" and (unit_raw is None or not uc):
            score += 0.05
        out.append({"code": code, "display": disp, "display_vi": vi, "unit_kind": ukind, "score": round(score, 2), "source": "curated", "verified": True})
    out.sort(key=lambda x: -x["score"])
    return out


VITALS = {  # key: (loinc, display EN, display VI, ucum code, nhãn đơn vị (fixedString của profile VN Core — không được dịch), profile VN Core)
    "mach": ("8867-4", "Heart rate", "Mạch", "/min", "/min", "vn-core-observation-heart-rate"),
    "nhiet_do": ("8310-5", "Body temperature", "Nhiệt độ cơ thể", "Cel", "°C", "vn-core-observation-body-temperature"),
    "huyet_ap": ("85354-9", "Blood pressure panel with all children optional", "Huyết áp", "mm[Hg]", "mmHg", "vn-core-observation-blood-pressure"),
    "nhip_tho": ("9279-1", "Respiratory rate", "Nhịp thở", "/min", "/min", "vn-core-observation-respiratory-rate"),
    "spo2": ("2708-6", "Oxygen saturation in Arterial blood", "SpO2", "%", "%", "vn-core-observation-spo2"),
    "can_nang": ("29463-7", "Body weight", "Cân nặng", "kg", "kg", "vn-core-observation-body-weight"),
    "chieu_cao": ("8302-2", "Body height", "Chiều cao", "cm", "cm", "vn-core-observation-body-height"),
    "bmi": ("39156-5", "Body mass index (BMI) [Ratio]", "BMI", "kg/m2", "kg/m2", "vn-core-observation-vital-signs"),
}
VITAL_BY_LOINC = {v[0]: k for k, v in VITALS.items()}
VITAL_ALIASES = {"mạch": "mach", "nhiệt độ": "nhiet_do", "huyết áp": "huyet_ap", "nhịp thở": "nhip_tho", "spo2": "spo2", "cân nặng": "can_nang", "chiều cao": "chieu_cao", "bmi": "bmi"}

IMAGING_LOINC = [  # (aliases đã norm, loinc, display, mã CLS QĐ 1227 nếu có, category vn-service-request-category-cs)
    (["cộng hưởng từ cột sống cổ", "mri cột sống cổ", "cht cột sống cổ"], "30667-0", "MR Cervical spine WO contrast", "1070466", "CDHA"),
    (["cộng hưởng từ sọ não", "mri sọ não", "cộng hưởng từ não"], "30657-1", "MR Brain WO contrast", "1070426", "CDHA"),
    (["doppler động mạch cảnh", "doppler mạch cảnh", "siêu âm doppler động mạch cảnh"], "43765-7", "US.doppler Carotid arteries - bilateral", "1071138", "CDHA"),
    (["doppler xuyên sọ", "siêu âm doppler xuyên sọ", "siêu âm doppler mạch máu não"], "24733-8", "US.doppler Head vessels", None, "CDHA"),
    (["điện não đồ", "ghi điện não đồ thông thường", "điện não"], "11523-8", "EEG study", None, "TDCN"),
    (["x quang ngực thẳng", "xq ngực thẳng", "chụp x-quang ngực thẳng"], "36643-5", "XR Chest 2 Views", None, "CDHA"),
    (["siêu âm ổ bụng", "siêu âm bụng tổng quát"], "24558-9", "US Abdomen", None, "CDHA"),
    (["điện tim", "điện tâm đồ", "ecg"], "11524-6", "EKG study", None, "TDCN"),
]


def imaging_candidates(name: str) -> list[dict]:
    n = norm(name)
    out = []
    for aliases, code, disp, cls, cat in IMAGING_LOINC:
        for a in aliases:
            if a in n:
                out.append({"code": code, "display": disp, "cls": cls, "category": cat, "score": round(len(a) / max(len(n), 1), 2), "source": "curated", "verified": True})
                break
    out.sort(key=lambda x: -x["score"])
    return out


DOC_TYPES = {  # loại phiếu -> LOINC document type (đã kiểm tra active; 18755-9/18760-9 là mã deprecated, KHÔNG dùng)
    "phieu-kham-benh-vao-vien": ("34117-2", "History and physical note"), "ket-qua-xet-nghiem-huyet-hoc": ("11502-2", "Laboratory report"),
    "ket-qua-xet-nghiem-sinh-hoa-mau": ("11502-2", "Laboratory report"), "phieu-xet-nghiem-nuoc-dich": ("11502-2", "Laboratory report"),
    "phieu-chup-cong-huong-tu": ("18748-4", "Diagnostic imaging study"), "phieu-sieu-am-doppler": ("18748-4", "Diagnostic imaging study"),
    "phieu-dien-nao-do": ("11523-8", "EEG study"), "phieu-cham-soc": ("34746-8", "Nurse Note"), "trich-bien-ban-hoi-chan": ("11488-4", "Consult note"),
    "phieu-dieu-tri": ("11506-3", "Progress note"), "ban-tom-tat-ho-so-benh-an": ("18842-5", "Discharge summary"), "phieu-cong-khai-dich-vu": ("51851-4", "Administrative note"),
    "bang-ke-chi-phi-kcb": ("51851-4", "Administrative note"), "giay-vao-vien": ("51851-4", "Administrative note"), "phieu-cls": ("11502-2", "Laboratory report"),
    "phieu-danh-gia-nguy-co-te-nga": ("34746-8", "Nurse Note"), "phieu-sang-loc-dinh-duong": ("34746-8", "Nurse Note"), "default": ("34109-9", "Note"),
}
SECTIONS = {  # mục bệnh án -> LOINC section code (khớp slice của vn-core-composition khi có)
    "chief_complaint": ("10154-3", "Chief complaint Narrative - Reported"), "history": ("11348-0", "History of Past illness note"),
    "physical_exam": ("29545-1", "Physical findings note"), "diagnosis": ("29548-5", "Diagnosis Narrative"),
    "lab_results": ("30954-2", "Relevant diagnostic tests/laboratory data note"), "imaging": ("18748-4", "Diagnostic imaging study"),
    "hospital_course": ("8648-8", "Hospital course note"), "medications": ("10160-0", "History of Medication use Narrative"),
    "medications_administered": ("29549-3", "Medication administered Narrative"), "procedures": ("47519-4", "History of Procedures Document"),
    "allergies": ("48765-2", "Allergies and adverse reactions Document"), "vital_signs": ("8716-3", "Vital signs note"),
    "plan_of_care": ("18776-5", "Plan of Care Note"), "discharge_summary": ("18842-5", "Discharge summary"),
    "discharge_disposition": ("8650-4", "Hospital discharge disposition note"), "admission_diagnosis": ("46241-6", "Hospital admission diagnosis Narrative - Reported"),
    "hpi": ("10164-2", "History of Present illness Narrative"), "administrative": ("51851-4", "Administrative note"), "problems": ("11450-4", "Problem list - Reported"),
}


def loinc_online(code: str, timeout: int = 20) -> dict | None:
    """Tra LOINC qua NLM Clinical Tables (cần mạng). -> {code, display, property, status} hoặc None nếu không tồn tại / lỗi mạng (trả {'error':...})."""
    import urllib.parse
    import urllib.request
    url = "https://clinicaltables.nlm.nih.gov/api/loinc_items/v3/search?" + urllib.parse.urlencode(
        {"terms": code, "sf": "LOINC_NUM", "df": "LOINC_NUM,LONG_COMMON_NAME,PROPERTY,STATUS", "maxList": 5})
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            rows = json.load(r)[3]
    except Exception as e:  # noqa
        return {"error": str(e)}
    for row in rows:
        if row[0] == code:
            return {"code": row[0], "display": row[1], "property": row[2], "status": row[3], "deprecated": row[1].lower().startswith("deprecated")}
    return None


# ------------------------------------------------------------------ ngày giờ Việt Nam
_DMY = r"(?P<d>\d{1,2})\s*[/\-\.]\s*(?P<m>\d{1,2})\s*[/\-\.]\s*(?P<y>\d{2,4})"
PATTERNS = [
    re.compile(r"(?P<H>\d{1,2})\s*giờ\s*(?P<M>\d{1,2})\s*phút\s*,?\s*ngày\s*(?P<d>\d{1,2})\s*tháng\s*(?P<m>\d{1,2})\s*năm\s*(?P<y>\d{4})", re.I),
    re.compile(r"ngày\s*(?P<d>\d{1,2})\s*tháng\s*(?P<m>\d{1,2})\s*năm\s*(?P<y>\d{4})", re.I),
    re.compile(r"(?P<H>\d{1,2})\s*[h:]\s*(?P<M>\d{2})\s+" + _DMY),
    re.compile(_DMY + r"\s+(?P<H>\d{1,2})\s*[h:]\s*(?P<M>\d{2})"),
    re.compile(r"(?P<H>\d{1,2})\s*giờ\s*(?P<M>\d{1,2})\s*phút\s*,?\s*ngày\s*" + _DMY, re.I),
    re.compile(_DMY),
]


def vn_datetime(text: str | None, tz: str = "+07:00") -> str | None:
    """'08 giờ 24 phút ngày 02 tháng 02 năm 2026' -> '2026-02-02T08:24:00+07:00'; chỉ ngày -> '2026-02-02'; không nhận ra -> None."""
    if not text:
        return None
    t = nfc(str(text))
    for pat in PATTERNS:
        m = pat.search(t)
        if not m:
            continue
        g = m.groupdict()
        y = int(g["y"]); y = y + 2000 if y < 100 else y
        d, mo = int(g["d"]), int(g["m"])
        if not (1 <= d <= 31 and 1 <= mo <= 12):
            continue
        if g.get("H") is not None:
            return f"{y:04d}-{mo:02d}-{d:02d}T{int(g['H']):02d}:{int(g['M']):02d}:00{tz}"
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def to_number(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return s
    t = str(s).strip().replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", t)
    if not m or m.group(0) != t:
        return None
    v = float(m.group(0))
    return int(v) if v.is_integer() and "." not in t else v


def parse_ref_range(txt: str | None, sex: str | None = None):
    """'( 3.4 - 9.7 )' -> (3.4, 9.7). Khoảng theo giới 'Nam 60 -120 / Nữ 40 -90' -> chọn theo sex ('Nam'|'Nữ'), None nếu không có sex."""
    if not txt:
        return None
    t = nfc(txt).replace("–", "-")
    if re.search(r"\bN[aữ]", t):
        if not sex:
            return None
        lines = [ln for ln in re.split(r"[\n;]", t) if ln.strip()]
        for ln in lines:
            if (sex == "Nữ" and re.search(r"Nữ", ln)) or (sex == "Nam" and re.search(r"Nam", ln)):
                m = re.search(r"(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)", ln)
                if m:
                    return float(m.group(1)), float(m.group(2))
        return None
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)", t)
    return (float(m.group(1)), float(m.group(2))) if m else None


# ------------------------------------------------------------------ họ tên, học hàm/học vị
def split_name(full: str) -> tuple[str, list[str]]:
    parts = [p for p in nfc(full or "").strip().split() if p]
    return (parts[0], parts[1:]) if parts else ("", [])


QUAL = [  # regex trên nhãn in -> mã vn-qualification-cs
    (r"PGS", "PGS"), (r"\bGS\b", "GS"), (r"\bTS\b", "TS"), (r"ThS|Th\.S|Thạc s", "ThS"),
    (r"BS\.?\s?CKII|BSCKII|CKII|Chuyên khoa II", "BS-CKII"), (r"BS\.?\s?CKI\b|BSCKI\b|CKI\b|Chuyên khoa I\b", "BS-CKI"),
    (r"\bBS\b|\bBs\b|Bác s[ĩỹ]|BSNT", "BS"), (r"KTV", "KTV"), (r"\bĐD\b|ĐD[\.\-]|Điều dưỡng|CNĐD", "DD"), (r"\bDS\b", "DS"), (r"\bCN\b", "CN"), (r"\bYS\b", "YS"), (r"\bHS\b", "HS"),
]
_PREFIX = r"^(PGS|GS|TS|ThS|Th\.S|BS\.?\s?CKII|BS\.?\s?CKI|BSCKII|BSCKI|BSNT|BS|Bs|KTV|ĐD|CNĐD|DS|CN|YS|HS|KHH)[\.\s\-]*"


def parse_person(label: str) -> dict:
    """'ThS.Bs Vương Quang Uyển' -> {raw, name, titles:['ThS','BS'], regulated:'bac-sy'|'dieu-duong'|'ky-thuat-y'|None}."""
    raw = nfc(label or "").strip()
    titles = [code for pat, code in QUAL if re.search(pat, raw)]
    name = raw
    for _ in range(3):
        name = re.sub(_PREFIX, "", name).strip()
    name = " ".join(w.capitalize() if w.isupper() else w for w in name.split())
    reg = None
    if any(c.startswith("BS") or c in ("ThS", "TS", "PGS", "GS") for c in titles):
        reg = "bac-sy"
    if "DD" in titles:
        reg = "dieu-duong"
    if "KTV" in titles:
        reg = "ky-thuat-y"
    return {"raw": raw, "name": name, "titles": titles, "regulated": reg}


if __name__ == "__main__":  # tự kiểm tra nhanh
    assert loinc_check_digit("6690-2") and loinc_check_digit("718-7") and loinc_check_digit("85354-9") and not loinc_check_digit("32623-3")
    assert vn_datetime("08 giờ 24 phút ngày 02 tháng 02 năm 2026") == "2026-02-02T08:24:00+07:00"
    assert vn_datetime("10h17 02/02/2026") == "2026-02-02T10:17:00+07:00" and vn_datetime("02 / 02 / 26 08 : 34") == "2026-02-02T08:34:00+07:00"
    assert parse_person("ThS.Bs Vương Quang Uyển")["name"] == "Vương Quang Uyển" and parse_person("ĐD- Vũ Văn Thành")["titles"] == ["DD"]
    assert lab_loinc_candidates("Trung tính [G/L]", "G/L")[0]["code"] == "751-8" and lab_loinc_candidates("Trung tính [%]", "%")[0]["code"] == "770-8"
    assert lab_loinc_candidates("Glucose", "mmol/L", "XN NƯỚC TIỂU")[0]["code"] == "25428-4" and lab_loinc_candidates("Glucose*", "mmol/L", "XÉT NGHIỆM")[0]["code"] == "14749-6"
    assert lab_loinc_candidates("Calci (toàn phần)", "mmol/l")[0]["code"] == "2000-8"
    print("vnlib OK; vendor =", find_vendor())


# ------------------------------------------------------------------ RxNorm (RxNav, có cache)
RX_ALIASES = {  # tên hoạt chất VN/BP (đã norm) -> tên dùng trong RxNorm
    "paracetamol": "acetaminophen", "natri clorid": "sodium chloride", "nacl": "sodium chloride", "kali clorid": "potassium chloride",
    "kcl": "potassium chloride", "adrenalin": "epinephrine", "noradrenalin": "norepinephrine",
    "vitamin b1": "thiamine", "vitamin b6": "pyridoxine", "vitamin b12": "cyanocobalamin",
    "vitamin c": "ascorbic acid", "vitamin k1": "phytonadione", "vitamin k": "phytonadione", "methylprednisolon": "methylprednisolone",
    "furosemid": "furosemide", "spironolacton": "spironolactone", "metoclopramid": "metoclopramide", "gliclazid": "gliclazide",
    "omeprazol": "omeprazole", "esomeprazol": "esomeprazole", "pantoprazol": "pantoprazole", "rabeprazol": "rabeprazole",
    "lansoprazol": "lansoprazole", "cefoperazon": "cefoperazone", "ceftriaxon": "ceftriaxone", "cefotaxim": "cefotaxime",
    "ceftazidim": "ceftazidime", "cefuroxim": "cefuroxime", "cefalexin": "cephalexin", "cefamandol": "cefamandole",
    "amoxicilin": "amoxicillin", "ampicilin": "ampicillin", "metronidazol": "metronidazole", "acid tranexamic": "tranexamic acid",
    "magnesi sulfat": "magnesium sulfate", "calci clorid": "calcium chloride", "calci gluconat": "calcium gluconate",
    "natri bicarbonat": "sodium bicarbonate", "drotaverin": "drotaverine", "alverin": "alverine", "aescin": "escin",
    "levothyroxin": "levothyroxine", "acetylcystein": "acetylcysteine", "bromhexin": "bromhexine", "acid acetylsalicylic": "aspirin",
    "nifedipin": "nifedipine", "amlodipin": "amlodipine", "dexamethason": "dexamethasone", "hydrocortison": "hydrocortisone",
    "prednisolon": "prednisolone", "morphin": "morphine", "lidocain": "lidocaine", "nicardipin": "nicardipine",
    "dobutamin": "dobutamine", "dopamin": "dopamine", "povidon iod": "povidone-iodine", "citicolin": "citicoline",
    "kali": "potassium chloride", "natri": "sodium chloride",
}


def _rx_cache_file() -> Path:
    return vendor() / "rxnorm" / "rxnav_cache.json"


@lru_cache(maxsize=1)
def _rx_cache() -> dict:
    f = _rx_cache_file()
    return read_json(f) if f.exists() else {}


def _rx_get(path: str, timeout: int = 20):
    import urllib.request
    with urllib.request.urlopen("https://rxnav.nlm.nih.gov/REST/" + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def rxnorm_ingredient(name: str, online: bool = True) -> dict | None:
    """Tên hoạt chất (VN/INN) -> {rxcui, name, tty} mức ingredient (IN). Chỉ tra cứu RxNav, không đoán.
    Kết quả (kể cả None = không thấy) được cache ở vendor/rxnorm/rxnav_cache.json. Chỉ gửi TÊN HOẠT CHẤT ra ngoài, không PII."""
    import urllib.parse
    q = norm(name)
    if not q:
        return None
    q = RX_ALIASES.get(q, q)
    cache = _rx_cache()
    if q in cache:
        return cache[q]
    if not online:
        return None
    res = None
    try:
        ids = []
        for mode in (0, 2):  # 0 = chính xác, 2 = chuẩn hoá
            ids = (_rx_get(f"rxcui.json?name={urllib.parse.quote(q)}&search={mode}").get("idGroup") or {}).get("rxnormId") or []
            if ids:
                break
        if ids:
            prop = _rx_get(f"rxcui/{ids[0]}/properties.json").get("properties") or {}
            if prop.get("tty") == "IN":
                res = {"rxcui": prop["rxcui"], "name": prop["name"], "tty": "IN"}
            else:
                groups = (_rx_get(f"rxcui/{ids[0]}/related.json?tty=IN").get("relatedGroup") or {}).get("conceptGroup") or []
                ins = [c for g in groups if g.get("tty") == "IN" for c in g.get("conceptProperties") or []]
                if len(ins) >= 1:
                    res = {"rxcui": ins[0]["rxcui"], "name": ins[0]["name"], "tty": "IN", "from": prop.get("tty")}
        if not res:  # approximateTerm: chỉ nhận ingredient có tên rất gần
            cands = (_rx_get(f"approximateTerm.json?term={urllib.parse.quote(q)}&maxEntries=5").get("approximateGroup") or {}).get("candidate") or []
            for c in cands:
                prop = _rx_get(f"rxcui/{c['rxcui']}/properties.json").get("properties") or {}
                if prop.get("tty") == "IN" and SequenceMatcher(None, q, prop["name"].lower()).ratio() >= 0.85:
                    res = {"rxcui": prop["rxcui"], "name": prop["name"], "tty": "IN", "approx": True}
                    break
    except Exception as e:  # lỗi mạng -> không cache, lần sau thử lại
        return {"error": str(e)}
    cache[q] = res
    write_json(cache, _rx_cache_file())
    return res
