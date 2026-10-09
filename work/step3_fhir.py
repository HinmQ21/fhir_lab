"""BƯỚC 3 (làm tay): clinical JSON -> FHIR R4 theo VN Core IG 0.10.0.

Đầu ra: output/<case>/03_fhir/resources/<Type>-<id>.json, bundle-transaction.json, bundle-document.json
Mọi URL canonical/CodeSystem lấy đúng theo package hl7.fhir.vn.core#0.10.0 (vendor/vncore).
"""
from __future__ import annotations

import html
import json
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, dump, load  # noqa: E402
from terminology import SYS, VITALS, VN, icd10_lookup, nfc, norm, parse_person, split_name, vn_datetime  # noqa: E402

CASE = sys.argv[1] if len(sys.argv) > 1 else "BN001736827"
OUT = ROOT / "output" / CASE
FHIR = OUT / "03_fhir"
C = json.loads((OUT / "02_clinical" / "clinical.json").read_text(encoding="utf-8"))

# ---- cấu hình cơ sở (không in trên phiếu; trong pipeline thật lấy từ config cơ sở KCB)
FACILITY = {
    "slug": "bvdk-phutho",
    "base_url": "http://his.bvdk-phutho.vn/fhir",       # fullUrl base cho Bundle
    "ma_cskcb": None,                                    # 5 ký tự theo QĐ BYT nếu biết (vd '25001'); None -> dùng mã nội bộ
    "ten_day_du": "Bệnh viện Đa khoa tỉnh Phú Thọ",
    "so_y_te": "Sở Y tế Phú Thọ",
    "province": ("25", "Tỉnh Phú Thọ"),
    "ward": ("08527", "Xã Bản Nguyên"),  # tra vn-ward-cs theo tên + tỉnh cha (xem step3 notes)
    "mrn_system": "http://his.bvdk-phutho.vn/sid/mrn",
    "staff_system": "http://his.bvdk-phutho.vn/sid/staff-id",
    "order_system": "http://his.bvdk-phutho.vn/sid/y-lenh",
}
PROF = lambda p: f"{VN}/StructureDefinition/{p}"
CS = lambda c: f"{VN}/CodeSystem/{c}"
SID = lambda s: f"{VN}/sid/{s}"
TH = "http://terminology.hl7.org/CodeSystem/"


def slug(s: str) -> str:
    import unicodedata
    s = nfc(s or "").replace("Đ", "D").replace("đ", "d")
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "x"


def coding(system, code, display=None):
    d = {"system": system, "code": code}
    if display:
        d["display"] = display
    return d


def cc(system, code, display=None, text=None):
    d = {"coding": [coding(system, code, display)]} if code else {}
    if text:
        d["text"] = text
    return d


def ref(res, display=None):
    d = {"reference": f"{res['resourceType']}/{res['id']}"}
    if display:
        d["display"] = display
    return d


def narrative(lines: list[str]) -> dict:
    body = "".join(f"<p>{html.escape(str(l))}</p>" for l in lines if l)
    return {"status": "generated", "div": f'<div xmlns="http://www.w3.org/1999/xhtml">{body or "<p>-</p>"}</div>'}


def iso_or_none(s):
    return vn_datetime(s) if s else None


def ddmmyyyy_to_iso(s):
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s or "")
    return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}" if m else None


RES: list[dict] = []


def add(r):
    RES.append(r)
    return r


# ====================================================================== Organization / Location
def build_organizations():
    syt = add({"resourceType": "Organization", "id": f"org-{slug(FACILITY['so_y_te'])}", "meta": {"profile": [PROF("vn-core-organization")]},
               # không dùng sid/organization-local-id cho Sở Y tế: invariant vn-organization-local-id-issuer-scoped đòi assigner.reference
               "identifier": [{"type": {"text": "Mã cơ quan quản lý (cấu hình cơ sở)"}, "system": f"{FACILITY['base_url']}/sid/organization-id", "value": slug(FACILITY["so_y_te"]).upper()}],
               "active": True, "type": [cc(TH + "organization-type", "govt", "Government")], "name": C["organization"]["so_y_te"] or FACILITY["so_y_te"]})
    syt["text"] = narrative([syt["name"]])
    hosp_id = f"org-{FACILITY['slug']}"
    ident = []
    if FACILITY["ma_cskcb"]:
        ident.append({"type": cc(CS("vn-identifier-type-cs"), "CSKCB", "Mã cơ sở khám chữa bệnh"), "system": SID("ma-cskcb"), "value": FACILITY["ma_cskcb"]})
    ident.append({"type": {"text": "Mã cơ sở nội bộ"}, "system": SID("organization-local-id"), "value": FACILITY["slug"].upper(),
                  "assigner": {"reference": f"Organization/{syt['id']}", "display": syt["name"]}})
    hosp = add({"resourceType": "Organization", "id": hosp_id, "meta": {"profile": [PROF("vn-core-organization")]},
                "extension": [
                    {"url": PROF("vn-ext-org-type"), "valueCodeableConcept": cc(CS("vn-organization-type-cs"), "BV", "Bệnh viện")},
                    {"url": PROF("vn-ext-org-level"), "valueCodeableConcept": cc(CS("vn-healthcare-level-cs"), "TINH", "Tỉnh")},
                    {"url": PROF("vn-ext-facility-care-level"), "valueCodeableConcept": cc(CS("vn-facility-care-level-cs"), "CO_BAN", "Cấp cơ bản")},
                ],
                "identifier": ident, "active": True, "type": [cc(TH + "organization-type", "prov", "Healthcare Provider", "Cơ sở khám bệnh, chữa bệnh")],
                "name": FACILITY["ten_day_du"], "alias": [x for x in (C["organization"]["benh_vien"], C["organization"].get("benh_vien_viet_tat")) if x],
                "partOf": ref(syt, syt["name"]),
                "address": [{"extension": [{"url": PROF("vn-ext-province"), "valueCoding": coding(CS("vn-province-cs"), *FACILITY["province"])}],
                             "state": FACILITY["province"][1], "country": "VN"}]})
    hosp["text"] = narrative([hosp["name"]])
    loc = C["location"]
    dept = add({"resourceType": "Organization", "id": f"org-dept-{slug(loc['ma_khoa'] or loc['khoa'])}", "meta": {"profile": [PROF("vn-core-organization-department")]},
                "identifier": [{"type": {"text": "Mã khoa cục bộ"}, "system": SID("organization-local-id"), "value": f"{FACILITY['slug'].upper()}-{loc['ma_khoa']}", "assigner": ref(hosp, hosp["name"])}],
                "active": True,
                "type": [cc(TH + "organization-type", "dept", "Hospital Department"), cc(CS("vn-department-cs"), loc["ma_khoa"], DEPT_NAMES.get(loc["ma_khoa"]))],
                "name": loc["khoa"], "partOf": ref(hosp, hosp["name"])})
    dept["text"] = narrative([dept["name"]])
    ward = add({"resourceType": "Location", "id": f"loc-{slug(loc['ma_khoa'])}", "meta": {"profile": [PROF("vn-core-location")]},
                "identifier": [{"system": SID("location-local-id"), "value": loc["ma_khoa"], "assigner": ref(hosp, hosp["name"])}],
                "status": "active", "name": f"{loc['khoa']}" + (f" - {loc['phong']}" if loc.get("phong") else ""),
                "physicalType": cc(TH + "location-physical-type", "wa", "Ward"), "managingOrganization": ref(dept, dept["name"])})
    ward["text"] = narrative([ward["name"]])
    bed = None
    if loc.get("giuong"):
        bed = add({"resourceType": "Location", "id": f"loc-bed-{slug(loc['giuong'])}", "meta": {"profile": [PROF("vn-core-location")]},
                   "identifier": [{"system": SID("location-local-id"), "value": loc["giuong"], "assigner": ref(hosp, hosp["name"])}],
                   "status": "active", "name": f"Giường {loc['giuong']}", "physicalType": cc(TH + "location-physical-type", "bd", "Bed"),
                   "managingOrganization": ref(dept, dept["name"]), "partOf": ref(ward)})
        bed["text"] = narrative([bed["name"]])
    return syt, hosp, dept, ward, bed


DEPT_NAMES = {c["code"]: c["display"] for c in json.loads((ROOT / "vendor/vncore/package/CodeSystem-vn-department-cs.json").read_text(encoding="utf-8"))["concept"]}
QUAL_NAMES = {c["code"]: c["display"] for c in json.loads((ROOT / "vendor/vncore/package/CodeSystem-vn-qualification-cs.json").read_text(encoding="utf-8"))["concept"]}
BHYT_TYPES = {c["code"]: c["display"] for c in json.loads((ROOT / "vendor/vncore/package/CodeSystem-vn-bhyt-card-type-cs.json").read_text(encoding="utf-8"))["concept"]}
ETHNIC = {c["display"].lower(): c for c in json.loads((ROOT / "vendor/vncore/package/CodeSystem-vn-ethnicity-cs.json").read_text(encoding="utf-8"))["concept"]}


# ====================================================================== Practitioner
PRACS: dict[str, dict] = {}


def practitioner(person: dict | str | None, hosp=None, dept=None, role_code=None):
    """person: dict từ parse_person hoặc chuỗi 'ThS.Bs Vương Quang Uyển'. Trả Practitioner (tạo một lần theo tên)."""
    if not person:
        return None
    if isinstance(person, str):
        person = parse_person(person)
    name = person.get("name") or person.get("raw")
    if not name:
        return None
    key = slug(name)
    if key in PRACS:
        return PRACS[key]
    fam, given = split_name(name)
    quals = [{"code": cc(CS("vn-qualification-cs"), code, QUAL_NAMES.get(code, disp)), "issuer": {"display": "Bộ Y tế"}} for code, disp in person.get("titles", [])]
    p = add({"resourceType": "Practitioner", "id": f"prac-{key}", "meta": {"profile": [PROF("vn-core-practitioner")]},
             "identifier": [{"type": {"text": "Mã nhân viên nội bộ (HIS)"}, "system": FACILITY["staff_system"], "value": key.upper(), "assigner": {"display": FACILITY["ten_day_du"]}}],
             "active": True, "name": [{"text": person.get("raw") or name, "family": fam, "given": given, "prefix": [t[0] for t in person.get("titles", [])] or None}]})
    if not p["name"][0]["prefix"]:
        del p["name"][0]["prefix"]
    if quals:
        p["qualification"] = quals
    p["text"] = narrative([p["name"][0]["text"]])
    PRACS[key] = p
    if hosp is not None and dept is not None:
        role = add({"resourceType": "PractitionerRole", "id": f"role-{key}", "meta": {"profile": [PROF("vn-core-practitioner-role")]},
                    "active": True, "practitioner": ref(p, p["name"][0]["text"]), "organization": ref(dept, dept["name"])})
        if role_code:
            role["code"] = [{"text": role_code}]
        role["text"] = narrative([f"{p['name'][0]['text']} - {dept['name']}"])
    return p


def pdisp(p):
    return p["name"][0]["text"] if p else None


# ====================================================================== Patient / Coverage / RelatedPerson
def build_patient(hosp):
    pt = C["patient"]
    ident = []
    if pt.get("so_cccd"):
        ident.append({"type": {"coding": [coding(CS("vn-identifier-type-cs"), "CCCD", "Căn cước công dân"), coding(TH + "v2-0203", "NI", "National unique individual identifier")]},
                      "system": SID("cccd"), "value": pt["so_cccd"]})
    if C["coverage"].get("so_the_bhyt"):
        ident.append({"type": cc(CS("vn-identifier-type-cs"), "BHYT", "Thẻ bảo hiểm y tế"), "system": SID("bhyt"), "value": C["coverage"]["so_the_bhyt"]})
    if pt.get("ma_benh_nhan"):
        ident.append({"type": {"coding": [coding(CS("vn-identifier-type-cs"), "MRN", "Mã bệnh nhân nội bộ"), coding(TH + "v2-0203", "MR", "Medical record number")]},
                      "system": FACILITY["mrn_system"], "value": pt["ma_benh_nhan"], "assigner": ref(hosp, hosp["name"])})
    if C["document_context"].get("ma_benh_an"):
        ident.append({"type": {"text": "Mã bệnh án"}, "system": f"{FACILITY['mrn_system'].rsplit('/', 1)[0]}/ma-benh-an", "value": C["document_context"]["ma_benh_an"], "assigner": ref(hosp, hosp["name"])})
    fam, given = split_name(pt["ho_ten"].title() if pt["ho_ten"].isupper() else pt["ho_ten"])
    dc = pt["dia_chi"]
    addr = {"extension": [{"url": PROF("vn-ext-province"), "valueCoding": coding(CS("vn-province-cs"), *FACILITY["province"])}],
            "text": dc.get("dia_chi_day_du"), "line": [x for x in (dc.get("thon_pho"),) if x], "city": dc.get("xa_phuong"), "state": dc.get("tinh_thanh_pho"), "country": "VN"}
    if FACILITY["ward"] and dc.get("xa_phuong") and norm(dc["xa_phuong"]) == norm(FACILITY["ward"][1]):
        addr["extension"].append({"url": PROF("vn-ext-ward"), "valueCoding": coding(CS("vn-ward-cs"), *FACILITY["ward"])})
    ext = []
    eth = ETHNIC.get((pt.get("dan_toc") or "").lower())
    if eth:
        ext.append({"url": PROF("vn-ext-ethnicity"), "valueCodeableConcept": cc(CS("vn-ethnicity-cs"), eth["code"], eth["display"])})
    if pt.get("nghe_nghiep"):
        ext.append({"url": PROF("vn-ext-occupation"), "valueCodeableConcept": {"text": pt["nghe_nghiep"]}})  # HIS mã '00' không thuộc vn-occupation-cs
    if (pt.get("ma_ngoai_kieu") or "VN") == "VN":
        ext.append({"url": "http://hl7.org/fhir/StructureDefinition/patient-citizenship", "extension": [{"url": "code", "valueCodeableConcept": cc("urn:iso:std:iso:3166", "VN", "Viet Nam")}]})
    p = add({"resourceType": "Patient", "id": f"pat-{slug(pt['ma_benh_nhan'])}", "meta": {"profile": [PROF("vn-core-patient")]}, "extension": ext, "identifier": ident,
             "active": True, "name": [{"use": "official", "text": pt["ho_ten"], "family": fam, "given": given}],
             "gender": {"Nữ": "female", "Nam": "male"}.get(pt.get("gioi_tinh"), "unknown"), "birthDate": ddmmyyyy_to_iso(pt.get("ngay_thang_nam_sinh")), "address": [addr],
             "managingOrganization": ref(hosp, hosp["name"])})
    p["text"] = narrative([f"{pt['ho_ten']} - {pt.get('gioi_tinh')} - sinh {pt.get('ngay_thang_nam_sinh')}", f"CCCD {pt.get('so_cccd')} / BHYT {C['coverage'].get('so_the_bhyt')} / MRN {pt.get('ma_benh_nhan')}"])
    rp = C.get("related_person") or {}
    if rp.get("ho_ten_dia_chi_nguoi_nha"):
        add({"resourceType": "RelatedPerson", "id": f"rel-{p['id']}", "patient": ref(p), "name": [{"text": rp["ho_ten_dia_chi_nguoi_nha"]}],
             "telecom": [{"system": "phone", "value": rp["dien_thoai_nguoi_nha"]}] if rp.get("dien_thoai_nguoi_nha") else None,
             "relationship": [{"text": rp.get("quan_he") or "Người nhà"}]})
    return p


def build_coverage(patient):
    cv = C["coverage"]
    if not cv.get("so_the_bhyt"):
        return None
    ext = []
    if cv.get("ma_doi_tuong_the") and cv["ma_doi_tuong_the"] in BHYT_TYPES:
        ext.append({"url": PROF("vn-ext-bhyt-card-type"), "valueCodeableConcept": cc(CS("vn-bhyt-card-type-cs"), cv["ma_doi_tuong_the"], BHYT_TYPES[cv["ma_doi_tuong_the"]])})
    if cv.get("noi_đk_kcb_bhyt", {}).get("ma"):
        ext.append({"url": PROF("vn-ext-primary-care-facility"), "valueReference": {"identifier": {"system": SID("ma-cskcb"), "value": cv["noi_đk_kcb_bhyt"]["ma"]}, "display": cv["noi_đk_kcb_bhyt"].get("text")}})
    cov = add({"resourceType": "Coverage", "id": f"cov-{cv['so_the_bhyt']}", "meta": {"profile": [PROF("vn-core-coverage")]}, "extension": ext,
               "identifier": [{"type": cc(CS("vn-identifier-type-cs"), "BHYT", "Thẻ bảo hiểm y tế"), "system": SID("bhyt"), "value": cv["so_the_bhyt"]}],
               "status": "active", "type": {"coding": [coding(TH + "insurance-coverage-type", "medical", "Medical")], "text": "Bảo hiểm y tế"},
               "subscriber": ref(patient, C["patient"]["ho_ten"]), "beneficiary": ref(patient, C["patient"]["ho_ten"]),
               "relationship": cc(TH + "subscriber-relationship", "self", "Self"),
               "period": {k: v for k, v in (("start", ddmmyyyy_to_iso(cv.get("bhyt_gia_tri_tu"))), ("end", ddmmyyyy_to_iso(cv.get("bhyt_gia_tri_den")) or iso_or_none(cv.get("bhyt_gia_tri_den")))) if v},
               "payor": [{"display": "Bảo hiểm Xã hội Việt Nam"}]})
    pct = re.search(r"(\d+)\s*%", cv.get("muc_huong_bhyt") or "")
    if pct:
        cov["costToBeneficiary"] = [{"type": {"text": "Mức hưởng BHYT"}, "valueQuantity": {"value": int(pct.group(1)), "unit": "%", "system": SYS["ucum"], "code": "%"}}]
    cov["text"] = narrative([f"Thẻ BHYT {cv['so_the_bhyt']} ({BHYT_TYPES.get(cv.get('ma_doi_tuong_the'), '')}) - mức hưởng {cv.get('muc_huong_bhyt')}", f"Hiệu lực {cv.get('bhyt_gia_tri_tu')} - {cv.get('bhyt_gia_tri_den')} - ĐK KCB ban đầu: {cv['noi_đk_kcb_bhyt'].get('text')} ({cv['noi_đk_kcb_bhyt'].get('ma')})"])
    return cov


# ====================================================================== Condition / Encounter
def build_conditions(patient, enc_id, recorder):
    conds = []
    enc = C["encounter"]
    for i, d in enumerate(C["diagnoses"], 1):
        if not d.get("code"):
            continue
        info = icd10_lookup(d["code"])
        codings = [coding(SYS["icd10vn"], d["code"], info["vi"] if info else d.get("display")), coding(SYS["icd10who"], d["code"], info["en"] if info else d.get("display_en"))]
        provisional = bool(re.search(r"\bTD\b|theo dõi|nghi", d.get("raw") or "", re.I)) and d["type"] == "AD"
        when = enc["vao_vien_iso"] if d["type"] == "AD" else enc["ra_vien_iso"]
        cnd = add({"resourceType": "Condition", "id": f"cond-{d['type'].lower()}-{i}", "meta": {"profile": [PROF("vn-core-condition-diagnosis")]},
                   "clinicalStatus": cc(TH + "condition-clinical", "active", "Active"),
                   "verificationStatus": cc(TH + "condition-ver-status", "provisional" if provisional else "confirmed", "Provisional" if provisional else "Confirmed"),
                   "category": [cc(TH + "condition-category", "encounter-diagnosis", "Encounter Diagnosis")],
                   "code": {"coding": codings, "text": d["text"]}, "subject": ref(patient, C["patient"]["ho_ten"]), "encounter": {"reference": f"Encounter/{enc_id}"},
                   "recordedDate": (when or "")[:10] or None, "recorder": ref(recorder, pdisp(recorder)) if recorder else None,
                   "note": [{"text": f"Nguồn: {d.get('source')}; mã hoá: {d.get('code_source')}"}]})
        cnd["text"] = narrative([f"{d['type']} #{d.get('rank')}: {d['text']} -> {d['code']} {cnd['code']['coding'][0].get('display', '')}"])
        conds.append((d, cnd))
    return conds


def build_encounter(patient, hosp, dept, ward, bed, conds, doctors):
    e = C["encounter"]; dis = C["discharge"]
    enc_id = f"enc-{slug(e.get('so_kham_benh') or e.get('so_vao_vien') or C['patient']['ma_benh_nhan'])}"
    ext = []
    if dis.get("ma_ket_qua_dieu_tri"):
        ext.append({"url": PROF("vn-ext-treatment-outcome"), "valueCodeableConcept": cc(CS("vn-treatment-outcome-cs"), dis["ma_ket_qua_dieu_tri"], dis.get("ten_ket_qua_dieu_tri"), dis.get("ket_qua_dieu_tri"))})
    if e.get("tuyen"):
        ext.append({"url": PROF("vn-ext-insurance-visit-type"), "valueCodeableConcept": cc(CS("vn-insurance-visit-type-cs"), e["tuyen"]["ma"], e["tuyen"]["text"])})
    ext.append({"url": PROF("vn-ext-referral-mode"), "valueCodeableConcept": cc(CS("vn-referral-mode-cs"), "5" if e.get("cap_cuu") else "1", "Cấp cứu" if e.get("cap_cuu") else "Đến khám")})
    period = {k: v for k, v in (("start", e.get("vao_vien_iso")), ("end", e.get("ra_vien_iso"))) if v}
    dept_period = {k: v for k, v in (("start", e["vao_khoa"].get("thoi_gian_iso")), ("end", e.get("ra_vien_iso"))) if v}
    enc = {"resourceType": "Encounter", "id": enc_id, "meta": {"profile": [PROF("vn-core-encounter")]}, "extension": ext,
           "identifier": [{"type": cc(CS("vn-identifier-type-cs"), "MA_LUOT_KCB", "Mã lượt khám chữa bệnh"), "system": SID("ma-luot-kcb"), "value": e.get("so_kham_benh") or e.get("so_vao_vien"), "assigner": ref(hosp, hosp["name"])}],
           "status": "finished", "class": coding(TH + "v3-ActCode", "IMP", "inpatient encounter"),
           "type": [cc(CS("vn-encounter-type-cs"), e["loai_kcb"]["ma"], e["loai_kcb"]["text"])],
           "subject": ref(patient, C["patient"]["ho_ten"]), "period": period,
           "reasonCode": [{"text": e.get("ly_do_vao_vien")}] if e.get("ly_do_vao_vien") else None,
           "diagnosis": [{"condition": ref(cnd, f"{d['text']} ({d['code']})"), "use": cc(TH + "diagnosis-role", d["type"], "Admission diagnosis" if d["type"] == "AD" else "Discharge diagnosis"), "rank": int(d["rank"]) if d.get("rank") else None} for d, cnd in conds],
           "hospitalization": {"admitSource": {"text": "Đến khám trực tiếp"} if not e.get("cap_cuu") else {"text": "Cấp cứu"},
                               "dischargeDisposition": cc(CS("vn-discharge-disposition-cs"), dis["ma_hinh_thuc_ra_vien"], dis["ten_hinh_thuc_ra_vien"], dis.get("huong_dieu_tri"))},
           "location": [{"location": ref(ward, ward["name"]), "status": "completed", "period": dept_period}] + ([{"location": ref(bed, bed["name"]), "status": "completed", "period": dept_period}] if bed else []),
           "serviceProvider": ref(hosp, hosp["name"]),
           "participant": [{"type": [cc(TH + "v3-ParticipationType", t, n)], "individual": ref(p, pdisp(p))} for t, n, p in doctors if p]}
    if e.get("so_vao_vien"):
        enc["identifier"].append({"type": {"text": "Số vào viện"}, "system": f"{FACILITY['mrn_system'].rsplit('/', 1)[0]}/so-vao-vien", "value": e["so_vao_vien"], "assigner": ref(hosp, hosp["name"])})
    if e.get("tong_so_ngay_dieu_tri"):
        enc["length"] = {"value": int(e["tong_so_ngay_dieu_tri"]), "unit": "ngày", "system": SYS["ucum"], "code": "d"}
    enc["text"] = narrative([f"Nội trú {e.get('vao_vien')} -> {e.get('ra_vien')} tại {e['vao_khoa']['khoa']} ({e['vao_khoa']['ma_khoa']})", f"Kết quả: {dis.get('ket_qua_dieu_tri')}; hình thức ra viện: {dis.get('ten_hinh_thuc_ra_vien')}"])
    return add(enc)


# ====================================================================== Observations
def quantity(value, unit, code):
    return {"value": value, "unit": unit, "system": SYS["ucum"], "code": code}


def build_vitals(patient, enc):
    out = []
    for i, o in enumerate(C["observations"], 1):
        if o.get("type") != "sign" or not o.get("code"):
            continue
        key = next((k for k, v in VITALS.items() if v[0] == o["code"]), None)
        if not key:
            continue
        loinc, disp_en, disp_vi, ucum_code, unit, profile = VITALS[key]
        ob = {"resourceType": "Observation", "id": f"obs-vs-{i}", "meta": {"profile": [PROF(profile)] + ([PROF("vn-core-observation-vital-signs")] if profile != "vn-core-observation-vital-signs" else [])},
              "status": "final", "category": [cc(TH + "observation-category", "vital-signs", "Vital Signs")],
              "code": {"coding": [coding(SYS["loinc"], loinc, disp_en)], "text": disp_vi}, "subject": ref(patient, C["patient"]["ho_ten"]), "encounter": ref(enc),
              "effectiveDateTime": o.get("time_iso") or C["encounter"]["vao_vien_iso"]}
        u = o["ucum"]
        if key == "huyet_ap":
            ob["component"] = [{"code": {"coding": [coding(SYS["loinc"], "8480-6", "Systolic blood pressure")], "text": "Tâm thu"}, "valueQuantity": quantity(u["components"]["systolic"], "mmHg", "mm[Hg]")},
                               {"code": {"coding": [coding(SYS["loinc"], "8462-4", "Diastolic blood pressure")], "text": "Tâm trương"}, "valueQuantity": quantity(u["components"]["diastolic"], "mmHg", "mm[Hg]")}]
        else:
            ob["valueQuantity"] = quantity(u["value"], unit, ucum_code)
        ob["note"] = [{"text": f"Nguồn: {o.get('source')} ({', '.join(o.get('time') or [])})"}]
        ob["text"] = narrative([f"{disp_vi}: {o.get('text')} lúc {o.get('time_iso')}"])
        out.append(add(ob))
    return out


SPECIMEN_TYPES = {"huyet_hoc": ("BLD", "Whole blood", "Máu toàn phần (EDTA)"), "sinh_hoa_mau": ("SER", "Serum", "Huyết thanh"), "nuoc_tieu": ("UR", "Urine", "Nước tiểu")}
PANEL_CODES = {"huyet_hoc": ("57021-8", "CBC W Auto Differential panel - Blood", "1000147"), "sinh_hoa_mau": ("24323-8", "Comprehensive metabolic 2000 panel - Serum or Plasma", None), "nuoc_tieu": ("50556-0", "Urinalysis dipstick panel - Urine by Automated test strip", "1010290")}
INTERP = {"H": ("H", "High"), "L": ("L", "Low"), "A": ("A", "Abnormal"), "N": ("N", "Normal")}


def build_labs(patient, enc, hosp, dept, srs_by_name, docrefs):
    reports_out = []
    lab_obs_all = []
    for key, rep in C["lab_reports"].items():
        st = SPECIMEN_TYPES[key]
        collector = practitioner(rep.get("nguoi_lay_mau"))
        spec = add({"resourceType": "Specimen", "id": f"spec-{key.replace('_', '-')}", "meta": {"profile": [PROF("vn-core-specimen")]},
                    "identifier": [{"system": SID("specimen-local-id"), "value": rep["ma_benh_pham"], "assigner": ref(hosp, hosp["name"])}] if rep.get("ma_benh_pham") else None,
                    "status": "available", "type": {"coding": [coding(TH + "v2-0487", st[0], st[1])], "text": rep.get("loai_mau") or st[2]},
                    "subject": ref(patient, C["patient"]["ho_ten"]), "receivedTime": vn_datetime(rep.get("thoi_gian_nhan_mau")),
                    "collection": {k: v for k, v in (("collectedDateTime", vn_datetime(rep.get("thoi_gian_lay_mau"))), ("collector", ref(collector, pdisp(collector)) if collector else None)) if v} or None,
                    "condition": [{"text": rep["tinh_trang_mau"]}] if rep.get("tinh_trang_mau") else None})
        spec["text"] = narrative([f"Mẫu {spec['type']['text']} {rep.get('ma_benh_pham') or ''} nhận lúc {rep.get('thoi_gian_nhan_mau') or rep.get('thoi_gian_thuc_hien')}"])
        performer = practitioner(rep.get("bs_tra_kq") or rep.get("nguoi_thuc_hien"))
        # ObservationLab bắt buộc effective[x]; phiếu nước tiểu của HIS không in giờ -> lùi về giờ nhận mẫu sinh hoá cùng mã số, cuối cùng là giờ vào viện
        eff = rep.get("thoi_gian_mau_iso") or rep.get("thoi_gian_iso") or next((x.get("thoi_gian_mau_iso") for x in C["lab_reports"].values() if x.get("thoi_gian_mau_iso")), None) or C["encounter"]["vao_vien_iso"]
        eff_note = None if (rep.get("thoi_gian_mau_iso") or rep.get("thoi_gian_iso")) else "Phiếu không in thời gian; effectiveDateTime lấy theo phiếu xét nghiệm cùng đợt"
        members = []
        for i, l in enumerate([x for x in C["lab_results"] if x["report"] == key], 1):
            codings = []
            if l.get("code"):
                codings.append(coding(SYS["loinc"], l["code"], l.get("display_en") or l.get("display")))
            ob = {"resourceType": "Observation", "id": f"obs-lab-{key.replace('_', '-')}-{i}", "meta": {"profile": [PROF("vn-core-observation-lab")]},
                  "status": "final", "category": [cc(TH + "observation-category", "laboratory", "Laboratory")],
                  "code": {"coding": codings, "text": l["ten_xet_nghiem"]} if codings else {"text": l["ten_xet_nghiem"]},
                  "subject": ref(patient, C["patient"]["ho_ten"]), "encounter": ref(enc), "effectiveDateTime": eff,
                  "issued": rep.get("thoi_gian_iso") or eff, "performer": [ref(performer, pdisp(performer))] if performer else [ref(hosp, hosp["name"])], "specimen": ref(spec, spec["type"]["text"])}
            u = l["ucum"]
            if u.get("value") is not None and u.get("code"):
                ob["valueQuantity"] = quantity(u["value"], u.get("unit") or u["code"], u["code"])
            elif u.get("value") is not None:
                # kết quả số không có đơn vị (pH, tỷ trọng...): VN Core vẫn đòi mã UCUM -> dùng '[pH]' hoặc '1' (không thứ nguyên)
                code = "[pH]" if norm(l["ten_xet_nghiem"]) == "ph" else "1"
                ob["valueQuantity"] = quantity(u["value"], u.get("unit_raw") or ("pH" if code == "[pH]" else None), code)
            elif u.get("value_text"):
                ob["valueString"] = u["value_text"]
            else:
                ob["dataAbsentReason"] = cc(TH + "data-absent-reason", "not-performed", "Not Performed")
            if l.get("đánh_dấu") in INTERP:
                ob["interpretation"] = [cc(TH + "v3-ObservationInterpretation", *INTERP[l["đánh_dấu"]])]
            if l.get("tham_chiếu"):
                rr = {"text": l["tham_chiếu"]}
                if l.get("ref_low") is not None and u.get("code"):
                    rr["low"] = quantity(l["ref_low"], u.get("unit") or u["code"], u["code"])
                    rr["high"] = quantity(l["ref_high"], u.get("unit") or u["code"], u["code"])
                ob["referenceRange"] = [rr]
            if l.get("quy_trinh"):
                ob["method"] = {"text": l["quy_trinh"].replace("\n", " ")}
            if l.get("ghi_chu") or eff_note:
                ob["note"] = [{"text": x} for x in (l.get("ghi_chu"), eff_note) if x]
            ob["text"] = narrative([f"{l['ten_xet_nghiem']}: {l.get('original_text')} {u.get('unit_raw') or ''} {('[' + l['đánh_dấu'] + ']') if l.get('đánh_dấu') else ''} (BT {l.get('tham_chiếu') or '-'})"])
            members.append(add(ob))
        lab_obs_all += members
        pc = PANEL_CODES[key]
        panel_codings = [coding(SYS["loinc"], pc[0], pc[1])] + ([coding(SYS["cls"], pc[2])] if pc[2] else [])
        panel_name = (rep.get("panels") or [{}])[0].get("ten") or key
        sr = srs_by_name.get(key)
        dr = add({"resourceType": "DiagnosticReport", "id": f"dr-lab-{key.replace('_', '-')}", "meta": {"profile": [PROF("vn-core-diagnostic-report-lab")]},
                  "basedOn": [ref(sr)] if sr else None, "status": "final",
                  "category": [cc(TH + "v2-0074", "LAB", "Laboratory"), cc(CS("vn-cost-category-cs"), "3", "Xét nghiệm")],
                  "code": {"coding": panel_codings, "text": panel_name}, "subject": ref(patient, C["patient"]["ho_ten"]), "encounter": ref(enc),
                  "effectiveDateTime": eff, "issued": rep.get("thoi_gian_iso") or eff,
                  "performer": [ref(hosp, hosp["name"])], "resultsInterpreter": [ref(performer, pdisp(performer))] if performer else None,
                  "specimen": [ref(spec, spec["type"]["text"])], "result": [ref(m, m["code"].get("text")) for m in members],
                  "conclusion": rep.get("nhan_xet"), "presentedForm": [docrefs[rep["file"]]["content"][0]["attachment"]] if rep["file"] in docrefs else None})
        dr["text"] = narrative([f"{panel_name} - {len(members)} chỉ số - trả KQ {rep.get('thoi_gian_tra_kq') or rep.get('thoi_gian_thuc_hien')}"] + [f"{m['code'].get('text')}: {m.get('valueQuantity', {}).get('value', m.get('valueString', '-'))}" for m in members])
        reports_out.append(dr)
    return reports_out, lab_obs_all


def build_imaging(patient, enc, hosp, srs_by_name, docrefs):
    out = []
    for i, r in enumerate(C["diagnostic_reports"], 1):
        is_rad = r.get("category") == "CDHA"
        interp = practitioner(r.get("bac_si_chuyen_khoa"))
        codings = ([coding(SYS["loinc"], r["code"], r.get("display"))] if r.get("code") else []) + ([coding(SYS["cls"], r["cls"])] if r.get("cls") else [])
        sr = srs_by_name.get(norm(r["ucum"]["key"]))
        dr = {"resourceType": "DiagnosticReport", "id": f"dr-img-{i}", "meta": {"profile": [PROF("vn-core-diagnostic-report-imaging" if is_rad else "vn-core-diagnostic-report")]},
              "basedOn": [ref(sr)] if sr else None, "status": "final",
              "category": [cc(TH + "v2-0074", "RAD" if is_rad else "OTH", "Radiology" if is_rad else "Other"), cc(CS("vn-cost-category-cs"), "4" if is_rad else "5", "Chẩn đoán hình ảnh" if is_rad else "Thăm dò chức năng")],
              "code": {"coding": codings, "text": r["ucum"]["key"]} if codings else {"text": r["ucum"]["key"]},
              "subject": ref(patient, C["patient"]["ho_ten"]), "encounter": ref(enc), "effectiveDateTime": r.get("time_iso"), "issued": r.get("time_iso"),
              "performer": [ref(hosp, hosp["name"])], "resultsInterpreter": [ref(interp, pdisp(interp))] if interp else None,
              "conclusion": r.get("original_text"), "presentedForm": [docrefs[r["source"]]["content"][0]["attachment"]] if r.get("source") in docrefs else None}
        dr["text"] = narrative([f"{r['ucum']['key']}", f"Kỹ thuật: {r.get('ky_thuat')}" if r.get("ky_thuat") else None] + [f"- {m}" for m in (r.get("mo_ta") or [])] + [f"Kết luận: {r.get('original_text')}"])
        out.append(add(dr))
    return out


# ====================================================================== ServiceRequest / MedicationRequest
ROUTES = {"uống": ("26643006", "Oral route", "1.01", "Uống"), "tiêm tĩnh mạch": ("47625008", "Intravenous route", "2.04", "Tiêm tĩnh mạch"), "truyền tĩnh mạch": ("47625008", "Intravenous route", "2.14", "Truyền tĩnh mạch"),
          "tiêm bắp": ("78421000", "Intramuscular route", "2.01", "Tiêm bắp"), "tiêm dưới da": ("34206005", "Subcutaneous route", "2.02", "Tiêm dưới da"), "bôi": ("6064005", "Topical route", "3.01", "Bôi"), "nhỏ mắt": ("54485002", "Ophthalmic route", "6.01", "Nhỏ mắt")}


def build_service_requests(patient, enc, conds):
    by_name = {}
    reason = [cnd["code"] for d, cnd in conds if d["type"] == "AD"][:1]
    for i, s in enumerate(C.get("service_requests", []), 1):
        requester = practitioner(s.get("bac_si"))
        from terminology import imaging_loinc, cls_search
        codings = []
        hit = imaging_loinc(s["ten_dich_vu"]) if s["loai"] == "CDHA" else None
        if hit:
            codings.append(coding(SYS["loinc"], hit[0], hit[1]))
            if hit[2]:
                codings.append(coding(SYS["cls"], hit[2]))
        else:
            c = cls_search(s["ten_dich_vu"], min_ratio=0.8)
            if c:
                codings.append(coding(SYS["cls"], c["code"], c["display"]))
                if c["loinc"]:
                    codings.append(coding(SYS["loinc"], c["loinc"][0][0], c["loinc"][0][1]))
        cat = {"XN": ("XN", "Xét nghiệm"), "CDHA": ("CDHA", "Chẩn đoán hình ảnh")}[s["loai"]]
        if "điện não" in norm(s["ten_dich_vu"]):
            cat = ("TDCN", "Thăm dò chức năng")
        sr = add({"resourceType": "ServiceRequest", "id": f"sr-{i}", "meta": {"profile": [PROF("vn-core-service-request")]},
                  "identifier": [{"system": FACILITY["order_system"], "value": f"{s.get('ngay_y_lenh_iso', '')}-{i}"}], "status": "completed", "intent": "order",
                  "category": [cc(CS("vn-service-request-category-cs"), *cat)], "priority": "routine",
                  "code": {"coding": codings, "text": s["ten_dich_vu"]} if codings else {"text": s["ten_dich_vu"]},
                  "subject": ref(patient, C["patient"]["ho_ten"]), "encounter": ref(enc), "authoredOn": s.get("ngay_y_lenh_iso") or s.get("ngay_iso"),
                  "requester": ref(requester, pdisp(requester)) if requester else None, "reasonCode": reason or None})
        sr["text"] = narrative([f"Chỉ định {cat[1]}: {s['ten_dich_vu']} ({s.get('ngay_y_lenh')})"])
        by_name[norm(s["ten_dich_vu"])] = sr
        # ánh xạ nhóm XN -> phiếu kết quả
        n = norm(s["ten_dich_vu"])
        if "tế bào máu" in n:
            by_name["huyet_hoc"] = sr
        elif "nước tiểu" in n:
            by_name["nuoc_tieu"] = sr
        elif any(k in n for k in ("glucose", "creatinin", "cholesterol")) and "sinh_hoa_mau" not in by_name:
            by_name["sinh_hoa_mau"] = sr
    return by_name


def build_medications(patient, enc, conds):
    out = []
    dd = [cnd for d, cnd in conds if d["type"] == "DD"]
    for i, m in enumerate(C["medication_requests"], 1):
        requester = practitioner(m.get("bac_si"))
        route = next((v for k, v in ROUTES.items() if k in norm(m.get("duong_dung") or "")), None)
        times = re.findall(r"\d{1,2}h(?:\d{2})?", m.get("cach_dung") or "")
        dosage = {"sequence": 1, "text": (m.get("dosage") or "").replace("\n", "; "), "patientInstruction": m.get("cach_dung")}
        if times:
            tod = []
            for t in times:  # '8h34' -> 08:34:00, '16h' -> 16:00:00 (FHIR time = hh:mm:ss)
                hh, _, mm = t.partition("h")
                tod.append(f"{int(hh):02d}:{int(mm or 0):02d}:00")
            dosage["timing"] = {"repeat": {"frequency": len(times), "period": 1, "periodUnit": "d", "timeOfDay": tod}}
        if route:
            dosage["route"] = {"coding": [coding(SYS["snomed"], route[0], route[1]), coding(CS("vn-medication-route-cs"), route[2], route[3])], "text": m.get("duong_dung")}
        if m.get("so_luong") and times:
            per = m["so_luong"] / len(times)
            dosage["doseAndRate"] = [{"doseQuantity": {"value": per, "unit": m.get("don_vi"), "system": SYS["ucum"], "code": "{tbl}" if (m.get("don_vi") or "").lower() == "viên" else "1"}}]
        mr = add({"resourceType": "MedicationRequest", "id": f"mr-{i}", "meta": {"profile": [PROF("vn-core-medication-request")]},
                  "status": "completed", "intent": "order", "category": [cc(TH + "medicationrequest-category", "inpatient", "Inpatient")],
                  "medicationCodeableConcept": {"coding": [coding(SYS["atc"], m["code"], m.get("display"))] if m.get("code") else [], "text": m["original_text"]},
                  "subject": ref(patient, C["patient"]["ho_ten"]), "encounter": ref(enc), "authoredOn": m.get("ngay_y_lenh_iso") or m.get("ngay_su_dung_iso") or C["encounter"]["vao_vien_iso"],
                  "requester": ref(requester, pdisp(requester)) if requester else None, "reasonReference": [ref(c) for c in dd] or None,
                  "dosageInstruction": [dosage],
                  "dispenseRequest": {"quantity": {"value": m["so_luong"], "unit": m.get("don_vi"), "system": SYS["ucum"], "code": "{tbl}" if (m.get("don_vi") or "").lower() == "viên" else "1"}} if m.get("so_luong") else None})
        if not mr["medicationCodeableConcept"]["coding"]:
            del mr["medicationCodeableConcept"]["coding"]
        mr["text"] = narrative([f"{m['original_text']} x {m.get('so_luong')} {m.get('don_vi')}; {m.get('cach_dung')}; ngày {m.get('ngay_su_dung')}"])
        out.append(mr)
    return out


# ====================================================================== Hội chẩn, tiền sử, dị ứng, tài liệu
def build_clinical_impression(patient, enc, conds):
    hc = C["practitioner"]
    docs_hc = [d for d in C["document_reference"]["pdf"] if d["form_code"] == "trich-bien-ban-hoi-chan"]
    if not docs_hc:
        return None
    data = load(OUT / "01_documents" / next(x["data_file"] for x in load(OUT / "manifest.json")["documents"] if x["form"] == "trich-bien-ban-hoi-chan"))
    bn = data.get("THÔNG TIN BỆNH NHÂN", {})
    assessor = practitioner(hc.get("truong_khoa"))
    when = vn_datetime(bn.get("Hội chẩn lúc"))
    ci = add({"resourceType": "ClinicalImpression", "id": "ci-hoi-chan-1", "meta": {"profile": [PROF("vn-core-clinical-impression")]}, "status": "completed",
              "description": "Trích biên bản hội chẩn", "subject": ref(patient, C["patient"]["ho_ten"]), "encounter": ref(enc), "effectiveDateTime": when, "date": when,
              "assessor": ref(assessor, pdisp(assessor)) if assessor else None,
              "summary": "\n".join(data.get("Tóm tắt quá trình diễn biến bệnh, quá trình điều trị và chăm sóc người bệnh") or []) + "\nKết luận: " + (data.get("Kết luận (Sau khi đã khám lại và thảo luận)") or ""),
              "finding": [{"itemReference": ref(cnd, d["text"])} for d, cnd in conds if d["type"] == "AD"],
              "note": [{"text": "Hướng điều trị tiếp: " + (data.get("Hướng điều trị tiếp") or "")}]})
    members = [practitioner(p) for p in hc.get("thanh_vien_hoi_chan", []) + [hc.get("thu_ky_hoi_chan")] if p]
    if members:
        ci["supportingInfo"] = [ref(p, pdisp(p)) for p in members if p]
    ci["text"] = narrative([ci["summary"]])
    return ci


def build_histories(patient, enc):
    out = []
    fh = C["family_histories"].get("tien_su_gia_dinh")
    if fh:
        f = add({"resourceType": "FamilyMemberHistory", "id": "fmh-1", "meta": {"profile": [PROF("vn-core-family-member-history")]}, "status": "completed",
                 "patient": ref(patient, C["patient"]["ho_ten"]), "relationship": {"coding": [coding(TH + "v3-RoleCode", "FAMMEMB", "family member")], "text": "Gia đình"},
                 "note": [{"text": f"Tiền sử gia đình: {fh}"}]})
        f["text"] = narrative([f"Tiền sử gia đình: {fh}"])
        out.append(f)
    # dị ứng từ phiếu chăm sóc
    for d in load(OUT / "manifest.json")["documents"]:
        if d["form"] == "phieu-cham-soc":
            data = load(OUT / "01_documents" / d["data_file"])
            da = data.get("Tiền sử dị ứng") or {}
            if da.get("Chưa ghi nhận") and not da.get("Nội dung"):
                a = add({"resourceType": "AllergyIntolerance", "id": "allergy-nka", "meta": {"profile": [PROF("vn-core-allergy-intolerance")]},
                         "clinicalStatus": cc(TH + "allergyintolerance-clinical", "active", "Active"), "verificationStatus": cc(TH + "allergyintolerance-verification", "unconfirmed", "Unconfirmed"),
                         "code": cc(SYS["snomed"], "716186003", "No known allergy", "Chưa ghi nhận tiền sử dị ứng"), "patient": ref(patient, C["patient"]["ho_ten"]), "encounter": ref(enc)})
                a["text"] = narrative(["Tiền sử dị ứng: chưa ghi nhận"])
                out.append(a)
            break
    return out


DOC_TYPES = {  # loại phiếu -> LOINC document type (như vnlib.DOC_TYPES, đã kiểm tra active; 18755-9/18760-9 là mã deprecated, KHÔNG dùng)
    "phieu-kham-benh-vao-vien": ("34117-2", "History and physical note"), "ket-qua-xet-nghiem-huyet-hoc": ("11502-2", "Laboratory report"),
    "ket-qua-xet-nghiem-sinh-hoa-mau": ("11502-2", "Laboratory report"), "phieu-xet-nghiem-nuoc-dich": ("11502-2", "Laboratory report"),
    "phieu-chup-cong-huong-tu": ("18748-4", "Diagnostic imaging study"), "phieu-sieu-am-doppler": ("18748-4", "Diagnostic imaging study"),
    "phieu-dien-nao-do": ("11523-8", "EEG study"), "phieu-cham-soc": ("34746-8", "Nurse Note"), "trich-bien-ban-hoi-chan": ("11488-4", "Consult note"),
    "phieu-dieu-tri": ("11506-3", "Progress note"), "ban-tom-tat-ho-so-benh-an": ("18842-5", "Discharge summary"), "phieu-cong-khai-dich-vu": ("51851-4", "Administrative note"),
    "bang-ke-chi-phi-kcb": ("51851-4", "Administrative note"), "giay-vao-vien": ("51851-4", "Administrative note"), "phieu-cls": ("11502-2", "Laboratory report"),
    "phieu-danh-gia-nguy-co-te-nga": ("34746-8", "Nurse Note"), "phieu-sang-loc-dinh-duong": ("34746-8", "Nurse Note"), "default": ("34109-9", "Note"),
}


def build_docrefs(patient, enc, hosp):
    out = {}
    for d in C["document_reference"]["pdf"]:
        if d.get("skipped"):
            continue
        t = DOC_TYPES.get(d["form_code"]) or DOC_TYPES["default"]
        dr = add({"resourceType": "DocumentReference", "id": f"docref-{slug(d['document_id'])[:60]}", "meta": {"profile": [PROF("vn-core-document-reference")]}, "status": "current",
                  "type": {"coding": [coding(SYS["loinc"], *t)], "text": d["title"]}, "category": [cc(SYS["loinc"], "11503-0", "Medical records", "Bệnh án")],
                  "subject": ref(patient, C["patient"]["ho_ten"]), "date": C["encounter"]["ra_vien_iso"] or C["encounter"]["vao_vien_iso"], "author": [ref(hosp, hosp["name"])], "custodian": ref(hosp, hosp["name"]),
                  "description": f"{d['title']} (mẫu {d['form_code']}, {d['pages']} trang)",
                  "content": [{"attachment": {"contentType": d["content_type"], "language": "vi", "url": d["storage_url"], "size": d["size_bytes"], "hash": d["hash_sha1_base64"], "title": d["file_name"], "creation": C["encounter"]["ra_vien_iso"]}, "format": coding("http://ihe.net/fhir/ihe.formatcode.fhir/CodeSystem/formatcode", "urn:ihe:iti:xds:2017:mimeTypeSufficient", "mimeType Sufficient")}],
                  "context": {"encounter": [ref(enc)], "period": {k: v for k, v in (("start", C["encounter"]["vao_vien_iso"]), ("end", C["encounter"]["ra_vien_iso"])) if v}}})
        dr["text"] = narrative([dr["description"]])
        out[d["file_name"]] = dr
    return out


# ====================================================================== Claim (bảng kê chi phí)
COST_CATS = {c["code"]: c["display"] for c in json.loads((ROOT / "vendor/vncore/package/CodeSystem-vn-cost-category-cs.json").read_text(encoding="utf-8"))["concept"]}


def build_claim(patient, enc, hosp, cov, conds, dept):
    cl = C["claim"]
    if not cl.get("item") or not cl["item"][0]:
        return None
    items = []
    seq = 1
    for grp in cl["item"][0]:
        cat = grp.get("ma_nhom_chi_phi")
        for r in grp["chi_tiet"]:
            items.append({"sequence": seq, "category": cc(CS("vn-cost-category-cs"), cat, COST_CATS.get(cat), grp.get("muc_con") or grp.get("muc")),
                          "productOrService": {"text": r["noi_dung"]}, "servicedPeriod": {"start": ddmmyyyy_to_iso(cl.get("chi_phi_tu_ngay")), "end": ddmmyyyy_to_iso(cl.get("chi_phi_den_ngay"))},
                          "quantity": {"value": r["so_luong"], "unit": r.get("don_vi_tinh")}, "unitPrice": {"value": r["don_gia_bv"], "currency": "VND"},
                          "net": {"value": r["thanh_tien_bv"], "currency": "VND"},
                          "extension": [{"url": PROF("vn-ext-claim-item-department"), "valueCodeableConcept": cc(CS("vn-department-cs"), C["location"]["ma_khoa"], DEPT_NAMES.get(C["location"]["ma_khoa"]))}],
                          "detail": [{"sequence": 1, "productOrService": {"text": "Quỹ BHYT thanh toán"}, "net": {"value": r["quy_bhyt"], "currency": "VND"}},
                                     {"sequence": 2, "productOrService": {"text": "Người bệnh cùng chi trả"}, "net": {"value": r["nguoi_benh_cung_chi_tra"], "currency": "VND"}},
                                     {"sequence": 3, "productOrService": {"text": "Người bệnh tự trả"}, "net": {"value": r["nguoi_benh_tu_tra"], "currency": "VND"}}]})
            seq += 1
    claim = add({"resourceType": "Claim", "id": f"claim-{slug(C['encounter'].get('so_kham_benh') or 'x')}", "meta": {"profile": [PROF("vn-core-claim")]},
                 "identifier": [{"type": cc(CS("vn-identifier-type-cs"), "MA_LK", "Mã liên kết hồ sơ BHYT"), "system": SID("ma-lk"), "value": C["encounter"].get("so_kham_benh"), "assigner": ref(hosp, hosp["name"])}],
                 "status": "active", "type": cc(TH + "claim-type", "institutional", "Institutional"), "use": "claim", "patient": ref(patient, C["patient"]["ho_ten"]),
                 "billablePeriod": {"start": ddmmyyyy_to_iso(cl.get("chi_phi_tu_ngay")), "end": ddmmyyyy_to_iso(cl.get("chi_phi_den_ngay"))}, "created": vn_datetime(cl.get("ngay_lap")) or C["encounter"]["ra_vien_iso"],
                 "provider": ref(hosp, hosp["name"]), "priority": cc(TH + "processpriority", "normal", "Normal"),
                 # vn-claim-diagnosis-has-icd10: diagnosis phải là CodeableConcept có coding vn-icd10-cs (không dùng diagnosisReference)
                 "diagnosis": [{"sequence": i, "diagnosisCodeableConcept": cnd["code"], "type": [cc(TH + "ex-diagnosistype", "principal" if i == 1 else "comorbidity")]} for i, (d, cnd) in enumerate([x for x in conds if x[0]["type"] == "DD"], 1)],
                 "insurance": [{"sequence": 1, "focal": True, "coverage": ref(cov)}] if cov else [{"sequence": 1, "focal": True, "coverage": {"display": "Tự chi trả"}}],
                 "item": items, "total": {"value": int(cl["tong_chi_phi"]["gia_tri"]), "currency": "VND"} if cl["tong_chi_phi"].get("gia_tri") else None})
    claim["text"] = narrative([f"Bảng kê chi phí: tổng {cl['tong_chi_phi'].get('gia_tri')} VND; BHYT {cl['quy_bhyt_thanh_toan'].get('gia_tri')}; cùng chi trả {cl['nguoi_benh_tra']['cung_tra'].get('gia_tri')}; tự trả {cl['nguoi_benh_tra']['khoan_khac'].get('gia_tri')}"] + [f"{it['productOrService']['text']}: {it['net']['value']}" for it in items])
    return claim


# ====================================================================== Composition (tài liệu bệnh án)
def section(title, loinc, display, entries=None, lines=None):
    s = {"title": title, "code": cc(SYS["loinc"], loinc, display), "text": narrative(lines or ["Xem các mục tham chiếu."])}
    if entries:
        s["entry"] = [ref(e) for e in entries if e]
    return s


def build_composition(patient, enc, hosp, author, conds, vitals, lab_drs, lab_obs, img_drs, meds, srs, ci, hist, claim):
    ctx = C["document_context"]; dis = C["discharge"]; cf = C["clinical_finding"]
    date = C["encounter"]["ra_vien_iso"] or C["encounter"]["vao_vien_iso"]
    comp = {"resourceType": "Composition", "id": f"comp-{slug(ctx.get('ma_benh_an') or C['patient']['ma_benh_nhan'])}", "meta": {"profile": [PROF("vn-core-composition")]},
            "identifier": {"system": SID("document-number"), "value": ctx.get("ma_benh_an") or C["patient"]["ma_benh_nhan"], "assigner": ref(hosp, hosp["name"])},
            "status": "final", "type": {"coding": [coding(SYS["loinc"], "18842-5", "Discharge summary")], "text": "Bệnh án nội trú / Tóm tắt hồ sơ bệnh án"},
            "category": [cc(SYS["loinc"], "11503-0", "Medical records")], "subject": ref(patient, C["patient"]["ho_ten"]), "encounter": ref(enc), "date": date,
            "author": [ref(author, pdisp(author))], "title": f"{ctx.get('ten_bieu_mau')} - {C['patient']['ho_ten']} - {ctx.get('ma_benh_an')}", "confidentiality": "R",
            "attester": [{"mode": "professional", "time": vn_datetime(ctx.get("ngay_ky_tom_tat")) or date, "party": ref(author, pdisp(author))}],
            "custodian": ref(hosp, hosp["name"]),
            "event": [{"period": enc["period"]}],
            "section": [
                section("Lý do vào viện", "10154-3", "Chief complaint Narrative - Reported", lines=[C["encounter"].get("ly_do_vao_vien")]),
                section("Bệnh sử và tiền sử", "11348-0", "History of Past illness Narrative", entries=hist, lines=[f"Quá trình bệnh lý: {C['history_of_present_illness'].get('qua_trinh_benh_ly')}", f"Tiền sử bản thân: {C['medical_history'].get('tien_su_ban_than')}", f"Tiền sử gia đình: {C['family_histories'].get('tien_su_gia_dinh')}"]),
                section("Khám bệnh", "29545-1", "Physical findings Narrative", entries=vitals, lines=[f"Toàn thân: {cf.get('kham_toan_than')}"] + [f"{k}: {v}" for k, v in cf.items() if v and k not in ("kham_toan_than", "trieu_chung_co_nang", "kham_ngoai_khoa")] + [f"Triệu chứng: {'; '.join(cf.get('trieu_chung_co_nang') or [])}"]),
                section("Chẩn đoán", "29548-5", "Diagnosis Narrative", entries=[c for _, c in conds] + ([ci] if ci else []), lines=[f"{d['type']} {d.get('rank')}: {d['text']} ({d['code']} - {cnd['code']['coding'][0].get('display', '')})" for d, cnd in conds]),
                section("Kết quả xét nghiệm", "30954-2", "Relevant diagnostic tests/laboratory data Narrative", entries=lab_drs + lab_obs, lines=[f"{dr['code']['text']}: {dr.get('conclusion') or len(dr.get('result', []))} chỉ số" for dr in lab_drs]),
                section("Chẩn đoán hình ảnh / Thăm dò chức năng", "18748-4", "Diagnostic imaging study", entries=img_drs, lines=[f"{dr['code']['text']}: {dr.get('conclusion')}" for dr in img_drs]),
                section("Quá trình điều trị", "8648-8", "Hospital course Narrative", entries=srs, lines=[ctx.get("tom_tat_benh_ly"), f"Phương pháp điều trị: {ctx.get('phuong_phap_dieu_tri')}"]),
                section("Thuốc điều trị", "10160-0", "History of Medication use Narrative", entries=meds, lines=[m["medicationCodeableConcept"]["text"] + " - " + m["dosageInstruction"][0]["text"] for m in meds]),
                section("Thủ thuật / phẫu thuật", "47519-4", "History of Procedures Document", lines=["Không có"]),
                section("Tình trạng ra viện và hướng điều trị", "18842-5", "Discharge summary", lines=[f"Tình trạng ra viện: {dis.get('ket_qua_dieu_tri')} (mã kết quả điều trị {dis.get('ma_ket_qua_dieu_tri')})", f"Hình thức ra viện: {dis.get('ten_hinh_thuc_ra_vien')}", f"Hướng điều trị: {dis.get('huong_dieu_tri')}"]),
            ]}
    if claim:
        comp["section"].append(section("Chi phí khám chữa bệnh", "51851-4", "Administrative note", entries=[claim], lines=[f"Tổng chi phí {C['claim']['tong_chi_phi'].get('gia_tri')} VND; quỹ BHYT {C['claim']['quy_bhyt_thanh_toan'].get('gia_tri')} VND"]))
    comp["text"] = narrative([comp["title"], f"Người bệnh: {C['patient']['ho_ten']}; Khoa: {C['location']['khoa']}; {C['encounter'].get('vao_vien')} -> {C['encounter'].get('ra_vien')}"])
    return add(comp)


# ====================================================================== Bundle
def strip_none(o):
    if isinstance(o, dict):
        return {k: strip_none(v) for k, v in o.items() if v is not None and v != [] and v != {}}
    if isinstance(o, list):
        return [strip_none(x) for x in o if x is not None]
    return o


def bundle(resources, btype, bid):
    ts = C["encounter"]["ra_vien_iso"] or "2026-01-01T00:00:00+07:00"
    b = {"resourceType": "Bundle", "id": bid, "meta": {"profile": [PROF("vn-core-bundle")]},
         "identifier": {"system": "urn:ietf:rfc:3986", "value": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, bid + CASE)}"}, "type": btype, "timestamp": ts, "entry": []}
    for r in resources:
        e = {"fullUrl": f"{FACILITY['base_url']}/{r['resourceType']}/{r['id']}", "resource": r}
        if btype == "transaction":
            e["request"] = {"method": "PUT", "url": f"{r['resourceType']}/{r['id']}"}
        b["entry"].append(e)
    return b


def main():
    syt, hosp, dept, ward, bed = build_organizations()
    patient = build_patient(hosp)
    cov = build_coverage(patient)
    pr = C["practitioner"]
    bs_dt = practitioner(pr.get("bac_si_dieu_tri"), hosp, dept, "Bác sĩ điều trị")
    bs_kb = practitioner(pr.get("bac_si_kham_benh"), hosp, dept, "Bác sĩ khám bệnh")
    truong_khoa = practitioner(pr.get("truong_khoa"), hosp, dept, "Trưởng khoa")
    enc_id = f"enc-{slug(C['encounter'].get('so_kham_benh') or C['encounter'].get('so_vao_vien') or C['patient']['ma_benh_nhan'])}"
    conds = build_conditions(patient, enc_id, bs_dt)
    enc = build_encounter(patient, hosp, dept, ward, bed, conds, [("ATND", "attender", bs_dt), ("ADM", "admitter", bs_kb), ("CON", "consultant", truong_khoa)])
    docrefs = build_docrefs(patient, enc, hosp)
    srs_by_name = build_service_requests(patient, enc, conds)
    vitals = build_vitals(patient, enc)
    lab_drs, lab_obs = build_labs(patient, enc, hosp, dept, srs_by_name, docrefs)
    img_drs = build_imaging(patient, enc, hosp, srs_by_name, docrefs)
    meds = build_medications(patient, enc, conds)
    ci = build_clinical_impression(patient, enc, conds)
    hist = build_histories(patient, enc)
    claim = build_claim(patient, enc, hosp, cov, conds, dept)
    srs = [r for r in RES if r["resourceType"] == "ServiceRequest"]
    comp = build_composition(patient, enc, hosp, bs_dt, conds, vitals, lab_drs, lab_obs, img_drs, meds, srs, ci, hist, claim)

    resources = [strip_none(r) for r in RES]
    import shutil
    shutil.rmtree(FHIR / "resources", ignore_errors=True)  # xoá kết quả lần trước (tránh resource mồ côi)
    (FHIR / "resources").mkdir(parents=True, exist_ok=True)
    for r in resources:
        dump(r, FHIR / "resources" / f"{r['resourceType']}-{r['id']}.json")
    # document bundle: Composition đầu tiên, sau đó mọi resource được tham chiếu
    doc_order = [comp] + [r for r in resources if r is not comp and r["resourceType"] != "Claim"] + ([c for c in resources if c["resourceType"] == "Claim"])
    comp_first = [r for r in resources if r["resourceType"] == "Composition"] + [r for r in resources if r["resourceType"] != "Composition"]
    dump(bundle(comp_first, "document", f"bundle-doc-{CASE}"), FHIR / "bundle-document.json")
    dump(bundle([r for r in resources], "transaction", f"bundle-tx-{CASE}"), FHIR / "bundle-transaction.json")
    from collections import Counter
    print("resources:", dict(Counter(r["resourceType"] for r in resources)))
    print("->", FHIR)


if __name__ == "__main__":
    main()
