"""quality_check.py — kiểm tra chất lượng clinical JSON v2 TRƯỚC khi dựng FHIR (vòng lặp chất lượng, bước 5 của skill).

Kiểm tra (mỗi phát hiện có mức error / warning / info):
  1. JSON Schema (assets/clinical.schema.json)
  2. Toàn vẹn tham chiếu: mọi id được trỏ tới (sources, performer, recorder, requester, results, specimen, condition…) phải tồn tại; id trùng
  3. Mã hoá: mã vn-*-cs tồn tại; ICD-10 VN có trong từ điển; CLS có trong QĐ 1227; LOINC đúng check digit + bảng đã kiểm (null => error,
     DEPRECATED => warning, chưa kiểm => info); UCUM trong bảng; coding source=llm chưa verified => error; verified=false => warning;
     codings rỗng => info (liệt kê để người duyệt tra cứu)
  4. Sinh hiệu: LOINC thuộc bảng VITALS, nhãn đơn vị đúng fixedString, giá trị trong khoảng sinh lý
  5. Xét nghiệm: có UCUM, khoảng tham chiếu low<high, cờ H/L nhất quán với giá trị
  6. Dòng thời gian: mọi mốc trong [vào viện - 1 ngày, ra viện + 2 ngày]; issued >= effective; sinh < vào viện; thẻ BHYT phủ đợt điều trị
  7. Bảng kê: thành tiền = SL x đơn giá; phân bổ = thành tiền; tổng = tổng dòng; quỹ BHYT ≈ thành tiền BH x mức hưởng
  8. Truy vết: mọi resource có sources hợp lệ; tài liệu đã trích xuất nhưng không được trích dẫn
  9. Nhắc PII nếu chưa ẩn danh
Kết quả: <out>/quality_report.json + quality_report.md; mã thoát 1 nếu có error.
  python quality_check.py <clinical.json> [--out dir] [--vendor dir] [--strict]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vnlib as V  # noqa: E402

ASSETS = Path(__file__).resolve().parent.parent / "assets"
LIST_TYPES = ["documents", "organizations", "locations", "practitioners", "conditions", "observations", "specimens", "service_requests", "diagnostic_reports", "medications",
              "medication_requests", "medication_administrations", "allergies", "family_history", "clinical_impressions", "care_plans", "related_persons", "procedures"]
REF_KEYS = {"performer", "recorder", "asserter", "requester", "author", "authors", "contributors", "assessor", "individual", "party", "collector", "results_interpreter", "results", "specimen",
            "based_on", "part_of", "has_member", "derived_from", "reason_reference", "problems", "addresses", "condition", "medication", "request", "presented_form", "entries", "coverage",
            "primary_care_facility", "managing_organization", "organization", "part_of", "service_provider", "department", "location", "assigner", "custodian", "account", "reference", "sources"}
PLAUSIBLE = {"8867-4": (20, 250), "8310-5": (30, 45), "9279-1": (4, 80), "2708-6": (50, 100), "29463-7": (1, 300), "8302-2": (30, 250), "39156-5": (8, 80), "8480-6": (50, 300), "8462-4": (20, 200)}


class Q:
    def __init__(self, clin: dict):
        self.c = clin
        self.issues: list[dict] = []
        self.ids: dict[str, str] = {}
        self.verified = V.read_json(ASSETS / "loinc_verified.json") if (ASSETS / "loinc_verified.json").exists() else {}
        self.stats = Counter()

    def add(self, sev, where, msg, key):
        self.issues.append({"severity": sev, "where": where, "message": msg, "key": key})

    # 1 ---------------------------------------------------------------
    def schema(self):
        try:
            import jsonschema
        except ImportError:
            self.add("warning", "meta", "jsonschema chưa cài — bỏ qua kiểm tra schema (pip install jsonschema)", "schema")
            return
        sch = V.read_json(ASSETS / "clinical.schema.json")
        for e in sorted(jsonschema.Draft202012Validator(sch).iter_errors(self.c), key=lambda e: list(e.path)):
            self.add("error", "/".join(str(x) for x in e.path) or "<root>", f"schema: {e.message[:200]}", "schema")

    # 2 ---------------------------------------------------------------
    def registry(self):
        def reg(id_, typ, where):
            if not id_:
                return
            if id_ in self.ids:
                self.add("error", where, f"id trùng: {id_} ({self.ids[id_]} và {typ})", "id-duplicate")
            self.ids[id_] = typ
        for t in LIST_TYPES:
            for i, x in enumerate(self.c.get(t) or []):
                reg(x.get("id"), t, f"{t}[{i}]")
                for r in x.get("roles", []) or []:
                    reg(r.get("id") or f"{x.get('id')}-role", "roles", f"{t}[{i}].roles")
        for k in ("patient", "coverage", "encounter"):
            if self.c.get(k):
                reg(self.c[k].get("id"), k, k)
        cs = self.c.get("cost_statement")
        if cs:
            for k, v in (cs.get("ids") or {}).items():
                reg(v, "cost_statement", "cost_statement.ids")
            slug = V.slug(cs.get("ma_lk", ""))
            for k in ("claim", "invoice", "account", "composition"):
                self.ids.setdefault((cs.get("ids") or {}).get(k) or f"{k}-{slug}", "cost_statement")
        for key, comp in (self.c.get("compositions") or {}).items():
            reg(comp.get("id") or f"composition-{key}", "compositions", f"compositions.{key}")
        self.doc_ids = {d["id"] for d in self.c.get("documents") or []}

    def references(self):
        def walk(node, path, parent_key=None):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k == "department" and path.startswith("cost_statement"):
                        continue  # trên dòng bảng kê, department là mã vn-department-cs, không phải tham chiếu
                    if k in REF_KEYS:
                        for ref in (v if isinstance(v, list) else [v]):
                            if isinstance(ref, str) and ref and ref not in self.ids:
                                if k == "sources" and ref in self.doc_ids:
                                    continue
                                self.add("error", f"{path}.{k}", f"tham chiếu không tồn tại: {ref}", "reference")
                            elif isinstance(ref, dict):
                                walk(ref, f"{path}.{k}", k)
                    else:
                        walk(v, f"{path}.{k}", k)
            elif isinstance(node, list):
                for i, x in enumerate(node):
                    walk(x, f"{path}[{i}]", parent_key)
        for t in LIST_TYPES + ["patient", "coverage", "encounter", "cost_statement", "compositions"]:
            if self.c.get(t) is not None:
                walk(self.c[t], t)

    # 3 ---------------------------------------------------------------
    def codings(self):
        unmapped_texts: list[str] = []

        def check_coding(co, where):
            sysk, code = co.get("system"), co.get("code")
            if not sysk or not code:
                self.add("error", where, "coding thiếu system/code", "coding")
                return
            self.stats["codings"] += 1
            if co.get("source") == "llm" and co.get("verified") is not True:
                self.add("error", where, f"mã do LLM đề xuất chưa được người duyệt xác nhận: {sysk}|{code}", "coding-llm-unverified")
            elif co.get("verified") is False:
                self.add("warning", where, f"coding đánh dấu verified=false: {sysk}|{code}", "coding-unverified")
            url = V.system_url(sysk)
            if sysk == "loinc" or url == V.SYS["loinc"]:
                if not V.loinc_check_digit(code):
                    self.add("error", where, f"LOINC {code} sai check digit", "loinc-checkdigit")
                elif code in self.verified:
                    ent = self.verified[code]
                    if ent is None:
                        self.add("error", where, f"LOINC {code} không tồn tại (NLM)", "loinc-nonexistent")
                    elif (ent.get("status") or "").upper() == "DEPRECATED":
                        self.add("warning", where, f"LOINC {code} đã deprecated: {ent.get('display')}", "loinc-deprecated")
                else:
                    self.add("info", where, f"LOINC {code} chưa có trong bảng đã kiểm — `term_lookup.py loinc {code} --online`", "loinc-unverified")
            elif sysk in ("icd10vn", "icd10who") or url in (V.SYS["icd10vn"], V.SYS["icd10who"]):
                if not V.icd10_lookup(code):
                    self.add("error", where, f"ICD-10 {code} không có trong từ điển ICD-10 VN", "icd10")
            elif sysk == "cls" or url == V.SYS["cls"]:
                if not any(r["code"] == code for r in V._cls_rows()):
                    self.add("error", where, f"mã CLS {code} không có trong QĐ 1227", "cls")
            elif url.startswith(V.VN + "/CodeSystem/"):
                cs_id = url.rsplit("/", 1)[-1]
                if V.cs_display(cs_id, code) is None:
                    self.add("error", where, f"mã {code} không có trong {cs_id}", "vn-cs")
            elif sysk == "ucum" or url == V.SYS["ucum"]:
                if code not in set(V.UCUM.values()) and not re.match(r"^[\w\[\]\{\}\*/\.%\-\^0-9]+$", code):
                    self.add("warning", where, f"UCUM lạ: {code}", "ucum")

        def walk(node, path):
            if isinstance(node, dict):
                if "codings" in node and isinstance(node["codings"], list):
                    if not node["codings"] and node.get("text"):
                        unmapped_texts.append(f"{path}: {node['text'][:80]}")
                    for i, co in enumerate(node["codings"]):
                        check_coding(co, f"{path}.codings[{i}]")
                for k, v in node.items():
                    if k != "codings":
                        walk(v, f"{path}.{k}")
            elif isinstance(node, list):
                for i, x in enumerate(node):
                    walk(x, f"{path}[{i}]")
        walk({k: v for k, v in self.c.items() if k not in ("conflicts", "unmapped", "review_notes", "meta")}, "")
        # quantity ucum
        def walk_q(node, path):
            if isinstance(node, dict):
                if "quantity" in node and isinstance(node["quantity"], dict):
                    q = node["quantity"]
                    if not q.get("ucum"):
                        self.add("warning", f"{path}.quantity", f"thiếu mã UCUM cho đơn vị '{q.get('unit_raw')}'", "ucum-missing")
                for k, v in node.items():
                    walk_q(v, f"{path}.{k}")
            elif isinstance(node, list):
                for i, x in enumerate(node):
                    walk_q(x, f"{path}[{i}]")
        walk_q(self.c.get("observations") or [], "observations")
        self.stats["unmapped_text_codes"] = len(unmapped_texts)
        self.unmapped_texts = unmapped_texts
        # enumerated code fields on resources
        enc = self.c.get("encounter") or {}
        for fld, cs_id in (("referral_mode", "vn-referral-mode-cs"), ("treatment_outcome", "vn-treatment-outcome-cs"), ("discharge_disposition", "vn-discharge-disposition-cs"), ("payment_method", "vn-payment-method-cs")):
            if enc.get(fld) and V.cs_display(cs_id, enc[fld]) is None:
                self.add("error", f"encounter.{fld}", f"mã {enc[fld]} không có trong {cs_id}", "vn-cs")
        if (enc.get("type") or {}).get("code") and V.cs_display("vn-encounter-type-cs", enc["type"]["code"]) is None:
            self.add("error", "encounter.type", f"mã {enc['type']['code']} không có trong vn-encounter-type-cs", "vn-cs")
        for o in self.c.get("organizations") or []:
            for fld, cs_id in (("org_type", "vn-organization-type-cs"), ("org_level", "vn-healthcare-level-cs"), ("facility_care_level", "vn-facility-care-level-cs"), ("department_code", "vn-department-cs")):
                if o.get(fld) and V.cs_display(cs_id, o[fld]) is None:
                    self.add("error", f"organizations[{o['id']}].{fld}", f"mã {o[fld]} không có trong {cs_id}", "vn-cs")
        for p in self.c.get("practitioners") or []:
            for q in p.get("qualifications") or []:
                if V.cs_display("vn-qualification-cs", q) is None:
                    self.add("error", f"practitioners[{p['id']}].qualifications", f"mã {q} không có trong vn-qualification-cs", "vn-cs")
            for r in p.get("roles") or []:
                for sp in r.get("specialty") or []:
                    if V.cs_display("vn-specialty-cs", sp) is None:
                        self.add("error", f"practitioners[{p['id']}].roles.specialty", f"mã {sp} không có trong vn-specialty-cs", "vn-cs")
        pt = self.c.get("patient") or {}
        if (pt.get("ethnicity") or {}).get("code") and V.cs_display("vn-ethnicity-cs", pt["ethnicity"]["code"]) is None:
            self.add("error", "patient.ethnicity", "mã dân tộc không có trong vn-ethnicity-cs", "vn-cs")
        addr = pt.get("address") or {}
        if addr.get("province_code") and V.cs_display("vn-province-cs", addr["province_code"]) is None:
            self.add("error", "patient.address.province_code", "mã tỉnh không có trong vn-province-cs", "vn-cs")
        if addr.get("ward_code") and V.cs_display("vn-ward-cs", addr["ward_code"]) is None:
            self.add("error", "patient.address.ward_code", "mã xã không có trong vn-ward-cs", "vn-cs")
        cs = self.c.get("cost_statement") or {}
        for i, ln in enumerate(cs.get("lines") or []):
            if ln.get("category") and V.cs_display("vn-cost-category-cs", ln["category"]) is None:
                self.add("error", f"cost_statement.lines[{i}].category", f"nhóm chi phí {ln['category']} không có trong vn-cost-category-cs", "vn-cs")
            if ln.get("department") and V.cs_display("vn-department-cs", ln["department"]) is None:
                self.add("error", f"cost_statement.lines[{i}].department", f"mã khoa {ln['department']} không có trong vn-department-cs", "vn-cs")
        if cs.get("claim_subtype") and V.cs_display("vn-patient-visit-type-cs", cs["claim_subtype"]) is None:
            self.add("error", "cost_statement.claim_subtype", "không có trong vn-patient-visit-type-cs", "vn-cs")

    # 4+5 -------------------------------------------------------------
    def observations(self):
        for o in self.c.get("observations") or []:
            w = f"observations[{o['id']}]"
            codes = [c["code"] for c in (o.get("code") or {}).get("codings", []) if c.get("system") == "loinc"]
            if o.get("kind") == "vital-sign":
                if not codes or codes[0] not in V.VITAL_BY_LOINC:
                    self.add("error", w, "sinh hiệu phải có LOINC thuộc bảng VITALS (term_lookup.py vital)", "vital-code")
                    continue
                key = V.VITAL_BY_LOINC[codes[0]]
                _, _, _, uc, label, _ = V.VITALS[key]
                q = (o.get("value") or {}).get("quantity")
                if q:
                    if q.get("ucum") != uc or q.get("unit") != label:
                        self.add("error", w, f"đơn vị sinh hiệu phải là ucum={uc} unit='{label}' (profile fixedString); có {q.get('ucum')}/{q.get('unit')}", "vital-unit")
                    lo, hi = PLAUSIBLE.get(codes[0], (None, None))
                    if lo is not None and not (lo <= q["value"] <= hi):
                        self.add("warning", w, f"giá trị {q['value']} ngoài khoảng sinh lý [{lo},{hi}] — kiểm tra lại phiếu", "vital-range")
                for comp in o.get("components") or []:
                    cc = [c["code"] for c in comp["code"].get("codings", []) if c.get("system") == "loinc"]
                    q = (comp.get("value") or {}).get("quantity") or {}
                    lo, hi = PLAUSIBLE.get(cc[0] if cc else "", (None, None))
                    if lo is not None and q and not (lo <= q["value"] <= hi):
                        self.add("warning", w, f"thành phần {cc[0]} giá trị {q['value']} ngoài [{lo},{hi}]", "vital-range")
                    if q and (q.get("ucum") != "mm[Hg]" or q.get("unit") != "mmHg"):
                        self.add("error", w, "huyết áp: ucum=mm[Hg], unit='mmHg' (fixedString)", "vital-unit")
            if o.get("kind") == "lab":
                q = (o.get("value") or {}).get("quantity")
                rr = o.get("reference_range") or {}
                if rr.get("low") is not None and rr.get("high") is not None and rr["low"] >= rr["high"]:
                    self.add("warning", w, f"khoảng tham chiếu low>=high: {rr}", "lab-range")
                if q and rr.get("low") is not None and rr.get("high") is not None:
                    flag = (o.get("interpretation") or [None])[0]
                    expect = "H" if q["value"] > rr["high"] else "L" if q["value"] < rr["low"] else None
                    if expect != flag and flag in ("H", "L", None):
                        self.add("warning", w, f"cờ {flag} không khớp giá trị {q['value']} so với tham chiếu [{rr['low']}, {rr['high']}] (mong đợi {expect})", "lab-flag")
                if not o.get("specimen"):
                    self.add("info", w, "xét nghiệm không gắn specimen", "lab-specimen")

    # 6 ---------------------------------------------------------------
    def timeline(self):
        enc = self.c.get("encounter") or {}
        per = enc.get("period") or {}
        try:
            start = datetime.fromisoformat(per["start"]); end = datetime.fromisoformat(per.get("end") or per["start"])
        except Exception:
            self.add("error", "encounter.period", "không đọc được period", "timeline"); return
        if end < start:
            self.add("error", "encounter.period", "ra viện trước vào viện", "timeline")
        lo, hi = start - timedelta(days=1), end + timedelta(days=2)

        def parse(s):
            try:
                d = datetime.fromisoformat(s)
                return d if d.tzinfo else d.replace(tzinfo=start.tzinfo)
            except Exception:
                return None
        def chk(val, where, allow_after_end_days=2):
            d = parse(val) if isinstance(val, str) else None
            if d and not (lo <= d <= end + timedelta(days=allow_after_end_days)):
                self.add("warning", where, f"mốc thời gian {val} ngoài đợt điều trị [{per['start']} .. {per.get('end')}]", "timeline")
        for d in self.c.get("documents") or []:
            chk(d.get("date"), f"documents[{d['id']}].date", 30)
        for t, flds in (("observations", ("effective", "issued")), ("service_requests", ("authored_on",)), ("diagnostic_reports", ("effective", "issued")), ("medication_requests", ("authored_on",)),
                        ("medication_administrations", ("effective",)), ("conditions", ("recorded_date",)), ("clinical_impressions", ("date",))):
            for x in self.c.get(t) or []:
                for f in flds:
                    chk(x.get(f), f"{t}[{x['id']}].{f}")
                if t in ("observations", "diagnostic_reports") and x.get("effective") and x.get("issued"):
                    a, b = parse(x["effective"]), parse(x["issued"])
                    if a and b and b < a:
                        self.add("warning", f"{t}[{x['id']}]", "issued trước effective", "timeline")
        pt = self.c.get("patient") or {}
        if pt.get("birth_date") and pt["birth_date"] > per["start"][:10]:
            self.add("error", "patient.birth_date", "ngày sinh sau ngày vào viện", "timeline")
        cov = self.c.get("coverage") or {}
        cp = cov.get("period") or {}
        if cp.get("start") and cp["start"] > per["start"][:10] or cp.get("end") and cp["end"] < per["start"][:10]:
            self.add("warning", "coverage.period", "thẻ BHYT không phủ ngày vào viện", "timeline")
        cs = self.c.get("cost_statement") or {}
        if cs and (cs.get("period") or {}).get("start") and cs["period"]["start"][:16] != per["start"][:16]:
            self.add("info", "cost_statement.period", "kỳ bảng kê khác period của Encounter", "timeline")

    # 7 ---------------------------------------------------------------
    def invoice(self):
        cs = self.c.get("cost_statement")
        if not cs:
            return
        lines = cs.get("lines") or []
        seqs = [ln["sequence"] for ln in lines]
        if len(seqs) != len(set(seqs)):
            self.add("error", "cost_statement.lines", "sequence trùng", "invoice")
        segs = {s["sequence"]: s for s in cs.get("segments") or []}
        tot = Counter()
        for ln in lines:
            w = f"cost_statement.lines[{ln['sequence']}]"
            if ln.get("segment") not in segs:
                self.add("error", w, f"segment {ln.get('segment')} không tồn tại", "invoice")
            exp = round(ln["quantity"] * ln["hospital_unit_price"])
            if abs(exp - ln["hospital_amount"]) > 1:
                self.add("error", w, f"thành tiền BV {ln['hospital_amount']} ≠ SL x đơn giá = {exp}", "invoice-amount")
            al = ln["allocation"]
            s = al["insurance_fund"] + al["patient_copay"] + al["other_source"] + al["patient_self_pay"]
            if abs(s - ln["hospital_amount"]) > 1:
                self.add("error", w, f"phân bổ {s} ≠ thành tiền {ln['hospital_amount']}", "invoice-allocation")
            if ln.get("insurance_amount") is not None and ln["insurance_amount"] > ln["hospital_amount"] + 1:
                self.add("warning", w, "thành tiền BH > thành tiền BV", "invoice")
            seg = segs.get(ln.get("segment")) or {}
            bl = ln.get("benefit_level") or seg.get("benefit_level")
            if bl and ln.get("insurance_amount") and ln.get("scope", "1") == "1":
                exp_fund = round(ln["insurance_amount"] * (ln.get("insurance_payment_ratio", 100) / 100) * bl / 100)
                if abs(exp_fund - al["insurance_fund"]) > 1:
                    self.add("warning", w, f"quỹ BHYT {al['insurance_fund']} ≠ thành tiền BH x tỷ lệ x mức hưởng = {exp_fund}", "invoice-fund")
            tot["hospital_total"] += ln["hospital_amount"]; tot["insurance_total"] += ln.get("insurance_amount") or 0
            tot["insurance"] += al["insurance_fund"]; tot["patient_copay"] += al["patient_copay"]; tot["other_fund"] += al["other_source"]; tot["patient_self_pay"] += al["patient_self_pay"]
            if str(ln.get("category", "")).startswith("8"):
                tot["medication"] += ln["hospital_amount"]
            if str(ln.get("category", "")).startswith("6"):
                tot["supplies"] += ln["hospital_amount"]
        T = cs.get("totals") or {}
        for k in ("hospital_total", "insurance_total", "insurance", "patient_copay", "other_fund", "patient_self_pay", "medication", "supplies"):
            if k in T and abs(T[k] - tot[k]) > 1:
                self.add("error", f"cost_statement.totals.{k}", f"tổng {T[k]} ≠ tổng các dòng {tot[k]}", "invoice-total")
        roles = [a.get("role") for a in cs.get("attestations") or []]
        if cs.get("composition_status") == "final" and len(set(roles)) < 3:
            self.add("error", "cost_statement.composition_status", "final cần đủ 3 chữ ký (preparer, billing-confirmation, patient-confirmation)", "invoice-signatures")

    # 8 ---------------------------------------------------------------
    def traceability(self):
        cited = Counter()
        for t in LIST_TYPES + ["patient", "coverage", "encounter", "cost_statement"]:
            items = self.c.get(t)
            items = items if isinstance(items, list) else ([items] if items else [])
            for x in items:
                if t == "documents":
                    continue
                src = x.get("sources") or []
                if not src:
                    self.add("error", f"{t}[{x.get('id', '?')}]", "thiếu sources (truy vết Provenance)", "sources")
                for s in src:
                    if s not in self.doc_ids:
                        self.add("error", f"{t}[{x.get('id', '?')}].sources", f"tài liệu {s} không có trong documents", "sources")
                    cited[s] += 1
        for d in self.c.get("documents") or []:
            if d.get("extracted") and not cited[d["id"]]:
                self.add("info", f"documents[{d['id']}]", "tài liệu đã trích xuất nhưng không resource nào trích dẫn", "sources-uncited")
        self.stats["documents_cited"] = len(cited)

    def pii(self):
        pt = self.c.get("patient") or {}
        if not (self.c.get("meta") or {}).get("deidentified") and any(i.get("type") in ("CCCD", "CMND", "BHYT", "HC") for i in pt.get("identifiers") or []):
            self.add("info", "patient", "chứa dữ liệu cá nhân thật (CCCD/BHYT…) — chỉ dùng nội bộ; chạy deidentify.py trước khi chia sẻ (PDPL 2023)", "pii")

    def run(self):
        self.schema(); self.registry(); self.references(); self.codings(); self.observations(); self.timeline(); self.invoice(); self.traceability(); self.pii()
        seen, uniq = set(), []
        for i in self.issues:
            k = (i["severity"], i["where"], i["message"])
            if k not in seen:
                seen.add(k); uniq.append(i)
        self.issues = uniq
        cnt = Counter(i["severity"] for i in self.issues)
        return {"case": (self.c.get("meta") or {}).get("case_id"), "errors": cnt["error"], "warnings": cnt["warning"], "infos": cnt["info"],
                "counts": {t: len(self.c.get(t) or []) for t in LIST_TYPES}, "stats": dict(self.stats), "conflicts": len(self.c.get("conflicts") or []), "unmapped": len(self.c.get("unmapped") or []),
                "review_notes": len(self.c.get("review_notes") or []), "unmapped_text_codes": self.unmapped_texts, "issues": self.issues}


def write_md(rep: dict, out: Path):
    md = [f"# Kiểm tra chất lượng clinical v2 — {rep['case']}", "", f"**Lỗi: {rep['errors']}** · cảnh báo: {rep['warnings']} · info: {rep['infos']}", "",
          "Số lượng: " + ", ".join(f"{k} {v}" for k, v in rep["counts"].items() if v), f"Conflicts: {rep['conflicts']} · Unmapped: {rep['unmapped']} · Review notes: {rep['review_notes']} · Codings: {rep['stats'].get('codings', 0)} · Text-only codes: {len(rep['unmapped_text_codes'])}", "",
          "| Mức | Vị trí | Thông điệp |", "|---|---|---|"]
    md += [f"| {i['severity']} | {i['where']} | {i['message']} |" for i in rep["issues"]]
    if rep["unmapped_text_codes"]:
        md += ["", "## Khái niệm chỉ có text (chưa mã hoá) — tra cứu lại nếu danh mục có", ""] + [f"- {x}" for x in rep["unmapped_text_codes"]]
    (out / "quality_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clinical")
    ap.add_argument("--out")
    ap.add_argument("--vendor")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    V.find_vendor(a.vendor)
    clin = V.read_json(a.clinical)
    rep = Q(clin).run()
    out = Path(a.out) if a.out else Path(a.clinical).resolve().parent
    out.mkdir(parents=True, exist_ok=True)
    V.write_json(rep, out / "quality_report.json")
    write_md(rep, out)
    if not a.quiet:
        for i in rep["issues"]:
            if i["severity"] != "info":
                print(f"{i['severity']:7} {i['where']}: {i['message']}")
    print(f"[quality_check] lỗi: {rep['errors']}, cảnh báo: {rep['warnings']}, info: {rep['infos']} → {out / 'quality_report.md'}")
    return 1 if rep["errors"] or (a.strict and rep["warnings"]) else 0


if __name__ == "__main__":
    sys.exit(main())
