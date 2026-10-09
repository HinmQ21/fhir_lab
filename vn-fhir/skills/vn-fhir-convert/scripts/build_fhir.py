"""build_fhir.py — sinh FHIR R4 (VN Core IG 0.10.0) từ clinical JSON v2 (xem references/clinical-schema.md).

Cách dùng:
    python build_fhir.py <clinical.json> <out_dir> [--vendor <dir>]

Đầu ra:
    <out_dir>/resources/<Type>-<id>.json          mọi resource, mỗi file một resource (có meta.profile + narrative)
    <out_dir>/bundle-document.json                Bundle document: Composition tóm tắt bệnh án + bao đóng tham chiếu + Provenance
    <out_dir>/bundle-cost-statement.json          Bundle bàn giao bảng kê (vn-core-cost-statement-document-bundle) nếu có cost_statement
    <out_dir>/bundle-transaction.json             Bundle transaction PUT mọi resource
    <out_dir>/build_report.json                   thống kê + id được sinh ra

Nguyên tắc: builder chỉ chuyển đổi, KHÔNG suy luận mã. Mọi mã (ICD-10, LOINC, CLS, ATC...) phải có sẵn trong clinical JSON
(code.codings[]), đã qua term_lookup / người duyệt. Mọi resource có `sources` được gom vào một Provenance theo từng tài liệu gốc.
"""
from __future__ import annotations

import argparse
import html
import json
import sys
import uuid
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vnlib as V  # noqa: E402
from vnlib import CS, PROF, SID, SYS, VN, system_url  # noqa: E402

TH = "http://terminology.hl7.org/CodeSystem/"
LIST_TYPES = [  # (khoá trong clinical JSON, resourceType)
    ("documents", "DocumentReference"), ("organizations", "Organization"), ("locations", "Location"), ("practitioners", "Practitioner"),
    ("conditions", "Condition"), ("observations", "Observation"), ("specimens", "Specimen"), ("service_requests", "ServiceRequest"),
    ("diagnostic_reports", "DiagnosticReport"), ("medications", "Medication"), ("medication_requests", "MedicationRequest"),
    ("medication_administrations", "MedicationAdministration"), ("allergies", "AllergyIntolerance"), ("family_history", "FamilyMemberHistory"),
    ("clinical_impressions", "ClinicalImpression"), ("care_plans", "CarePlan"), ("related_persons", "RelatedPerson"), ("procedures", "Procedure"),
]
IDENT_SYSTEM = {  # loại định danh -> system (None = namespace của cơ sở)
    "CCCD": SID("cccd"), "CMND": SID("cmnd"), "BHYT": SID("bhyt"), "BHXH": SID("bhxh"), "MA_LK": SID("ma-lk"), "MA_LUOT_KCB": SID("ma-luot-kcb"),
    "CSKCB": SID("ma-cskcb"), "CSKCB13": SID("ma-gtin-cskcb"), "GPHN": SID("gphn"), "CCHN": SID("cchn"), "MRN": None, "HC": None, "GKS": SID("gks"),
}
LOCAL_SYSTEM = {"Organization": SID("organization-local-id"), "Location": SID("location-local-id"), "Specimen": SID("specimen-local-id"),
                "DocumentReference": SID("document-number")}
ENC_EXT = {"referral_mode": ("vn-ext-referral-mode", "vn-referral-mode-cs"), "insurance_visit_type": ("vn-ext-insurance-visit-type", "vn-insurance-visit-type-cs"),
           "treatment_outcome": ("vn-ext-treatment-outcome", "vn-treatment-outcome-cs"), "payment_method": ("vn-ext-payment-method", "vn-payment-method-cs")}
ORG_EXT = {"org_type": ("vn-ext-org-type", "vn-organization-type-cs"), "org_level": ("vn-ext-org-level", "vn-healthcare-level-cs"),
           "facility_care_level": ("vn-ext-facility-care-level", "vn-facility-care-level-cs"), "rank_status": ("vn-ext-org-rank-status", "vn-organization-rank-status-cs")}
INTERP = {"H": "High", "L": "Low", "N": "Normal", "A": "Abnormal", "HH": "Critical high", "LL": "Critical low", "POS": "Positive", "NEG": "Negative"}
DAR = {"not-performed": "Not Performed", "unknown": "Unknown", "not-asked": "Not Asked", "masked": "Masked", "error": "Error", "not-applicable": "Not Applicable"}
PARTICIPANT_TYPE = {"ATND": "attender", "ADM": "admitter", "DIS": "discharger", "CON": "consultant", "PPRF": "primary performer", "SPRF": "secondary performer", "REF": "referrer"}
DIAG_USE = {"AD": "AD", "DD": "DD", "CC": "CC", "CM": "CM", "pre-op": "pre-op", "post-op": "post-op", "billing": "billing"}
SPECIMEN_V2 = {"BLD": "Whole blood", "SER": "Serum", "PLAS": "Plasma", "UR": "Urine", "CSF": "Cerebral spinal fluid", "SPT": "Sputum", "STL": "Stool", "SWAB": "Swab"}


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def money(v) -> dict | None:
    return None if v is None else {"value": v, "currency": "VND"}


def strip_none(o):
    if isinstance(o, dict):
        return {k: strip_none(v) for k, v in o.items() if v is not None and v != [] and v != {}}
    if isinstance(o, list):
        return [strip_none(x) for x in o if x is not None]
    return o


class BuildError(Exception):
    pass


class Builder:
    def __init__(self, clin: dict):
        self.c = clin
        self.meta = clin["meta"]
        self.fac = self.meta["facility"]
        self.base = self.fac["base_url"].rstrip("/")
        self.fac_org = self.fac["organization"]
        self.res: dict[str, dict] = {}        # "Type/id" -> resource
        self.order: list[str] = []
        self.registry: dict[str, str] = {}    # id -> resourceType
        self.sources: dict[str, set[str]] = defaultdict(set)   # doc id -> {Type/id}
        self.unsourced: list[str] = []
        self.warnings: list[str] = []
        self._register()

    # ------------------------------------------------------------------ đăng ký id -> resourceType (cho tham chiếu xuôi)
    def _register(self):
        for key, rt in LIST_TYPES:
            for it in self.c.get(key, []) or []:
                self.registry[it["id"]] = rt
        for p in self.c.get("practitioners", []) or []:
            for i, r in enumerate(p.get("roles", []) or []):
                r.setdefault("id", f"{p['id']}-role-{i + 1}")
                self.registry[r["id"]] = "PractitionerRole"
        if self.c.get("patient"):
            self.registry[self.c["patient"]["id"]] = "Patient"
        if self.c.get("coverage"):
            self.registry[self.c["coverage"]["id"]] = "Coverage"
        if self.c.get("encounter"):
            self.registry[self.c["encounter"]["id"]] = "Encounter"
        cs = self.c.get("cost_statement")
        if cs:
            ids = cs.setdefault("ids", {})
            ml = V.slug(cs["ma_lk"])
            for k, rt in (("claim", "Claim"), ("invoice", "Invoice"), ("account", "Account"), ("composition", "Composition")):
                ids.setdefault(k, f"{k}-{ml}")
                self.registry[ids[k]] = rt
        for key, comp in (self.c.get("compositions") or {}).items():
            comp.setdefault("id", f"composition-{V.slug(key)}")
            self.registry[comp["id"]] = "Composition"

    # ------------------------------------------------------------------ helpers
    def ref(self, id_: str | None, display: str | None = None) -> dict | None:
        if not id_:
            return None
        if "/" in id_:
            r = {"reference": id_}
        else:
            rt = self.registry.get(id_)
            if not rt:
                raise BuildError(f"tham chiếu tới id không tồn tại trong clinical JSON: {id_}")
            r = {"reference": f"{rt}/{id_}"}
        d = display or self.display_of(id_)
        if d:
            r["display"] = d
        return r

    def display_of(self, id_: str) -> str | None:
        rt = self.registry.get(id_)
        if rt == "Organization":
            return next((o["name"] for o in self.c["organizations"] if o["id"] == id_), None)
        if rt == "Practitioner":
            return next((p["name"]["text"] for p in self.c["practitioners"] if p["id"] == id_), None)
        if rt == "PractitionerRole":
            for p in self.c["practitioners"]:
                for r in p.get("roles", []):
                    if r["id"] == id_:
                        return f"{p['name']['text']} — {r.get('title_text') or ''}".strip(" —")
        if rt == "Patient":
            return self.c["patient"]["name"]["text"]
        if rt == "Location":
            return next((l["name"] for l in self.c["locations"] if l["id"] == id_), None)
        return None

    def refs(self, ids) -> list[dict]:
        return [self.ref(i) for i in (ids or []) if i]

    @staticmethod
    def coding(c: dict) -> dict:
        out = {"system": system_url(c["system"]), "code": str(c["code"])}
        if c.get("display"):
            out["display"] = c["display"]
        return out

    def cc(self, obj, default_text: str | None = None) -> dict | None:
        """{text, codings[]} | str -> CodeableConcept. Coding có verified=false vẫn được xuất nhưng ghi cảnh báo."""
        if obj is None:
            return None
        if isinstance(obj, str):
            return {"text": obj}
        out: dict = {}
        cods = []
        for c in obj.get("codings", []) or []:
            if c.get("verified") is False:
                self.warnings.append(f"coding chưa xác minh: {c.get('system')}|{c.get('code')} ({obj.get('text')})")
            cods.append(self.coding(c))
        if cods:
            out["coding"] = cods
        if obj.get("text") or default_text:
            out["text"] = obj.get("text") or default_text
        return out or None

    def cs_cc(self, cs_id: str, code: str | None, text: str | None = None) -> dict | None:
        """Mã trong CodeSystem VN Core -> CodeableConcept với display tra từ vendor."""
        if not code:
            return None
        disp = V.cs_display(cs_id, str(code))
        if disp is None:
            self.warnings.append(f"mã '{code}' không có trong {cs_id}")
        out = {"coding": [strip_none({"system": CS(cs_id), "code": str(code), "display": disp})]}
        if text:
            out["text"] = text
        return out

    def ext(self, name: str, value_key: str, value) -> dict | None:
        if value is None:
            return None
        return {"url": PROF(name) if not name.startswith("http") else name, value_key: value}

    def identifier(self, ident: dict, owner_type: str) -> dict:
        typ = ident.get("type")
        out: dict = {}
        if ident.get("use"):
            out["use"] = ident["use"]
        if typ and V.cs_display("vn-identifier-type-cs", typ):
            out["type"] = {"coding": [{"system": CS("vn-identifier-type-cs"), "code": typ, "display": V.cs_display("vn-identifier-type-cs", typ)}]}
        elif typ == "LOCAL" and owner_type == "Organization" and ident.get("text"):
            out["type"] = {"text": ident["text"]}
        elif ident.get("text") or typ:
            out["type"] = {"text": ident.get("text") or typ}
        system = ident.get("system")
        if not system:
            if typ == "MRN":
                system = self.fac.get("mrn_system") or SID("mrn")
            elif typ == "LOCAL":
                system = LOCAL_SYSTEM.get(owner_type, self.local_sid("local-id"))
            elif typ in IDENT_SYSTEM and IDENT_SYSTEM[typ]:
                system = IDENT_SYSTEM[typ]
            else:
                system = self.local_sid(V.slug(typ or "id"))
        out["system"] = system
        out["value"] = str(ident["value"])
        if ident.get("period"):
            out["period"] = ident["period"]
        assigner = ident.get("assigner")
        if assigner is None and (system.startswith(f"{VN}/sid/") and typ not in ("CCCD", "CMND", "BHYT", "BHXH", "CSKCB", "CSKCB13", "GPHN", "CCHN")):
            assigner = self.fac_org  # URI dùng tạm cấp quốc gia -> bắt buộc assigner
        if assigner:
            out["assigner"] = self.ref(assigner) if assigner in self.registry else {"display": assigner}
        return out

    def address(self, a: dict | None) -> dict | None:
        if not a:
            return None
        out: dict = {"use": a.get("use", "home"), "type": a.get("type", "physical")}
        exts = []
        if a.get("province_code"):
            exts.append({"url": PROF("vn-ext-province"), "valueCoding": strip_none({"system": CS("vn-province-cs"), "code": a["province_code"], "display": V.cs_display("vn-province-cs", a["province_code"]) or a.get("state")})})
        if a.get("ward_code"):
            exts.append({"url": PROF("vn-ext-ward"), "valueCoding": strip_none({"system": CS("vn-ward-cs"), "code": a["ward_code"], "display": V.cs_display("vn-ward-cs", a["ward_code"]) or a.get("ward")})})
        if exts:
            out["extension"] = exts
        out.update({"text": a.get("text"), "line": a.get("line"), "city": a.get("city") or a.get("ward"), "district": a.get("district"), "state": a.get("state"), "country": a.get("country", "VN")})
        return strip_none(out)

    def quantity(self, q: dict) -> dict:
        code = q.get("ucum") or V.ucum(q.get("unit_raw"))[0]
        unit = q.get("unit") or V.ucum(q.get("unit_raw"))[1] or q.get("unit_raw")
        out = {"value": q["value"], "unit": unit, "system": SYS["ucum"], "code": code}
        if q.get("comparator"):
            out["comparator"] = q["comparator"]
        if not code:
            self.warnings.append(f"không có mã UCUM cho đơn vị '{q.get('unit_raw')}'")
            out.pop("system"); out.pop("code")
        return strip_none(out)

    def value_of(self, v: dict | None, prefix: str = "value") -> dict:
        """{'quantity':{...}} | {'string':..} | {'integer':..} | {'boolean':..} | {'codeable':{...}} | {'absent':'not-performed'} -> phần tử FHIR."""
        if not v:
            return {}
        if "quantity" in v:
            return {f"{prefix}Quantity": self.quantity(v["quantity"])}
        if "string" in v:
            return {f"{prefix}String": v["string"]}
        if "integer" in v:
            return {f"{prefix}Integer": v["integer"]}
        if "boolean" in v:
            return {f"{prefix}Boolean": v["boolean"]}
        if "datetime" in v:
            return {f"{prefix}DateTime": v["datetime"]}
        if "codeable" in v:
            return {f"{prefix}CodeableConcept": self.cc(v["codeable"])}
        if "absent" in v:
            return {"dataAbsentReason": {"coding": [{"system": TH + "data-absent-reason", "code": v["absent"], "display": DAR.get(v["absent"], v["absent"])}]}}
        raise BuildError(f"value không hợp lệ: {v}")

    def narrative(self, title: str, lines: list) -> dict:
        body = "".join(f"<p>{esc(x)}</p>" for x in lines if x)
        return {"status": "generated", "div": f'<div xmlns="http://www.w3.org/1999/xhtml"><p><b>{esc(title)}</b></p>{body}</div>'}

    def add(self, res: dict, profile: str | list | None, title: str, lines: list, sources=None):
        res = strip_none(res)
        if profile:
            res.setdefault("meta", {})["profile"] = [PROF(p) if not p.startswith("http") else p for p in (profile if isinstance(profile, list) else [profile])]
        res["text"] = self.narrative(title, lines)
        # thứ tự khoá: resourceType, id, meta, text, ...
        ordered = {k: res[k] for k in ("resourceType", "id", "meta", "text") if k in res}
        ordered.update({k: v for k, v in res.items() if k not in ordered})
        key = f"{res['resourceType']}/{res['id']}"
        if key in self.res:
            raise BuildError(f"trùng id: {key}")
        self.res[key] = ordered
        self.order.append(key)
        if sources:
            for s in sources:
                self.sources[s].add(key)
        elif res["resourceType"] not in ("Provenance", "Bundle"):
            self.unsourced.append(key)
        return ordered

    # ------------------------------------------------------------------ build
    def build(self) -> dict:
        self.organizations(); self.locations(); self.practitioners(); self.patient(); self.coverage(); self.encounter()
        self.conditions(); self.specimens(); self.service_requests(); self.observations(); self.diagnostic_reports()
        self.medications(); self.medication_requests(); self.medication_administrations(); self.allergies(); self.family_history()
        self.clinical_impressions(); self.care_plans(); self.documents(); self.cost_statement(); self.compositions(); self.provenances()
        return self.res

    # ---- Organization
    def organizations(self):
        for o in self.c.get("organizations", []) or []:
            dept = o.get("kind") == "department"
            idents = [self.identifier(i, "Organization") for i in o.get("identifiers", []) or []]
            if dept and o.get("local_code"):
                idents.append({"type": {"text": "Mã khoa cục bộ"}, "system": SID("organization-local-id"), "value": o["local_code"], "assigner": self.ref(o.get("part_of") or self.fac_org)})
            r: dict = {"resourceType": "Organization", "id": o["id"], "identifier": idents or None, "active": o.get("active", True), "name": o["name"],
                       "alias": o.get("aliases") or None, "telecom": o.get("telecom") or None, "address": [self.address(o["address"])] if o.get("address") else None,
                       "partOf": self.ref(o.get("part_of"))}
            if dept:
                r["type"] = [{"coding": [{"system": TH + "organization-type", "code": "dept", "display": "Hospital Department"}]},
                             self.cs_cc("vn-department-cs", o["department_code"], o.get("department_text")) if o.get("department_code") else {"coding": [{"system": CS("vn-department-cs"), "code": "K99"}], "text": o["name"]}]
                if not o.get("part_of"):
                    raise BuildError(f"department {o['id']} thiếu part_of")
            else:
                r["extension"] = [self.ext(u, "valueCodeableConcept", self.cs_cc(cs, o.get(k))) for k, (u, cs) in ORG_EXT.items() if o.get(k)] or None
                if o.get("org_type"):
                    r["type"] = [{"coding": [{"system": TH + "organization-type", "code": "prov", "display": "Healthcare Provider"}]}]
            self.add(r, "vn-core-organization-department" if dept else "vn-core-organization", o["name"], [o.get("kind"), (o.get("address") or {}).get("text")], o.get("sources"))

    # ---- Location
    def locations(self):
        for l in self.c.get("locations", []) or []:
            pt = {"ward": ("wa", "Ward"), "room": ("ro", "Room"), "bed": ("bd", "Bed"), "clinic": ("ro", "Room"), "building": ("bu", "Building"), "area": ("area", "Area")}.get(l.get("kind"), (None, None))
            r = {"resourceType": "Location", "id": l["id"], "status": l.get("status", "active"), "name": l["name"],
                 "identifier": [{"system": SID("location-local-id"), "value": l["local_code"], "assigner": self.ref(l.get("managing_organization") or self.fac_org)}] if l.get("local_code") else None,
                 "mode": "instance", "physicalType": {"coding": [{"system": TH + "location-physical-type", "code": pt[0], "display": pt[1]}]} if pt[0] else None,
                 "managingOrganization": self.ref(l.get("managing_organization")), "partOf": self.ref(l.get("part_of"))}
            self.add(r, "vn-core-location", l["name"], [l.get("kind")], l.get("sources"))

    # ---- Practitioner + PractitionerRole
    def practitioners(self):
        for p in self.c.get("practitioners", []) or []:
            name = p["name"]
            idents = [self.identifier(i, "Practitioner") for i in p.get("identifiers", []) or []]
            if not idents:  # profile yêu cầu identifier 1..* -> mã nhân viên nội bộ suy từ tên (ghi rõ trong review)
                idents = [{"type": {"text": "Mã nhân viên nội bộ (tạm, suy từ họ tên)"}, "system": self.fac.get("staff_system") or self.local_sid("staff-id"), "value": V.slug(name["text"])}]
                self.warnings.append(f"practitioner {p['id']} không có mã nhân viên -> dùng slug tên làm định danh tạm")
            quals = []
            for q in p.get("qualifications", []) or []:
                code = q if isinstance(q, str) else q["code"]
                quals.append({"code": {"coding": [{"system": CS("vn-qualification-cs"), "code": code, "display": V.cs_display("vn-qualification-cs", code)}], "text": q.get("text") if isinstance(q, dict) else None}})
            r = {"resourceType": "Practitioner", "id": p["id"], "identifier": idents, "active": True,
                 "name": [{"use": "official", "text": name["text"], "family": name.get("family"), "given": name.get("given")}], "gender": p.get("gender"), "qualification": quals or None}
            self.add(r, "vn-core-practitioner", name["text"], [p.get("name_raw")], p.get("sources"))
            for role in p.get("roles", []) or []:
                rr = {"resourceType": "PractitionerRole", "id": role["id"], "active": True, "practitioner": self.ref(p["id"]), "organization": self.ref(role.get("organization")),
                      "code": [self.cs_cc("vn-practitioner-title-cs", role["title_code"], role.get("title_text"))] if role.get("title_code") else ([{"text": role["title_text"]}] if role.get("title_text") else None),
                      "specialty": [self.cs_cc("vn-specialty-cs", s) for s in role.get("specialty", []) or []] or None,
                      "location": [self.ref(role["location"])] if role.get("location") else None, "period": role.get("period")}
                self.add(rr, "vn-core-practitioner-role", f"{name['text']} — {role.get('title_text') or ''}", [self.display_of(role.get("organization")) if role.get("organization") else None], p.get("sources"))

    # ---- Patient
    def patient(self):
        p = self.c.get("patient")
        if not p:
            return
        exts = []
        if p.get("ethnicity"):
            exts.append(self.ext("vn-ext-ethnicity", "valueCodeableConcept", self.cs_cc("vn-ethnicity-cs", p["ethnicity"]["code"], p["ethnicity"].get("text"))))
        if p.get("occupation"):
            oc = p["occupation"]
            exts.append(self.ext("vn-ext-occupation", "valueCodeableConcept", self.cs_cc("vn-occupation-cs", oc["code"], oc.get("text")) if oc.get("code") else {"text": oc["text"]}))
        if p.get("nationality"):
            n = p["nationality"]
            exts.append({"url": "http://hl7.org/fhir/StructureDefinition/patient-citizenship", "extension": [{"url": "code", "valueCodeableConcept": {"coding": [{"system": "urn:iso:std:iso:3166", "code": n["code"], "display": n.get("display")}], "text": n.get("text")}}]})
        contacts = []
        for ct in p.get("contacts", []) or []:
            contacts.append({"relationship": [{"text": ct.get("relationship_text")}] if ct.get("relationship_text") else None, "name": {"text": ct["name"]} if ct.get("name") else None, "telecom": ct.get("telecom"), "address": self.address(ct.get("address"))})
        r = {"resourceType": "Patient", "id": p["id"], "extension": exts or None, "identifier": [self.identifier(i, "Patient") for i in p["identifiers"]], "active": True,
             "name": [{"use": "official", "text": p["name"]["text"], "family": p["name"].get("family"), "given": p["name"].get("given")}], "telecom": p.get("telecom"),
             "gender": p["gender"], "birthDate": p["birth_date"], "address": [self.address(p["address"])] if p.get("address") else None, "contact": contacts or None,
             "managingOrganization": self.ref(p.get("managing_organization") or self.fac_org)}
        if p.get("birth_date_estimated"):
            r["_birthDate"] = {"extension": [{"url": "http://hl7.org/fhir/StructureDefinition/data-absent-reason", "valueCode": "unknown"}]} if not p.get("birth_date") else None
        self.add(r, "vn-core-patient", p["name"]["text"], [f"Giới tính: {p['gender']}", f"Ngày sinh: {p['birth_date']}", (p.get("address") or {}).get("text")], p.get("sources"))

    # ---- Coverage
    def coverage(self):
        cv = self.c.get("coverage")
        if not cv:
            return
        pt = self.c["patient"]["id"]
        exts = []
        if cv.get("card_type"):
            exts.append(self.ext("vn-ext-bhyt-card-type", "valueCodeableConcept", self.cs_cc("vn-bhyt-card-type-cs", cv["card_type"]["code"], cv["card_type"].get("text"))))
        if cv.get("primary_care_facility"):
            exts.append(self.ext("vn-ext-primary-care-facility", "valueReference", self.ref(cv["primary_care_facility"])))
        payor = cv.get("payor")
        r = {"resourceType": "Coverage", "id": cv["id"], "extension": exts or None,
             "identifier": [{"type": {"coding": [{"system": CS("vn-identifier-type-cs"), "code": "BHYT", "display": V.cs_display("vn-identifier-type-cs", "BHYT")}]}, "system": SID("bhyt"), "value": cv["bhyt_number"]}],
             "status": cv.get("status", "active"), "type": {"coding": [{"system": TH + "insurance-coverage-type", "code": "medical", "display": "Medical"}], "text": "Bảo hiểm y tế"},
             "subscriber": self.ref(pt), "subscriberId": cv["bhyt_number"], "beneficiary": self.ref(pt),
             "relationship": {"coding": [{"system": TH + "subscriber-relationship", "code": "self", "display": "Self"}]}, "period": cv.get("period"),
             "payor": [self.ref(payor) if payor in self.registry else {"display": payor or "Bảo hiểm xã hội Việt Nam"}]}
        if cv.get("benefit_level") is not None:
            r["costToBeneficiary"] = [{"type": {"coding": [{"system": TH + "coverage-copay-type", "code": "copaypct", "display": "Copay Percentage"}], "text": "Mức hưởng BHYT"},
                                       "valueQuantity": {"value": cv["benefit_level"], "unit": "%", "system": SYS["ucum"], "code": "%"}}]
        self.add(r, "vn-core-coverage", f"Thẻ BHYT {cv['bhyt_number']}", [f"Mức hưởng {cv.get('benefit_level')}%", json.dumps(cv.get("period"), ensure_ascii=False)], cv.get("sources"))

    # ---- Encounter
    def encounter(self):
        e = self.c.get("encounter")
        if not e:
            return
        exts = [self.ext(u, "valueCodeableConcept", self.cs_cc(cs, e.get(k))) for k, (u, cs) in ENC_EXT.items() if e.get(k)]
        cls = e.get("class", "IMP")
        r = {"resourceType": "Encounter", "id": e["id"], "extension": exts or None, "identifier": [self.identifier(i, "Encounter") for i in e.get("identifiers", []) or []],
             "status": e.get("status", "finished"),
             "class": {"system": TH + "v3-ActCode", "code": cls, "display": {"IMP": "inpatient encounter", "AMB": "ambulatory", "EMER": "emergency", "VR": "virtual"}.get(cls)},
             "type": [self.cs_cc("vn-encounter-type-cs", e["type"]["code"], e["type"].get("text"))] if e.get("type") else None,
             "priority": e.get("priority"), "subject": self.ref(self.c["patient"]["id"]),
             "participant": [{"type": [{"coding": [{"system": TH + "v3-ParticipationType", "code": pp["type"], "display": PARTICIPANT_TYPE.get(pp["type"])}]}] if pp.get("type") else None,
                              "period": pp.get("period"), "individual": self.ref(pp["individual"])} for pp in e.get("participants", []) or []] or None,
             "period": e.get("period"), "length": {"value": e["length_days"], "unit": "ngày", "system": SYS["ucum"], "code": "d"} if e.get("length_days") is not None else None,
             "reasonCode": [{"text": e["reason_text"]}] if e.get("reason_text") else None,
             "diagnosis": [{"condition": self.ref(d["condition"]), "use": {"coding": [{"system": TH + "diagnosis-role", "code": DIAG_USE.get(d["use"], d["use"])}]}, "rank": d.get("rank")} for d in e.get("diagnoses", []) or []] or None,
             "account": [self.ref(e["account"])] if e.get("account") else None,
             "hospitalization": strip_none({"admitSource": {"text": e["admit_source_text"]} if e.get("admit_source_text") else None,
                                            "dischargeDisposition": self.cs_cc("vn-discharge-disposition-cs", e.get("discharge_disposition"), e.get("discharge_disposition_text"))}) or None,
             "location": [{"location": self.ref(l["location"]), "status": l.get("status", "completed"), "period": l.get("period")} for l in e.get("locations", []) or []] or None,
             "serviceProvider": self.ref(e.get("service_provider") or self.fac_org)}
        self.add(r, "vn-core-encounter", "Lượt điều trị", [f"{(e.get('period') or {}).get('start')} → {(e.get('period') or {}).get('end')}", e.get("reason_text")], e.get("sources"))

    # ---- Condition
    def conditions(self):
        enc = (self.c.get("encounter") or {}).get("id")
        for cd in self.c.get("conditions", []) or []:
            code = self.cc(cd["code"], cd.get("text"))
            has_icd = any(c.get("system") == SYS["icd10vn"] for c in (code or {}).get("coding", []))
            r = {"resourceType": "Condition", "id": cd["id"],
                 "clinicalStatus": {"coding": [{"system": TH + "condition-clinical", "code": cd.get("clinical_status", "active")}]},
                 "verificationStatus": {"coding": [{"system": TH + "condition-ver-status", "code": cd.get("verification_status", "confirmed")}]},
                 "category": [{"coding": [{"system": TH + "condition-category", "code": "encounter-diagnosis", "display": "Encounter Diagnosis"}]}],
                 "code": code, "subject": self.ref(self.c["patient"]["id"]), "encounter": self.ref(cd.get("encounter") or enc), "onsetDateTime": cd.get("onset"),
                 "recordedDate": cd["recorded_date"], "recorder": self.ref(cd.get("recorder")), "asserter": self.ref(cd.get("asserter")),
                 "note": [{"text": cd["note"]}] if cd.get("note") else None}
            self.add(r, "vn-core-condition-diagnosis" if has_icd else "vn-core-condition", cd.get("text") or code.get("text"), [f"Vai trò: {cd.get('role')}", cd.get("note")], cd.get("sources"))

    # ---- Specimen
    def specimens(self):
        for s in self.c.get("specimens", []) or []:
            t = s["type"]
            r = {"resourceType": "Specimen", "id": s["id"], "identifier": [self.identifier(i, "Specimen") for i in s.get("identifiers", []) or []] or None,
                 "accessionIdentifier": {"system": self.local_sid("accession"), "value": s["accession"]} if s.get("accession") else None,
                 "status": s.get("status", "available"), "type": {"coding": [{"system": SYS["v2-0487"], "code": t["code"], "display": SPECIMEN_V2.get(t["code"], t.get("display"))}], "text": t.get("text")},
                 "subject": self.ref(self.c["patient"]["id"]), "receivedTime": s.get("received"), "request": self.refs(s.get("requests")),
                 "collection": strip_none({"collector": self.ref(s.get("collector")), "collectedDateTime": s.get("collected")}) or None, "note": [{"text": s["note"]}] if s.get("note") else None}
            self.add(r, "vn-core-specimen", f"Mẫu {t.get('text') or t['code']}", [s.get("received"), s.get("note")], s.get("sources"))

    # ---- ServiceRequest
    def service_requests(self):
        enc = (self.c.get("encounter") or {}).get("id")
        for s in self.c.get("service_requests", []) or []:
            r = {"resourceType": "ServiceRequest", "id": s["id"], "extension": [self.ext("vn-ext-referral-mode", "valueCodeableConcept", self.cs_cc("vn-referral-mode-cs", s["referral_mode"]))] if s.get("referral_mode") else None,
                 "identifier": [self.identifier(i, "ServiceRequest") for i in s.get("identifiers", []) or []] or None, "requisition": {"system": self.fac.get("order_system") or self.local_sid("y-lenh"), "value": s["requisition"]} if s.get("requisition") else None,
                 "status": s.get("status", "completed"), "intent": s.get("intent", "order"),
                 "category": [self.cs_cc("vn-service-request-category-cs", s["category"])] if s.get("category") else None, "priority": s.get("priority", "routine"),
                 "code": self.cc(s["code"]), "subject": self.ref(self.c["patient"]["id"]), "encounter": self.ref(enc), "occurrenceDateTime": s.get("occurrence"),
                 "authoredOn": s.get("authored_on"), "requester": self.ref(s.get("requester")), "performer": self.refs(s.get("performer")),
                 "reasonCode": [self.cc(x) for x in s.get("reason", []) or []] or None, "reasonReference": self.refs(s.get("reason_reference")),
                 "specimen": self.refs(s.get("specimen")), "note": [{"text": s["note"]}] if s.get("note") else None}
            self.add(r, "vn-core-service-request", s["code"].get("text"), [f"Chỉ định {s.get('authored_on')}", f"Nhóm {s.get('category')}"], s.get("sources"))

    # ---- Observation
    def observations(self):
        enc = (self.c.get("encounter") or {}).get("id")
        pt = self.c["patient"]["id"]
        for o in self.c.get("observations", []) or []:
            kind = o.get("kind", "exam")
            code = self.cc(o["code"])
            loinc = next((c["code"] for c in (code or {}).get("coding", []) if c["system"] == SYS["loinc"]), None)
            profile = None
            cat = None
            if kind == "vital-sign":
                vk = V.VITAL_BY_LOINC.get(loinc)
                profile = V.VITALS[vk][5] if vk else "vn-core-observation-vital-signs"
                cat = [{"coding": [{"system": TH + "observation-category", "code": "vital-signs", "display": "Vital Signs"}]}]
            elif kind == "lab":
                profile = "vn-core-observation-lab"
                cat = [{"coding": [{"system": TH + "observation-category", "code": "laboratory", "display": "Laboratory"}]}]
            else:
                catcode = {"exam": "exam", "survey": "survey", "social-history": "social-history", "imaging": "imaging", "procedure": "procedure", "activity": "activity"}.get(kind, "exam")
                cat = [{"coding": [{"system": TH + "observation-category", "code": catcode}]}]
            r = {"resourceType": "Observation", "id": o["id"], "basedOn": self.refs(o.get("based_on")), "partOf": self.refs(o.get("part_of")), "status": o.get("status", "final"),
                 "category": cat, "code": code, "subject": self.ref(pt), "encounter": self.ref(o.get("encounter") or enc), "effectiveDateTime": o.get("effective"),
                 "issued": o.get("issued"), "performer": self.refs(o.get("performer"))}
            r.update(self.value_of(o.get("value")))
            if o.get("interpretation"):
                r["interpretation"] = [{"coding": [{"system": TH + "v3-ObservationInterpretation", "code": i, "display": INTERP.get(i)}]} for i in o["interpretation"]]
            if o.get("note"):
                r["note"] = [{"text": o["note"]}]
            if o.get("body_site"):
                r["bodySite"] = self.cc(o["body_site"])
            if o.get("method_text"):
                r["method"] = {"text": o["method_text"]}
            if o.get("specimen"):
                r["specimen"] = self.ref(o["specimen"])
            if o.get("device_text"):
                r["device"] = {"display": o["device_text"]}
            rr = o.get("reference_range")
            if rr:
                q = o.get("value", {}).get("quantity", {})
                unit = rr.get("unit") or q.get("unit_raw")
                uc = V.ucum(unit)
                rng = {"low": {"value": rr["low"], "unit": uc[1] or unit, "system": SYS["ucum"] if uc[0] else None, "code": uc[0]} if rr.get("low") is not None else None,
                       "high": {"value": rr["high"], "unit": uc[1] or unit, "system": SYS["ucum"] if uc[0] else None, "code": uc[0]} if rr.get("high") is not None else None, "text": rr.get("text")}
                r["referenceRange"] = [strip_none(rng)]
            if o.get("components"):
                comps = []
                for cmp in o["components"]:
                    c = {"code": self.cc(cmp["code"])}
                    c.update(self.value_of(cmp.get("value")))
                    if cmp.get("interpretation"):
                        c["interpretation"] = [{"coding": [{"system": TH + "v3-ObservationInterpretation", "code": i, "display": INTERP.get(i)}]} for i in cmp["interpretation"]]
                    comps.append(c)
                r["component"] = comps
            if o.get("has_member"):
                r["hasMember"] = self.refs(o["has_member"])
            if o.get("derived_from"):
                r["derivedFrom"] = self.refs(o["derived_from"])
            val = o.get("value") or {}
            vtxt = val.get("quantity", {}).get("value") if "quantity" in val else val.get("string") or val.get("integer") or (val.get("codeable") or {}).get("text") or (f"không có kết quả ({val.get('absent')})" if "absent" in val else "")
            self.add(r, profile, (code or {}).get("text") or "Observation", [f"{vtxt} {val.get('quantity', {}).get('unit_raw', '')}".strip(), o.get("effective"), o.get("note")], o.get("sources"))

    # ---- DiagnosticReport
    def diagnostic_reports(self):
        enc = (self.c.get("encounter") or {}).get("id")
        for d in self.c.get("diagnostic_reports", []) or []:
            kind = d.get("kind", "lab")
            svc = d.get("category_service") or {"lab": "LAB", "imaging": "RAD", "functional": "OTH", "pathology": "PAT"}[kind]
            cost = d.get("cost_category") or {"lab": "3", "imaging": "4", "functional": "5", "pathology": "3"}[kind]
            profile = {"lab": "vn-core-diagnostic-report-lab", "imaging": "vn-core-diagnostic-report-imaging"}.get(kind, "vn-core-diagnostic-report")
            cats = [{"coding": [{"system": SYS["v2-0074"], "code": svc, "display": {"LAB": "Laboratory", "RAD": "Radiology", "OTH": "Other", "PAT": "Pathology"}.get(svc)}]},
                    self.cs_cc("vn-cost-category-cs", cost)]
            if d.get("category_extra"):
                cats.append(self.cc(d["category_extra"]))
            r = {"resourceType": "DiagnosticReport", "id": d["id"], "identifier": [self.identifier(i, "DiagnosticReport") for i in d.get("identifiers", []) or []] or None,
                 "basedOn": self.refs(d.get("based_on")), "status": d.get("status", "final"), "category": cats, "code": self.cc(d["code"]), "subject": self.ref(self.c["patient"]["id"]),
                 "encounter": self.ref(enc), "effectiveDateTime": d.get("effective"), "issued": d.get("issued"), "performer": self.refs(d.get("performer")),
                 "resultsInterpreter": self.refs(d.get("results_interpreter")), "specimen": self.refs(d.get("specimen")) if kind != "imaging" else None, "result": self.refs(d.get("results")),
                 "conclusion": d.get("conclusion"), "conclusionCode": [self.cc(x) for x in d.get("conclusion_codes", []) or []] or None,
                 "presentedForm": [{"contentType": doc["content_type"], "url": self._doc_url(doc), "title": doc.get("title"), "creation": doc.get("date")} for doc in [self._doc(d["presented_form"])] if doc] if d.get("presented_form") else None}
            lines = [f"Kỹ thuật: {d['technique_text']}" if d.get("technique_text") else None] + [f"- {x}" for x in d.get("findings", []) or []] + [f"Kết luận: {d['conclusion']}" if d.get("conclusion") else None]
            self.add(r, profile, d["code"].get("text"), lines, d.get("sources"))

    def _doc(self, doc_id):
        return next((x for x in self.c.get("documents", []) if x["id"] == doc_id), None)

    def _doc_url(self, doc):
        return doc.get("url") or f"{self.base}/Binary/{doc['id']}"

    # ---- Medication*
    def medications(self):
        for m in self.c.get("medications", []) or []:
            r = {"resourceType": "Medication", "id": m["id"], "code": self.cc(m["code"]), "status": "active", "form": self.cc(m["form"]) if m.get("form") else None,
                 "ingredient": [{"itemCodeableConcept": self.cc(i["item"]), "isActive": True, "strength": {"numerator": self.quantity(i["strength"]), "denominator": {"value": 1, "unit": i.get("per", "viên"), "system": SYS["ucum"], "code": V.ucum(i.get("per", "viên"))[0] or "1"}} if i.get("strength") else None} for i in m.get("ingredients", []) or []] or None}
            self.add(r, "vn-core-medication", m["code"].get("text"), [", ".join(f"{i['item'].get('text')} {i.get('strength', {}).get('value', '')}{i.get('strength', {}).get('unit_raw', '')}" for i in m.get("ingredients", []) or [])], m.get("sources"))

    def dosage(self, d: dict) -> dict:
        route = d.get("route") or {}
        rc = []
        if route.get("snomed"):
            rc.append({"system": SYS["snomed"], "code": route["snomed"], "display": route.get("snomed_display")})
        if route.get("vn_code"):
            rc.append({"system": CS("vn-medication-route-cs"), "code": route["vn_code"], "display": V.cs_display("vn-medication-route-cs", route["vn_code"])})
        t = d.get("timing") or {}
        out = {"text": d.get("text"), "route": strip_none({"coding": [strip_none(c) for c in rc] or None, "text": route.get("text")}) or None,
               "timing": strip_none({"repeat": strip_none({"frequency": t.get("frequency"), "period": t.get("period"), "periodUnit": t.get("period_unit"), "timeOfDay": t.get("time_of_day"), "boundsPeriod": t.get("bounds")}) or None, "code": {"text": t["text"]} if t.get("text") else None}) or None,
               "doseAndRate": [{"type": {"coding": [{"system": TH + "dose-rate-type", "code": "ordered", "display": "Ordered"}]}, "doseQuantity": self.quantity(d["dose"])}] if d.get("dose") else None}
        return strip_none(out)

    def medication_requests(self):
        enc = (self.c.get("encounter") or {}).get("id")
        for m in self.c.get("medication_requests", []) or []:
            d = m.get("dosage") or {}
            r = {"resourceType": "MedicationRequest", "id": m["id"], "extension": [self.ext("vn-ext-payment-source", "valueCodeableConcept", self.cs_cc("vn-payment-source-cs", m["payment_source"]))] if m.get("payment_source") else None,
                 "identifier": [self.identifier(i, "MedicationRequest") for i in m.get("identifiers", []) or []] or None, "status": m.get("status", "completed"), "intent": m.get("intent", "order"),
                 "category": [{"coding": [{"system": TH + "medicationrequest-category", "code": m.get("category", "inpatient"), "display": {"inpatient": "Inpatient", "outpatient": "Outpatient", "discharge": "Discharge"}.get(m.get("category", "inpatient"))}]}],
                 "medicationReference": self.ref(m["medication"]) if m.get("medication") in self.registry else None, "medicationCodeableConcept": self.cc(m["medication_code"]) if m.get("medication_code") else None,
                 "subject": self.ref(self.c["patient"]["id"]), "encounter": self.ref(enc), "authoredOn": m["authored_on"], "requester": self.ref(m.get("requester")),
                 "reasonReference": self.refs(m.get("reason_reference")), "note": [{"text": m["note"]}] if m.get("note") else None,
                 "dosageInstruction": [self.dosage(d)],
                 "dispenseRequest": strip_none({"validityPeriod": m.get("use_period"), "quantity": self.quantity(m["dispense"]) if m.get("dispense") else None}) or None}
            self.add(r, "vn-core-medication-request", f"Y lệnh thuốc: {self._med_name(m)}", [d.get("text"), f"Kê {m['authored_on']}"], m.get("sources"))

    def _med_name(self, m):
        if m.get("medication"):
            med = next((x for x in self.c.get("medications", []) if x["id"] == m["medication"]), None)
            if med:
                return med["code"].get("text")
        return (m.get("medication_code") or {}).get("text", "")

    def medication_administrations(self):
        enc = (self.c.get("encounter") or {}).get("id")
        for m in self.c.get("medication_administrations", []) or []:
            d = self.dosage(m.get("dosage") or {})
            d.pop("timing", None); d.pop("doseAndRate", None)
            if m.get("dosage", {}).get("dose"):
                d["dose"] = self.quantity(m["dosage"]["dose"])
            r = {"resourceType": "MedicationAdministration", "id": m["id"], "status": m.get("status", "completed"),
                 "category": {"coding": [{"system": TH + "medication-admin-category", "code": m.get("category", "inpatient"), "display": "Inpatient"}]},
                 "medicationReference": self.ref(m["medication"]) if m.get("medication") in self.registry else None, "medicationCodeableConcept": self.cc(m["medication_code"]) if m.get("medication_code") else None,
                 "subject": self.ref(self.c["patient"]["id"]), "context": self.ref(enc), "effectiveDateTime": m.get("effective"), "effectivePeriod": m.get("effective_period"),
                 "performer": [{"actor": self.ref(p)} for p in m.get("performer", []) or []] or None, "request": self.ref(m.get("request")), "note": [{"text": m["note"]}] if m.get("note") else None, "dosage": d or None}
            self.add(r, "vn-core-medication-administration", f"Dùng thuốc: {self._med_name(m)}", [m.get("effective"), d.get("text")], m.get("sources"))

    # ---- AllergyIntolerance / FamilyMemberHistory
    def allergies(self):
        enc = (self.c.get("encounter") or {}).get("id")
        for a in self.c.get("allergies", []) or []:
            if a.get("nka"):
                code = {"coding": [{"system": SYS["snomed"], "code": "716186003", "display": "No known allergy"}], "text": a.get("text") or "Chưa ghi nhận dị ứng"}
            else:
                code = self.cc(a["code"])
            r = {"resourceType": "AllergyIntolerance", "id": a["id"], "clinicalStatus": {"coding": [{"system": TH + "allergyintolerance-clinical", "code": a.get("clinical_status", "active")}]} if not a.get("nka") else None,
                 "verificationStatus": {"coding": [{"system": TH + "allergyintolerance-verification", "code": a.get("verification_status", "confirmed")}]},
                 "type": a.get("type"), "category": a.get("category"), "criticality": a.get("criticality"), "code": code, "patient": self.ref(self.c["patient"]["id"]), "encounter": self.ref(enc),
                 "recordedDate": a.get("recorded_date"), "recorder": self.ref(a.get("recorder")), "note": [{"text": a["note"]}] if a.get("note") else None,
                 "reaction": [{"manifestation": [self.cc(x) for x in rx.get("manifestation", [])], "severity": rx.get("severity")} for rx in a.get("reactions", []) or []] or None}
            self.add(r, "vn-core-allergy-intolerance", code.get("text") or "Dị ứng", [a.get("recorded_date"), a.get("note")], a.get("sources"))

    def family_history(self):
        for f in self.c.get("family_history", []) or []:
            rel = f.get("relationship") or {"text": "Gia đình"}
            r = {"resourceType": "FamilyMemberHistory", "id": f["id"], "status": f.get("status", "completed"), "patient": self.ref(self.c["patient"]["id"]), "date": f.get("date"),
                 "relationship": self.cc(rel) if isinstance(rel, dict) else {"text": rel}, "sex": f.get("sex"),
                 "note": [{"text": f["note"]}] if f.get("note") else None,
                 "condition": [{"code": self.cc(c["code"]), "note": [{"text": c["note"]}] if c.get("note") else None} for c in f.get("conditions", []) or []] or None}
            self.add(r, "vn-core-family-member-history", "Tiền sử gia đình", [f.get("note")], f.get("sources"))

    # ---- ClinicalImpression / CarePlan
    def clinical_impressions(self):
        enc = (self.c.get("encounter") or {}).get("id")
        for ci in self.c.get("clinical_impressions", []) or []:
            r = {"resourceType": "ClinicalImpression", "id": ci["id"], "status": ci.get("status", "completed"), "code": {"text": ci["code_text"]} if ci.get("code_text") else None,
                 "description": ci.get("description"), "subject": self.ref(self.c["patient"]["id"]), "encounter": self.ref(enc), "effectiveDateTime": ci.get("date"), "date": ci.get("date"),
                 "assessor": self.ref(ci.get("assessor")), "problem": self.refs(ci.get("problems")), "summary": ci.get("summary"),
                 "finding": [{"itemReference": self.ref(f["reference"]), "basis": f.get("basis")} if f.get("reference") else {"itemCodeableConcept": self.cc(f["code"]), "basis": f.get("basis")} for f in ci.get("findings", []) or []] or None,
                 "prognosisCodeableConcept": [{"text": ci["prognosis_text"]}] if ci.get("prognosis_text") else None,
                 "note": [{"text": n} for n in ci.get("notes", []) or []] or None}
            self.add(r, "vn-core-clinical-impression", ci.get("code_text") or "Nhận định lâm sàng", [ci.get("summary"), ci.get("description")], ci.get("sources"))

    def care_plans(self):
        enc = (self.c.get("encounter") or {}).get("id")
        for cp in self.c.get("care_plans", []) or []:
            acts = []
            for a in cp.get("activities", []) or []:
                det = {"kind": a.get("kind"), "code": self.cc(a["code"]) if a.get("code") else None, "status": a.get("status", "completed"), "description": a.get("description"),
                       "scheduledString": a.get("scheduled_text"), "scheduledTiming": a.get("scheduled_timing"), "performer": self.refs(a.get("performer")), "reasonCode": [self.cc(x) for x in a.get("reason", []) or []] or None}
                acts.append({"detail": strip_none(det)})
            r = {"resourceType": "CarePlan", "id": cp["id"], "status": cp.get("status", "completed"), "intent": cp.get("intent", "plan"), "category": [self.cc(c) for c in cp.get("category", []) or []] or None,
                 "title": cp.get("title"), "description": cp.get("description"), "subject": self.ref(self.c["patient"]["id"]), "encounter": self.ref(enc), "period": cp.get("period"), "created": cp.get("created"),
                 "author": self.ref(cp.get("author")), "contributor": self.refs(cp.get("contributors")), "addresses": self.refs(cp.get("addresses")), "activity": acts or None,
                 "note": [{"text": n} for n in cp.get("notes", []) or []] or None}
            self.add(r, "vn-core-care-plan", cp.get("title") or "Kế hoạch chăm sóc", [cp.get("description")] + [f"- {a.get('description') or (a.get('code') or {}).get('text')}" for a in cp.get("activities", []) or []], cp.get("sources"))

    # ---- DocumentReference (mỗi tài liệu gốc)
    def documents(self):
        enc = (self.c.get("encounter") or {}).get("id")
        for d in self.c.get("documents", []) or []:
            t = d.get("type") or V.DOC_TYPES.get(d.get("form"), V.DOC_TYPES["default"])
            tcode, tdisp = (t["code"], t.get("display")) if isinstance(t, dict) else t
            idents = []
            if d.get("identifier"):
                idents.append(self.identifier({"type": "LOCAL", "text": "Số tài liệu", **d["identifier"]}, "DocumentReference"))
            r = {"resourceType": "DocumentReference", "id": d["id"], "masterIdentifier": {"system": "urn:ietf:rfc:3986", "value": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, self.base + '/doc/' + d['id'])}"},
                 "identifier": idents or None, "status": d.get("status", "current"), "docStatus": "final", "type": {"coding": [{"system": SYS["loinc"], "code": tcode, "display": tdisp}], "text": d.get("title")},
                 "category": [self.cc(d["category"])] if d.get("category") else None, "subject": self.ref(self.c["patient"]["id"]), "date": d.get("date") if d.get("date") and "T" in d["date"] else (f"{d['date']}T00:00:00+07:00" if d.get("date") else None),
                 "author": self.refs(d.get("authors")) or [self.ref(d.get("author_org") or self.fac_org)], "authenticator": self.ref(d.get("authenticator")), "custodian": self.ref(d.get("custodian") or self.fac_org),
                 "description": d.get("note"),
                 "content": [{"attachment": strip_none({"contentType": d.get("content_type", "application/pdf"), "language": "vi", "url": self._doc_url(d), "size": d.get("size_bytes"), "hash": d.get("sha1_b64"), "title": d.get("title") or d.get("file"), "creation": d.get("date")}),
                              "format": {"system": "http://ihe.net/fhir/ihe.formatcode.fhir/CodeSystem/formatcode", "code": "urn:ihe:iti:xds:2017:mimeTypeSufficient", "display": "mimeType Sufficient"}}],
                 "context": strip_none({"encounter": [self.ref(enc)] if enc else None, "period": (self.c.get("encounter") or {}).get("period"), "facilityType": None}) or None}
            self.add(r, "vn-core-document-reference", d.get("title") or d.get("file"), [f"Tệp: {d.get('file')} ({d.get('pages')} trang)", f"Biểu mẫu: {d.get('form')}", "Chưa trích xuất (bản scan)" if d.get("extracted") is False else None], [d["id"]])

    # ---- Cost statement: Account + Claim + Invoice + Composition
    def cost_statement(self):
        cs = self.c.get("cost_statement")
        if not cs:
            return
        ids = cs["ids"]
        pt = self.c["patient"]["id"]
        enc = (self.c.get("encounter") or {}).get("id")
        cov = (self.c.get("coverage") or {}).get("id")
        ma_lk_ident = {"type": {"coding": [{"system": CS("vn-identifier-type-cs"), "code": "MA_LK", "display": V.cs_display("vn-identifier-type-cs", "MA_LK")}]}, "system": SID("ma-lk"), "value": cs["ma_lk"], "assigner": self.ref(self.fac_org)}
        period = cs["period"]
        # Account
        acc = {"resourceType": "Account", "id": ids["account"], "identifier": [ma_lk_ident], "status": cs.get("account_status", "inactive"),
               "type": {"coding": [{"system": TH + "v3-ActCode", "code": "PBILLACCT", "display": "patient billing account"}]}, "name": cs.get("account_name") or f"Đợt điều trị {period.get('start', '')[:10]} – {period.get('end', '')[:10]}",
               "subject": [self.ref(pt)], "servicePeriod": period, "coverage": [{"coverage": self.ref(cov), "priority": 1}] if cov else None, "owner": self.ref(self.fac_org), "description": cs.get("account_description")}
        self.add(acc, "vn-core-account", acc["name"], [f"MA_LK {cs['ma_lk']}"], cs.get("sources"))
        # Claim
        lines = cs["lines"]
        items = []
        for ln in lines:
            exts = []
            if ln.get("department"):
                exts.append(self.ext("vn-ext-claim-item-department", "valueCodeableConcept", self.cs_cc("vn-department-cs", ln["department"])))
            ici = self._insurance_cost_info(ln, cs)
            if ici:
                exts.append(ici)
            alloc = self._allocation(ln)
            if alloc:
                exts.append(alloc)
            if ln.get("payment_source"):
                exts.append(self.ext("vn-ext-payment-source", "valueCodeableConcept", self.cs_cc("vn-payment-source-cs", ln["payment_source"])))
            items.append(strip_none({"extension": exts or None, "sequence": ln["sequence"], "category": self.cs_cc("vn-cost-category-cs", ln["category"]), "productOrService": self.cc(ln.get("code")) if ln.get("code") else {"text": ln["text"]},
                                     "servicedDate": ln.get("serviced") if isinstance(ln.get("serviced"), str) else None, "servicedPeriod": ln.get("serviced") if isinstance(ln.get("serviced"), dict) else None,
                                     "quantity": {"value": ln["quantity"], "unit": ln.get("unit")} if ln.get("quantity") is not None else None, "unitPrice": money(ln.get("hospital_unit_price")), "net": money(ln.get("hospital_amount")),
                                     "encounter": [self.ref(enc)] if enc else None}))
        totals = cs.get("totals") or {}
        summary = [{"url": k, "valueMoney": money(totals[j])} for j, k in (("medication", "totalMedication"), ("supplies", "totalSupplies"), ("insurance", "totalInsurance"), ("patient_copay", "totalPatientCopay"),
                                                                           ("patient_self_pay", "totalPatientSelfPay"), ("other_fund", "totalOtherFund"), ("non_covered", "totalNonCovered"), ("insurance_ffs", "totalInsuranceFeeForService")) if totals.get(j) is not None]
        claim = {"resourceType": "Claim", "id": ids["claim"],
                 "extension": [x for x in [{"url": PROF("vn-ext-claim-cost-summary"), "extension": summary} if summary else None,
                                           self.ext("vn-ext-payment-method", "valueCodeableConcept", self.cs_cc("vn-payment-method-cs", cs.get("payment_method"))) if cs.get("payment_method") else None] if x],
                 "identifier": [ma_lk_ident] + [self.identifier(i, "Claim") for i in cs.get("identifiers", []) or []], "status": "active",
                 "type": {"coding": [{"system": TH + "claim-type", "code": cs.get("claim_type", "institutional"), "display": "Institutional" if cs.get("claim_type", "institutional") == "institutional" else "Professional"}]},
                 "subType": self.cs_cc("vn-patient-visit-type-cs", cs["claim_subtype"]) if cs.get("claim_subtype") else None, "use": "claim", "patient": self.ref(pt),
                 "billablePeriod": period, "created": cs.get("prepared_at", period.get("end")), "insurer": self.ref(cs["insurer"]) if cs.get("insurer") in self.registry else ({"display": cs["insurer"]} if cs.get("insurer") else None),
                 "provider": self.ref(self.fac_org), "priority": {"coding": [{"system": TH + "processpriority", "code": "normal", "display": "Normal"}]},
                 "diagnosis": [{"sequence": i + 1, "diagnosisCodeableConcept": self._claim_dx(d), "type": [self._dx_type(d)]} for i, d in enumerate(cs.get("diagnoses", []))] or None,
                 "insurance": [{"sequence": 1, "focal": True, "coverage": self.ref(cov)}] if cov else [{"sequence": 1, "focal": True, "coverage": {"display": "Không có BHYT"}}],
                 "item": items, "total": money(totals.get("hospital_total"))}
        self.add(claim, "vn-core-claim", f"Hồ sơ thanh toán MA_LK {cs['ma_lk']}", [f"Tổng chi phí {totals.get('hospital_total')} VND", f"BHYT {totals.get('insurance')} VND"], cs.get("sources"))
        # Invoice
        segs = []
        for sg in cs["segments"]:
            segs.append({"url": PROF("vn-ext-cost-statement-segment"), "extension": strip_none([{"url": "sequence", "valuePositiveInt": sg["sequence"]}, {"url": "coverage", "valueReference": self.ref(sg["coverage"])} if sg.get("coverage") else None,
                                                                                               {"url": "period", "valuePeriod": sg["period"]}, {"url": "benefitLevel", "valueDecimal": sg["benefit_level"]} if sg.get("benefit_level") is not None and sg.get("coverage") else None])})
        line_items = []
        for ln in lines:
            lext = {"url": PROF("vn-ext-cost-statement-line"), "extension": strip_none([{"url": "segmentSequence", "valuePositiveInt": ln.get("segment", 1)}, {"url": "category", "valueCodeableConcept": self.cs_cc("vn-cost-category-cs", ln["category"])},
                                                                                       {"url": "servicedDate", "valueDate": ln["serviced"]} if isinstance(ln.get("serviced"), str) else {"url": "serviced", "valuePeriod": ln["serviced"]},
                                                                                       {"url": "unit", "valueString": ln["unit"]} if ln.get("unit") else None, {"url": "hospitalUnitPrice", "valueMoney": money(ln["hospital_unit_price"])} if ln.get("hospital_unit_price") is not None else None,
                                                                                       {"url": "relatedServiceSequence", "valuePositiveInt": ln["related_sequence"]} if ln.get("related_sequence") else None])}
            # 'serviced' slice: value[x] là date|Period -> url phải là 'serviced' với valueDate/valuePeriod
            for e in lext["extension"]:
                if e["url"] == "servicedDate":
                    e["url"] = "serviced"
            exts = [lext]
            ici = self._insurance_cost_info(ln, cs)
            if ici:
                exts.append(ici)
            alloc = self._allocation(ln)
            if alloc:
                exts.append(alloc)
            line_items.append(strip_none({"extension": exts, "sequence": ln["sequence"], "chargeItemCodeableConcept": self.cc(ln.get("code")) if ln.get("code") else {"text": ln["text"]},
                                          "priceComponent": [{"type": "base", "code": {"text": "Thành tiền bệnh viện"}, "factor": ln.get("quantity"), "amount": money(ln.get("hospital_amount"))}]}))
        inv = {"resourceType": "Invoice", "id": ids["invoice"], "extension": segs, "identifier": [ma_lk_ident] + ([{"type": {"text": "Số bảng kê / số lưu trữ"}, "system": self.local_sid("so-bang-ke"), "value": cs["so_bang_ke"]}] if cs.get("so_bang_ke") else []),
               "status": cs.get("invoice_status", "issued"), "type": {"text": cs.get("form_name") or "Bảng kê chi phí khám bệnh, chữa bệnh (Mẫu 01/KBCB, QĐ 697/QĐ-BYT)"},
               "subject": self.ref(pt), "recipient": self.ref(pt), "date": cs.get("prepared_at", period.get("end")), "participant": [{"role": {"text": "Người lập bảng kê"}, "actor": self.ref(cs["prepared_by"])}] if cs.get("prepared_by") else None,
               "issuer": self.ref(self.fac_org), "account": self.ref(ids["account"]), "lineItem": line_items, "totalNet": money(totals.get("hospital_total")),
               "note": [{"text": f"Bằng chữ: {cs['total_in_words']}"}] if cs.get("total_in_words") else None}
        self.add(inv, "vn-core-cost-statement-invoice", f"Bảng kê chi phí MA_LK {cs['ma_lk']}", [f"{len(lines)} dòng; tổng {totals.get('hospital_total')} VND"] + [f"- {ln['text']}: {ln.get('hospital_amount')}" for ln in lines], cs.get("sources"))
        # Composition (bản ký bảng kê)
        attesters = []
        for at in cs.get("attestations", []) or []:
            mode = "personal" if at["role"] == "patient-confirmation" else "professional"
            party = at.get("party") or (pt if mode == "personal" else self.fac_org)
            attesters.append(strip_none({"extension": [{"url": PROF("vn-ext-cost-statement-attestation-role"), "extension": strip_none([{"url": "role", "valueCode": at["role"]}, {"url": "attestedAt", "valueDateTime": at["time"]} if at.get("time") else None])}],
                                         "mode": mode, "time": at.get("time"), "party": self.ref(party)}))
        sections = []
        for sg in cs["segments"]:
            title = sg.get("title") or f"Từ {sg['period']['start'][:10]} đến {sg['period']['end'][:10]}" + (f" — mức hưởng {sg['benefit_level']}%" if sg.get("benefit_level") is not None else "")
            sections.append({"title": title, "code": {"text": "Phân đoạn bảng kê"}, "text": self.narrative(title, [f"{len([l for l in lines if l.get('segment', 1) == sg['sequence']])} dòng chi phí"]), "entry": [self.ref(ids["invoice"])]})
        comp = {"resourceType": "Composition", "id": ids["composition"], "identifier": ma_lk_ident, "status": cs.get("composition_status", "final" if len(attesters) == 3 else "preliminary"),
                "type": {"text": "Bảng kê chi phí khám bệnh, chữa bệnh (QĐ 697/QĐ-BYT)"}, "subject": self.ref(pt), "encounter": self.ref(enc), "date": cs.get("prepared_at", period.get("end")),
                "author": [self.ref(cs["prepared_by"]) if cs.get("prepared_by") else self.ref(self.fac_org)], "title": f"Bảng kê chi phí khám bệnh, chữa bệnh — MA_LK {cs['ma_lk']}",
                "attester": attesters or None, "custodian": self.ref(self.fac_org), "section": sections}
        self.add(comp, "vn-core-cost-statement-composition", comp["title"], [f"Trạng thái {comp['status']}; {len(attesters)} xác nhận"], cs.get("sources"))

    DX_TYPES = {"principal": "Principal Diagnosis", "admitting": "Admitting Diagnosis", "clinical": "Clinical Diagnosis", "differential": "Differential Diagnosis", "discharge": "Discharge Diagnosis",
                "laboratory": "Laboratory Diagnosis", "nursing": "Nursing Diagnosis", "prenatal": "Prenatal Diagnosis", "radiology": "Radiology Diagnosis", "remote": "Remote Diagnosis", "retrospective": "Retrospective Diagnosis", "self": "Self Diagnosis"}

    def _dx_type(self, d):
        t = d.get("type", "principal")
        if t in self.DX_TYPES:
            return strip_none({"coding": [{"system": TH + "ex-diagnosistype", "code": t, "display": self.DX_TYPES[t]}], "text": d.get("type_text")})
        return {"text": d.get("type_text") or t}

    def _claim_dx(self, d):
        if d.get("condition"):
            cd = next(x for x in self.c["conditions"] if x["id"] == d["condition"])
            cc = self.cc(cd["code"], cd.get("text"))
            cc["coding"] = [c for c in cc.get("coding", []) if c["system"] == SYS["icd10vn"]]
            return cc
        return self.cc(d["code"])

    def _insurance_cost_info(self, ln, cs):
        sub = strip_none([
            {"url": "scope", "valueCodeableConcept": self.cs_cc("vn-insurance-scope-cs", ln["scope"])} if ln.get("scope") else None,
            {"url": "paymentRatio", "valueDecimal": ln["insurance_payment_ratio"]} if ln.get("insurance_payment_ratio") is not None else None,
            {"url": "benefitLevel", "valueDecimal": ln["benefit_level"]} if ln.get("benefit_level") is not None else None,
            {"url": "servicePaymentRatio", "valueDecimal": ln["service_payment_ratio"]} if ln.get("service_payment_ratio") is not None else None,
            {"url": "insuranceUnitPrice", "valueMoney": money(ln["insurance_unit_price"])} if ln.get("insurance_unit_price") is not None else None,
            {"url": "insuranceLineAmount", "valueMoney": money(ln["insurance_amount"])} if ln.get("insurance_amount") is not None else None])
        return {"url": PROF("vn-ext-insurance-cost-info"), "extension": sub} if sub else None

    def _allocation(self, ln):
        a = ln.get("allocation")
        if not a:
            return None
        return {"url": PROF("vn-ext-line-payment-allocation"), "extension": [{"url": k, "valueMoney": money(a.get(j, 0))} for j, k in (("insurance_fund", "insuranceFund"), ("patient_copay", "patientCopay"), ("other_source", "otherSource"), ("patient_self_pay", "patientSelfPay"))]}

    # ---- Composition lâm sàng (tóm tắt bệnh án / giấy ra viện...)
    def compositions(self):
        enc = (self.c.get("encounter") or {}).get("id")
        for key, cp in (self.c.get("compositions") or {}).items():
            sections = []
            for s in cp.get("sections", []):
                code, disp = V.SECTIONS.get(s.get("key"), (None, None))
                if s.get("code"):
                    code, disp = s["code"]["code"], s["code"].get("display")
                sections.append(strip_none({"title": s["title"], "code": {"coding": [{"system": SYS["loinc"], "code": code, "display": disp}]} if code else {"text": s["title"]},
                                            "text": self.narrative(s["title"], s.get("text_lines", [])) if s.get("text_lines") else None, "entry": self.refs(s.get("entries")),
                                            "emptyReason": {"coding": [{"system": TH + "list-empty-reason", "code": "nilknown"}]} if not s.get("text_lines") and not s.get("entries") else None}))
            ident = cp.get("identifier")
            r = {"resourceType": "Composition", "id": cp["id"],
                 "identifier": self.identifier({"type": "LOCAL", "text": ident.get("text", "Mã bệnh án"), "value": ident["value"], "system": ident.get("system") or SID("document-number"), "assigner": ident.get("assigner", self.fac_org)}, "DocumentReference") if ident else None,
                 "status": cp.get("status", "final"), "type": {"coding": [{"system": SYS["loinc"], "code": cp["type"]["code"], "display": cp["type"].get("display")}], "text": cp.get("type_text")},
                 "category": [self.cc(c) for c in cp.get("category", []) or []] or None, "subject": self.ref(self.c["patient"]["id"]), "encounter": self.ref(enc), "date": cp["date"],
                 "author": self.refs(cp.get("authors")) or [self.ref(self.fac_org)], "title": cp["title"], "confidentiality": cp.get("confidentiality", "R"),
                 "attester": [{"mode": a.get("mode", "professional"), "time": a.get("time"), "party": self.ref(a["party"])} for a in cp.get("attesters", []) or []] or None,
                 "custodian": self.ref(cp.get("custodian") or self.fac_org), "event": [{"period": (self.c.get("encounter") or {}).get("period")}] if enc else None, "section": sections}
            self.add(r, "vn-core-composition", cp["title"], [cp.get("date")], cp.get("sources"))

    # ---- Provenance: một bản ghi cho mỗi tài liệu gốc, cộng một bản cho dữ liệu từ cấu hình cơ sở
    def provenances(self):
        recorded = self.meta.get("created")
        docs = {d["id"]: d for d in self.c.get("documents", []) or []}
        cost_ids = set((self.c.get("cost_statement") or {}).get("ids", {}).values())

        def make(pid, targets, doc, label):
            targets = sorted(t for t in targets if not t.startswith("Provenance/"))
            if not targets:
                return
            author = (doc or {}).get("authors") or []
            who = self.ref(author[0]) if author else self.ref(self.fac_org)
            reasons = [{"coding": [{"system": CS("vn-consent-purpose-cs"), "code": "TREAT", "display": V.cs_display("vn-consent-purpose-cs", "TREAT")}]}]
            if any(t.split("/")[1] in cost_ids for t in targets):
                reasons.append({"coding": [{"system": CS("vn-consent-purpose-cs"), "code": "HPAYMT", "display": V.cs_display("vn-consent-purpose-cs", "HPAYMT")}]})
            r = {"resourceType": "Provenance", "id": pid, "target": [{"reference": t} for t in targets], "occurredDateTime": (doc or {}).get("date") if doc and "T" in str(doc.get("date", "")) else None,
                 "recorded": recorded, "policy": [self.meta["policy"]] if self.meta.get("policy") else None, "reason": reasons,
                 "activity": {"coding": [{"system": TH + "v3-DataOperation", "code": "CREATE", "display": "create"}], "text": self.meta.get("activity_text") or "Chuyển đổi từ hồ sơ bệnh án giấy/PDF sang FHIR"},
                 "agent": [{"type": {"coding": [{"system": TH + "provenance-participant-type", "code": "author", "display": "Author"}]}, "who": who, "onBehalfOf": self.ref(self.fac_org) if who.get("reference") != f"Organization/{self.fac_org}" else None},
                           {"type": {"coding": [{"system": TH + "provenance-participant-type", "code": "assembler", "display": "Assembler"}]}, "who": {"display": self.meta.get("created_by", "vn-fhir-convert")}}],
                 "entity": [{"role": "source", "what": self.ref(doc["id"])}] if doc else None}
            self.add(r, "vn-core-provenance", label, [f"{len(targets)} resource"], None)

        for did, targets in sorted(self.sources.items()):
            if did not in docs:
                self.warnings.append(f"sources trỏ tới tài liệu không tồn tại: {did}")
                continue
            make(f"prov-{did}", targets, docs[did], f"Nguồn gốc từ tài liệu {docs[did].get('title') or did}")
        if self.unsourced:
            make("prov-facility-config", self.unsourced, None, "Nguồn gốc từ cấu hình cơ sở / danh mục (không có tài liệu gốc)")

    # ------------------------------------------------------------------ bundles
    def full_url(self, key):
        return f"{self.base}/{key}"

    def local_sid(self, name: str) -> str:
        """Namespace định danh cục bộ của cơ sở: cùng gốc với mrn_system (http://his.../sid/<name>); fallback <base_url>/sid/<name>."""
        mrn = self.fac.get("mrn_system")
        root = mrn.rsplit("/", 1)[0] if mrn and "/" in mrn else f"{self.base}/sid"
        return f"{root}/{name}"

    def closure(self, roots: list[str]) -> list[str]:
        seen: list[str] = []
        stack = list(roots)
        while stack:
            k = stack.pop(0)
            if k in seen or k not in self.res:
                continue
            seen.append(k)
            for ref in self._refs_in(self.res[k]):
                if ref in self.res and ref not in seen:
                    stack.append(ref)
        # Provenance của các resource đã gom (+ bao đóng của agent/entity: Practitioner, Organization, DocumentReference)
        for k in self.order:
            if k.startswith("Provenance/") and k not in seen and any(t["reference"] in seen for t in self.res[k]["target"]):
                seen.append(k)
                stack = [r for r in self._refs_in({x: v for x, v in self.res[k].items() if x != "target"}) if r in self.res and r not in seen]
                while stack:
                    r = stack.pop(0)
                    if r in seen:
                        continue
                    seen.append(r)
                    stack.extend(x for x in self._refs_in(self.res[r]) if x in self.res and x not in seen)
        return seen

    def _refs_in(self, node, acc=None):
        acc = [] if acc is None else acc
        if isinstance(node, dict):
            if isinstance(node.get("reference"), str) and "/" in node["reference"]:
                acc.append(node["reference"])
            for v in node.values():
                self._refs_in(v, acc)
        elif isinstance(node, list):
            for x in node:
                self._refs_in(x, acc)
        return acc

    def bundle(self, bid, btype, keys, profile, identifier=None, request=False) -> dict:
        b = {"resourceType": "Bundle", "id": bid, "meta": {"profile": [PROF(profile)]}, "identifier": identifier, "type": btype, "timestamp": self.meta.get("created"),
             "entry": [strip_none({"fullUrl": self.full_url(k), "resource": self._bundle_copy(k, keys), "request": {"method": "PUT", "url": k} if request else None}) for k in keys]}
        return strip_none(b)

    def _bundle_copy(self, key, keys):
        """Provenance trong bundle chỉ giữ target nằm trong bundle (bản độc lập trong resources/ vẫn giữ đủ target)."""
        r = self.res[key]
        if key.startswith("Provenance/"):
            inside = set(keys)
            r = dict(r, target=[t for t in r["target"] if t["reference"] in inside])
        return r

    def bundles(self) -> dict[str, dict]:
        out = {}
        case = V.slug(self.meta.get("case_id", "case"))
        comps = self.c.get("compositions") or {}
        main = comps.get("discharge_summary") or (next(iter(comps.values())) if comps else None)
        if main:
            keys = self.closure([f"Composition/{main['id']}"])
            out["bundle-document.json"] = self.bundle(f"doc-{case}", "document", keys, "vn-core-bundle", {"system": "urn:ietf:rfc:3986", "value": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, self.base + '/bundle-document/' + case)}"})
        cs = self.c.get("cost_statement")
        if cs:
            ids = cs["ids"]
            roots = [f"Composition/{ids['composition']}", f"Invoice/{ids['invoice']}", f"Account/{ids['account']}"]
            keys = self.closure(roots)
            keys = roots + [k for k in keys if k not in roots]  # Composition, Invoice, Account đứng đầu
            out["bundle-cost-statement.json"] = self.bundle(f"cost-{case}", "document", keys, "vn-core-cost-statement-document-bundle", {"system": SID("ma-lk"), "value": cs["ma_lk"]})
        out["bundle-transaction.json"] = self.bundle(f"tx-{case}", "transaction", list(self.order), "vn-core-bundle", request=True)
        return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clinical")
    ap.add_argument("out_dir")
    ap.add_argument("--vendor")
    a = ap.parse_args(argv)
    V.find_vendor(a.vendor)
    clin = V.read_json(a.clinical)
    if str(clin.get("meta", {}).get("schema_version", "")).split(".")[0] != "2":
        raise SystemExit("clinical JSON phải có meta.schema_version '2.x' (xem references/clinical-schema.md)")
    b = Builder(clin)
    b.build()
    out = Path(a.out_dir)
    rd = out / "resources"
    if rd.exists():
        for f in rd.glob("*.json"):
            f.unlink()
    for k in b.order:
        V.write_json(b.res[k], rd / f"{k.replace('/', '-')}.json")
    bundles = b.bundles()
    for name, bd in bundles.items():
        V.write_json(bd, out / name)
    counts = defaultdict(int)
    for k in b.order:
        counts[k.split("/")[0]] += 1
    report = {"case_id": clin["meta"].get("case_id"), "resources": len(b.order), "by_type": dict(sorted(counts.items())), "bundles": {k: len(v["entry"]) for k, v in bundles.items()},
              "provenance_documents": sorted(b.sources.keys()), "unsourced_resources": b.unsourced, "warnings": b.warnings}
    V.write_json(report, out / "build_report.json")
    print(f"[build_fhir] {len(b.order)} resources -> {rd}; bundles: {', '.join(f'{k} ({len(v['entry'])})' for k, v in bundles.items())}; cảnh báo: {len(b.warnings)}")
    for w in b.warnings:
        print("  !", w)


if __name__ == "__main__":
    main()
