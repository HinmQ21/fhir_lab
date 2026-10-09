"""Thuật ngữ & chuẩn hoá dùng cho bước 2/3.

- CLS (QĐ 1227) -> LOINC qua ConceptMap vn-cm-cls-to-loinc (vendor/vncore/terminology)
- ICD-10 VN (QĐ 4469) tra tên bệnh: vendor/icd10/icd10_vn.json
- Bảng LOINC sinh hiệu VN Core (vn-vital-signs-codes-vs) + đơn vị UCUM cố định của profile
- Bảng UCUM cho đơn vị in trên phiếu XN Việt Nam
- Bảng LOINC "curated" cho các chỉ số XN hay gặp (ưu tiên đơn vị mol/L như phiếu VN), chọn theo tên + loại đơn vị + loại phiếu
  (nội dung bảng sao từ vn-fhir/skills/vn-fhir-convert/scripts/vnlib.py — đã đối chiếu NLM 2026-10-08; module này đứng độc lập, không import vnlib)
- Kiểm tra chữ số kiểm tra LOINC: loinc_check_digit(code)
- Parse ngày giờ kiểu Việt Nam -> ISO 8601 (+07:00)
"""
from __future__ import annotations

import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

VENDOR = Path(__file__).resolve().parents[1] / "vendor"  # đổi bằng configure(vendor_dir)


def configure(vendor_dir):
    """Trỏ tới thư mục vendor (chứa vncore/ và icd10/) và xoá cache đã nạp."""
    global VENDOR, _ICD, _CLS
    VENDOR = Path(vendor_dir)
    _ICD = None
    _CLS = None
VN = "http://fhir.hl7.org.vn/core"
SYS = {
    "loinc": "http://loinc.org",
    "ucum": "http://unitsofmeasure.org",
    "icd10vn": f"{VN}/CodeSystem/vn-icd10-cs",
    "icd10who": "http://hl7.org/fhir/sid/icd-10",
    "cls": f"{VN}/CodeSystem/vn-cls-cs",
    "atc": "http://www.whocc.no/atc",
    "snomed": "http://snomed.info/sct",
    "rxnorm": "http://www.nlm.nih.gov/research/umls/rxnorm",
}


def nfc(s):
    return unicodedata.normalize("NFC", s) if isinstance(s, str) else s


def norm(s: str) -> str:
    s = nfc(s or "").lower()
    s = re.sub(r"[\*\(\)\[\]\{\}:;,\.]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ------------------------------------------------------------------ ICD-10 VN
_ICD = None


def icd10_lookup(code: str) -> dict | None:
    global _ICD
    if _ICD is None:
        _ICD = json.loads((VENDOR / "icd10/icd10_vn.json").read_text(encoding="utf-8"))
        # vài mã 3 ký tự thiếu trong file nguồn
        _ICD.setdefault("R51", {"vi": "Đau đầu", "en": "Headache", "chapter": "Các triệu chứng, dấu hiệu"})
        _ICD.setdefault("R42", {"vi": "Chóng mặt và choáng váng", "en": "Dizziness and giddiness", "chapter": "Các triệu chứng, dấu hiệu"})
    if not code:
        return None
    c = code.strip().upper()
    return _ICD.get(c) or _ICD.get(c.replace(".", ""))


ICD_RE = re.compile(r"\b([A-TV-Z][0-9]{2}(?:\.[0-9]{1,2})?)\b")


# ------------------------------------------------------------------ CLS -> LOINC
_CLS = None


def _load_cls():
    global _CLS
    if _CLS is None:
        cs = json.loads((VENDOR / "vncore/terminology/CodeSystem-vn-cls-cs.json").read_text(encoding="utf-8"))
        cm = json.loads((VENDOR / "vncore/terminology/ConceptMap-vn-cm-cls-to-loinc.json").read_text(encoding="utf-8"))
        targets = {}
        for g in cm["group"]:
            for e in g["element"]:
                targets[e["code"]] = [(t["code"], t.get("display"), t.get("equivalence")) for t in e.get("target", [])]
        rows = []
        for c in cs["concept"]:
            props = {p["code"]: (p.get("valueString") or p.get("valueCode")) for p in c.get("property", [])}
            rows.append({"code": c["code"], "display": c.get("display", ""), "norm": norm(c.get("display", "")),
                         "appendix": props.get("appendix"), "ucum": props.get("ucum-unit"),
                         "loinc": targets.get(c["code"], [])})
        _CLS = rows
    return _CLS


def cls_search(name: str, appendix: str | None = None, min_ratio: float = 0.82) -> dict | None:
    """Tìm dịch vụ CLS gần nhất theo tên (QĐ 1227). appendix: '01' XN huyết học, '02' hoá sinh, '03' vi sinh..., '05' CĐHA."""
    q = norm(name)
    best, best_r = None, 0.0
    for r in _load_cls():
        if appendix and r["appendix"] != appendix:
            continue
        ratio = SequenceMatcher(None, q, r["norm"]).ratio()
        if q and (q in r["norm"] or r["norm"] in q):
            ratio = max(ratio, 0.9)
        if ratio > best_r:
            best, best_r = r, ratio
    if best and best_r >= min_ratio:
        return {**best, "match_ratio": round(best_r, 3)}
    return None


# ------------------------------------------------------------------ UCUM (bảng như vnlib; khoá viết thường trừ G/L, T/L phân biệt hoa/thường)
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


# ------------------------------------------------------------------ LOINC: chữ số kiểm tra
def loinc_check_digit(code: str) -> bool:
    """Kiểm tra chữ số kiểm tra LOINC (mod 10, 'Luhn-like'). '6690-2' -> True; '32623-3' -> False."""
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


# ------------------------------------------------------------------ Sinh hiệu (VN Core vn-vital-signs-codes-vs)
VITALS = {
    # key clinical : (loinc, display EN, display VI, ucum code, unit label, profile slug)
    "mach": ("8867-4", "Heart rate", "Mạch", "/min", "/min", "vn-core-observation-heart-rate"),
    "nhiet_do": ("8310-5", "Body temperature", "Nhiệt độ cơ thể", "Cel", "°C", "vn-core-observation-body-temperature"),
    "huyet_ap": ("85354-9", "Blood pressure panel with all children optional", "Huyết áp", "mm[Hg]", "mmHg", "vn-core-observation-blood-pressure"),
    "nhip_tho": ("9279-1", "Respiratory rate", "Nhịp thở", "/min", "/min", "vn-core-observation-respiratory-rate"),
    "spo2": ("2708-6", "Oxygen saturation in Arterial blood", "SpO2", "%", "%", "vn-core-observation-spo2"),
    "can_nang": ("29463-7", "Body weight", "Cân nặng", "kg", "kg", "vn-core-observation-body-weight"),
    "chieu_cao": ("8302-2", "Body height", "Chiều cao", "cm", "cm", "vn-core-observation-body-height"),
    "bmi": ("39156-5", "Body mass index (BMI) [Ratio]", "BMI", "kg/m2", "kg/m2", "vn-core-observation-vital-signs"),
}
VITAL_ALIASES = {
    "mạch": "mach", "nhiệt độ": "nhiet_do", "huyết áp": "huyet_ap", "nhịp thở": "nhip_tho", "spo2": "spo2",
    "cân nặng": "can_nang", "chiều cao": "chieu_cao", "bmi": "bmi",
}

# ------------------------------------------------------------------ LOINC curated cho XN phổ biến ở VN (sao từ vnlib.LAB_LOINC, đã đối chiếu NLM 2026-10-08)
# Mỗi dòng: aliases, unit_kind ('count','frac','molar','mass','enz','pres','other'), loinc, display EN, display VI.
# Alias được norm() lại lúc nạp (xem _LAB_ROWS) nên có thể viết "trung tính [%]" hay "trung tính %" đều khớp.
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
# (loinc, display EN, display VI, mã CLS QĐ 1227) — giữ bộ 4 như trước để normalize.py dùng panel[2]/panel[3]
LAB_PANELS = {
    "tổng phân tích tế bào máu": ("57021-8", "CBC W Auto Differential panel - Blood", "Tổng phân tích tế bào máu ngoại vi", "1000147"),
    "xn hóa sinh": ("24323-8", "Comprehensive metabolic 2000 panel - Serum or Plasma", "Xét nghiệm hoá sinh máu", None),
    "điện giải đồ": ("24326-1", "Electrolytes 1998 panel - Serum or Plasma", "Điện giải đồ (Na, K, Cl)", "1010079"),
    "tổng phân tích nước tiểu": ("50556-0", "Urinalysis dipstick panel - Urine by Automated test strip", "Tổng phân tích nước tiểu", "1010290"),
    "xn nước tiểu": ("50556-0", "Urinalysis dipstick panel - Urine by Automated test strip", "Tổng phân tích nước tiểu", "1010290"),
}
URINE_HINT = re.compile(r"nước tiểu|niệu|urine", re.I)


def _lab_rows():
    """Bảng LAB_LOINC với alias đã norm() (cùng phép chuẩn hoá với tên tra cứu) + cờ dòng nước tiểu. Lazy, cache."""
    global _LAB_ROWS
    if _LAB_ROWS is None:
        rows = []
        for aliases, ukind, code, disp, vi in LAB_LOINC:
            urine_row = any(a.startswith("u:") for a in aliases) or "Urine" in disp
            als = []
            for a in aliases:
                a2 = norm(a[2:] if a.startswith("u:") else a)
                if a2 and a2 not in als:
                    als.append(a2)
            rows.append((als, ukind, code, disp, vi, urine_row))
        _LAB_ROWS = rows
    return _LAB_ROWS


_LAB_ROWS = None


def lab_loinc_candidates(name: str, unit_raw: str | None = None, panel: str | None = None) -> list[dict]:
    """Ứng viên LOINC curated cho một chỉ số XN, xếp hạng theo tên + loại đơn vị + loại phiếu (máu/nước tiểu).
    Ứng viên cùng loại bệnh phẩm với phiếu luôn đứng trước ứng viên khác loại. Trả [] nếu không có."""
    n = norm(name)
    uc = ucum(unit_raw)[0]
    kind = UNIT_KIND.get(uc) if uc else None
    is_urine = bool(panel and URINE_HINT.search(panel))
    out = []
    for aliases, ukind, code, disp, vi, urine_row in _lab_rows():
        score = 0.0
        for a2 in aliases:
            if n == a2:
                score = max(score, 1.0)
            elif n.startswith(a2 + " ") or (a2.startswith(n + " ") and len(n) > 3):
                score = max(score, 0.8)
        if score == 0:
            continue
        specimen_ok = is_urine == urine_row
        if not specimen_ok:
            score -= 0.5 if is_urine else 0.6
        if kind and ukind != "other":
            score += 0.15 if kind == ukind else -0.3
        elif ukind == "pres" and (unit_raw is None or not uc):
            score += 0.05
        out.append({"code": code, "display": disp, "display_vi": vi, "unit_kind": ukind, "specimen_match": specimen_ok,
                    "score": round(score, 2), "source": "curated", "verified": True})
    out.sort(key=lambda x: (not x["specimen_match"], -x["score"]))
    return out


def lab_loinc(name: str, panel: str | None = None, unit_raw: str | None = None):
    """-> (loinc, display_en, display_vi, source) hoặc None. unit_raw (vd 'mmol/L', 'G/L', '%') giúp chọn Moles/Mass/#/%."""
    cands = lab_loinc_candidates(name, unit_raw, panel)
    if not cands:
        return None
    best = cands[0]
    src = "curated" if best["score"] >= 1.0 else "curated-prefix"
    if not best["specimen_match"]:
        src += "-specimen?"
    return (best["code"], best["display"], best["display_vi"], src)


# ------------------------------------------------------------------ CĐHA / TDCN (sao từ vnlib.IMAGING_LOINC)
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
            a2 = norm(a)
            if a2 in n:
                out.append({"code": code, "display": disp, "cls": cls, "category": cat, "score": round(len(a2) / max(len(n), 1), 2), "source": "curated", "verified": True})
                break
    out.sort(key=lambda x: -x["score"])
    return out


def imaging_loinc(name: str):
    """-> (loinc, display, mã CLS|None, source) hoặc None."""
    cands = imaging_candidates(name)
    if cands:
        c = cands[0]
        return (c["code"], c["display"], c["cls"], "curated")
    hit = cls_search(name, appendix="05", min_ratio=0.8)
    if hit and hit["loinc"]:
        return (hit["loinc"][0][0], hit["loinc"][0][1], hit["code"], "cls-map")
    return None


# ------------------------------------------------------------------ ngày giờ Việt Nam
_MONTHS = r"(?P<d>\d{1,2})\s*[/\-\.]\s*(?P<m>\d{1,2})\s*[/\-\.]\s*(?P<y>\d{2,4})"
PATTERNS = [
    # 08 giờ 24 phút ngày 02 tháng 02 năm 2026  |  08 giờ 30 phút  ngày 02 tháng 02 năm 2026
    re.compile(r"(?P<H>\d{1,2})\s*giờ\s*(?P<M>\d{1,2})\s*phút\s*,?\s*ngày\s*(?P<d>\d{1,2})\s*tháng\s*(?P<m>\d{1,2})\s*năm\s*(?P<y>\d{4})", re.I),
    # ngày 02 tháng 02 năm 2026
    re.compile(r"ngày\s*(?P<d>\d{1,2})\s*tháng\s*(?P<m>\d{1,2})\s*năm\s*(?P<y>\d{4})", re.I),
    # 10h17 02/02/2026 | 08:31 02/02/2026 | 15h23 02/02/2026
    re.compile(r"(?P<H>\d{1,2})\s*[h:]\s*(?P<M>\d{2})\s+" + _MONTHS),
    # 02/02/2026 08:34 | 02 / 02 / 26 08 : 34
    re.compile(_MONTHS + r"\s+(?P<H>\d{1,2})\s*[h:]\s*(?P<M>\d{2})"),
    # 08 giờ 24 phút, ngày 02 / 02 / 2026 (bảng kê)
    re.compile(r"(?P<H>\d{1,2})\s*giờ\s*(?P<M>\d{1,2})\s*phút\s*,?\s*ngày\s*" + _MONTHS, re.I),
    # 02/02/2026
    re.compile(_MONTHS),
]


def vn_datetime(text: str | None, default_year: int | None = None) -> str | None:
    """'08 giờ 24 phút ngày 02 tháng 02 năm 2026' -> '2026-02-02T08:24:00+07:00'; chỉ ngày -> '2026-02-02'."""
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
            return f"{y:04d}-{mo:02d}-{d:02d}T{int(g['H']):02d}:{int(g['M']):02d}:00+07:00"
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def vn_date_only(text: str | None) -> str | None:
    iso = vn_datetime(text)
    return iso[:10] if iso else None


def to_number(s) -> float | None:
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


def parse_ref_range(txt: str | None):
    """'( 3.4 - 9.7 )' -> (3.4, 9.7); 'Nam 60 -120 \nNữ 40 -90' -> None (giới tính-phụ thuộc, giữ text)."""
    if not txt:
        return None
    t = txt.replace("–", "-")
    if re.search(r"\bN[aữ]", t):
        return None
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)", t)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None


def split_name(full: str) -> tuple[str, list[str]]:
    parts = [p for p in nfc(full).strip().split() if p]
    if not parts:
        return "", []
    return parts[0], parts[1:]


# học hàm/học vị viết tắt trên phiếu -> vn-qualification-cs
QUAL = [
    (r"PGS", "PGS", "Phó Giáo sư"), (r"\bGS\b", "GS", "Giáo sư"), (r"\bTS\b", "TS", "Tiến sĩ"), (r"ThS|Th\.S|Thạc s", "ThS", "Thạc sĩ"),
    (r"BS\.?CKII|BSCKII|CKII|Chuyên khoa II", "BS-CKII", "Bác sĩ Chuyên khoa II"), (r"BS\.?CKI\b|BSCKI\b|CKI\b", "BS-CKI", "Bác sĩ Chuyên khoa I"),
    (r"\bBS\b|Bs\b|Bác s[ĩỹ]|BSNT", "BS", "Bác sĩ"), (r"KTV", "KTV", "Kỹ thuật viên y"), (r"\bĐD\b|Điều dưỡng|CNĐD", "DD", "Điều dưỡng"),
    (r"\bDS\b", "DS", "Dược sĩ đại học"), (r"\bCN\b", "CN", "Cử nhân y tế"),
]


def parse_person(label: str) -> dict:
    """'ThS.Bs Vương Quang Uyển' -> {name, titles:[codes], regulated:'bac-sy'|'dieu-duong'|'ky-thuat-y'|None}."""
    raw = nfc(label or "").strip()
    titles = []
    name = raw
    for pat, code, disp in QUAL:
        if re.search(pat, raw):
            titles.append((code, disp))
    # cắt tiền tố chức danh: phần trước khoảng trắng đầu tiên sau các token viết tắt
    m = re.match(r"^((?:[A-Za-zĐđ\.]+\s*)*?)((?:[A-ZĐ][a-zà-ỹđ]+\s*){2,})$", raw)
    if m:
        name = m.group(2).strip()
    name = re.sub(r"^(PGS|GS|TS|ThS|Th\.S|BS\.?CKII|BS\.?CKI|BSCKII|BSCKI|BSNT|BS|Bs|KTV|ĐD|CNĐD|DS|CN)[\.\s\-]*", "", name).strip()
    name = re.sub(r"^(PGS|GS|TS|ThS|Th\.S|BS\.?CKII|BS\.?CKI|BSCKII|BSCKI|BSNT|BS|Bs|KTV|ĐD|CNĐD|DS|CN)[\.\s\-]*", "", name).strip()
    name = " ".join(w.capitalize() if w.isupper() else w for w in name.split())
    reg = None
    codes = [c for c, _ in titles]
    if any(c.startswith("BS") or c in ("ThS", "TS", "PGS", "GS") for c in codes):
        reg = "bac-sy"
    if "DD" in codes:
        reg = "dieu-duong"
    if "KTV" in codes:
        reg = "ky-thuat-y"
    return {"raw": raw, "name": name, "titles": titles, "regulated": reg}
