"""Bước 4 — gộp kết quả các nhiệm vụ thành một clinical.json v2 và chuẩn hoá xác định (không LLM).

Gộp: khối thực thể (nhân viên y tế, tổ chức, buồng/giường, chẩn đoán, thuốc, mẫu…) trùng id hoặc trùng tên => một phần tử, gộp sources;
     resource sự kiện trùng id => đổi id. Sau đó khử trùng theo tên chuẩn hoá và ánh xạ lại mọi tham chiếu.
Chuẩn hoá: id hợp lệ, ngày giờ ISO (+07:00), họ tên tách family/given, học hàm theo vn-qualification-cs, mã do LLM tự ghi bị loại
(chỉ giữ ICD-10 in trên phiếu đã kiểm), mã danh mục VN Core sai bị bỏ, tham chiếu treo bị gỡ (ghi review_notes), số dạng "50.600" -> 50600.
"""
from __future__ import annotations

import copy
import re

LIST_BLOCKS = ["organizations", "locations", "practitioners", "conditions", "observations", "specimens", "service_requests", "diagnostic_reports",
               "medications", "medication_requests", "medication_administrations", "allergies", "family_history", "clinical_impressions", "care_plans"]
SINGLE_BLOCKS = ["patient", "coverage", "encounter", "cost_statement"]
ENTITY_BLOCKS = {"organizations", "locations", "practitioners", "conditions", "medications", "specimens", "service_requests", "allergies", "family_history"}
PREFIX = {"organizations": "org", "locations": "loc", "practitioners": "pr", "conditions": "cond", "observations": "obs", "specimens": "spc",
          "service_requests": "sr", "diagnostic_reports": "dr", "medications": "med", "medication_requests": "mr", "medication_administrations": "ma",
          "allergies": "ai", "family_history": "fmh", "clinical_impressions": "ci", "care_plans": "cp"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{0,63}$")
ISO_DT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?([+-]\d{2}:\d{2}|Z)$")
ISO_D = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATE_KEYS = {"effective", "issued", "authored_on", "recorded_date", "date", "received", "collected", "created", "prepared_at", "start", "end",
             "occurrence", "time", "onset", "birth_date"}
DATE_ONLY_KEYS = {"birth_date"}
NUM_KEYS = {"low", "high", "hospital_unit_price", "insurance_unit_price", "hospital_amount", "insurance_amount", "insurance_fund", "patient_copay",
            "other_source", "patient_self_pay", "benefit_level", "service_payment_ratio", "insurance_payment_ratio", "length_days", "rank",
            "sequence", "segment", "frequency", "hospital_total", "insurance_total", "insurance", "other_fund", "non_covered", "medication", "supplies"}
# mảnh học hàm/chức danh còn dính đầu họ tên sau parse_person ("&cki Phạm…", "Bs. Nguyễn…")
TITLE_WORDS = set("bác sĩ chuyên khoa điều dưỡng viên kỹ thuật kĩ trưởng phó phòng thạc tiến y tá dược cử nhân người ký "
                  "thực hiện lập phiếu đọc kết quả điều trị khám bệnh giám đốc thư chủ tọa ckii cki bs bsckii bscki ths ts ktv".split())
TITLE_LEFTOVER = re.compile(r"^(?:[&.\-\s:]*(?:CKII|CKI|BSCKII|BSCKI|BSNT|BS|ThS|TS|PGS|GS|KTV|ĐD|DD|CNĐD|CN|DS|YS)(?![^\W\d_]))+[&.\-\s:]*", re.I)
SPECIMEN_V2 = {"BLD", "SER", "PLAS", "UR", "CSF", "SPT", "STL", "SWAB"}
SPECIMEN_KW = [("huyet thanh", "SER"), ("huyet tuong", "PLAS"), ("nuoc tieu", "UR"), ("dich nao tuy", "CSF"), ("dom", "SPT"), ("phan", "STL"),
               ("dich phet", "SWAB"), ("swab", "SWAB"), ("mau", "BLD")]
EXTRA_REF_KEYS = {"medication", "request", "requests", "individual", "location", "managing_organization", "primary_care_facility", "authenticator",
                  "author_org", "encounter", "insurer", "invoice", "prepared_by", "authors"}   # = mọi khoá build_fhir gọi ref()/refs()


class Assembler:
    def __init__(self, kb, meta: dict, documents: list[dict]):
        self.kb, self.V = kb, kb.V
        self.state: dict = {"meta": meta, "documents": copy.deepcopy(documents)}
        for b in LIST_BLOCKS:
            self.state[b] = []
        self.state.update({"compositions": {}, "conflicts": [], "unmapped": [], "review_notes": []})
        self.enc_dx: list[dict] = []
        self.origin: dict[str, str] = {}      # id -> task đã tạo (để truy vết / sửa)
        self.ref_keys = set(kb.qc.REF_KEYS) | EXTRA_REF_KEYS

    # ------------------------------------------------------------------ gộp
    def merge(self, task, data: dict):
        if not isinstance(data, dict) or data.get("_error"):
            return
        data = copy.deepcopy(data)
        default_src = list(task.docs)
        for b in SINGLE_BLOCKS:
            v = data.get(b)
            if isinstance(v, dict) and v:
                v.setdefault("sources", default_src)
                if not self.state.get(b):
                    self.state[b] = v
                    if v.get("id"):
                        self.origin[v["id"]] = task.name
                else:
                    _fill(self.state[b], v)
        for b in LIST_BLOCKS:
            for i, x in enumerate(data.get(b) or []):
                if not isinstance(x, dict):
                    continue
                if not x.get("id"):
                    x["id"] = f"{PREFIX[b]}-{self.V.slug(task.name)}-{i + 1}"
                x.setdefault("sources", default_src)
                exist = next((y for y in self.state[b] if y.get("id") == x["id"]), None)
                if exist is not None and b in ENTITY_BLOCKS:
                    _fill(exist, x)
                    continue
                if exist is not None or self._id_taken(x["id"]):
                    base, n = x["id"], 2
                    while self._id_taken(f"{base}-{n}"):
                        n += 1
                    x["id"] = f"{base}-{n}"
                self.state[b].append(x)
                self.origin[x["id"]] = task.name
        for k, comp in (data.get("compositions") or {}).items():
            if isinstance(comp, dict):
                comp.setdefault("sources", default_src)
                self.state["compositions"][k] = comp
                if comp.get("id"):
                    self.origin[comp["id"]] = task.name
        for dx in data.get("encounter_diagnoses") or []:
            if isinstance(dx, dict) and dx.get("condition"):
                self.enc_dx.append(dx)
        for p in data.get("documents_patch") or []:
            d = next((d for d in self.state["documents"] if d["id"] == (p or {}).get("id")), None)
            if not d:
                continue
            for k in ("title", "date", "authors", "author_org"):
                if p.get(k) and (not d.get(k) or k == "date" and d.get(k) is None):
                    d[k] = p[k]
            if p.get("extracted") is False and d["id"] in task.docs:
                d["extracted"] = False
            if p.get("note") and p["note"] not in (d.get("note") or ""):
                d["note"] = (d.get("note") + "; " if d.get("note") else "") + p["note"]
        for k in ("conflicts", "unmapped"):
            for x in data.get(k) or []:
                if isinstance(x, dict) and x not in self.state[k]:
                    self.state[k].append(x)
        for x in data.get("review_notes") or []:
            if isinstance(x, str) and x not in self.state["review_notes"]:
                self.state["review_notes"].append(x)

    @classmethod
    def from_state(cls, kb, clin: dict) -> "Assembler":
        """Dựng lại từ một clinical đã gộp (vòng sửa) để chạy lại finalize."""
        a = cls(kb, clin["meta"], clin.get("documents") or [])
        st = copy.deepcopy(clin)
        for b in LIST_BLOCKS:
            st[b] = st.get(b) or []
        for k in ("conflicts", "unmapped", "review_notes"):
            st[k] = st.get(k) or []
        st["compositions"] = st.get("compositions") or {}
        a.state = st
        return a

    def interim(self):
        """Giữa các đợt: id hợp lệ + khử trùng thực thể để REGISTRY gửi cho đợt sau gọn và nhất quán."""
        s, V = self.state, self.V
        mapping = {}
        for b in LIST_BLOCKS:
            for x in s[b]:
                new = _fix_id(V, x["id"])
                if new != x["id"]:
                    mapping[x["id"]] = new
                    x["id"] = new
        self.remap(mapping)
        self._dedupe("practitioners", lambda p: V.norm(V.parse_person(p.get("name_raw") or (p.get("name") or {}).get("text") or "")["name"] or p["id"]))
        self._dedupe("organizations", lambda o: V.norm(o.get("name") or o["id"]))
        self._dedupe("locations", lambda x: V.norm(x.get("name") or x["id"]))
        self._dedupe("conditions", lambda c: (c.get("role"), V.norm(c.get("text") or c["id"])))

    def _id_taken(self, id_):
        if any(x.get("id") == id_ for b in LIST_BLOCKS for x in self.state[b]):
            return True
        return any((self.state.get(b) or {}).get("id") == id_ for b in SINGLE_BLOCKS)

    # ------------------------------------------------------------------ khử trùng + chuẩn hoá
    def finalize(self, facility: dict) -> dict:
        s = self.state
        V = self.V
        # id hợp lệ
        mapping = {}
        for b in LIST_BLOCKS:
            for x in s[b]:
                new = _fix_id(V, x["id"])
                if new != x["id"]:
                    mapping[x["id"]] = new
                    x["id"] = new
        for b in SINGLE_BLOCKS:
            if s.get(b) and s[b].get("id"):
                new = _fix_id(V, s[b]["id"])
                if new != s[b]["id"]:
                    mapping[s[b]["id"]] = new
                    s[b]["id"] = new
        self.remap(mapping)
        # practitioners: tên chuẩn, học hàm, khử trùng theo tên
        for p in s["practitioners"]:
            raw = p.get("name_raw") or (p.get("name") or {}).get("text") or ""
            pp = V.parse_person(re.sub(r"(?i)\b(BS|Bs)\s*[&.]\s*(CKII|CKI)\b", r"\1\2", raw))     # "BS&CKI", "BS.CKII" -> BSCKI/BSCKII
            name = pp["name"] or (p.get("name") or {}).get("text") or raw
            name = TITLE_LEFTOVER.sub("", name).strip() or name
            fam, giv = V.split_name(name)
            p["name"] = {"text": name, "family": fam, "given": giv}
            quals = list(dict.fromkeys([q for q in (p.get("qualifications") or []) + pp["titles"] if V.cs_display("vn-qualification-cs", q)]))
            if quals:
                p["qualifications"] = quals
            else:
                p.pop("qualifications", None)
            if not p.get("name_raw"):
                p["name_raw"] = raw
        # "BÁC SĨ Chuyên Khoa" (khối chữ ký) không phải họ tên: tên chỉ gồm từ chức danh -> bỏ, drop_dangling gỡ tham chiếu
        junk = [p for p in s["practitioners"] if p["name"]["text"]
                and set(V.norm(p["name"]["text"]).split()) <= TITLE_WORDS]
        for p in junk:
            s["review_notes"].append(f"practitioner: bỏ '{p['name']['text']}' (chức danh, không phải họ tên)")
        s["practitioners"] = [p for p in s["practitioners"] if p not in junk]
        # id sinh từ token che PII (pr-p1, pr-pii-p1) -> id theo tên thật đã trả lại
        rn = {}
        for p in s["practitioners"]:
            if re.fullmatch(r"pr-(?:pii-)?p\d+(?:-.*)?", p["id"]) and p["name"].get("text"):
                new = _fix_id(V, "pr-" + V.slug(p["name"]["text"]))
                if not self._id_taken(new):
                    rn[p["id"]] = new
                    p["id"] = new
        self.remap(rn)
        self._dedupe("practitioners", lambda p: V.norm(p["name"]["text"]))
        # vai trò: bỏ trùng, id vai trò duy nhất toàn hồ sơ
        seen_roles = set()
        for p in s["practitioners"]:
            roles = []
            for r in p.get("roles") or []:
                if isinstance(r, dict) and r not in roles:
                    roles.append(r)
            for i, r in enumerate(roles):
                rid = r.get("id") or f"{p['id']}-role-{i + 1}"
                while rid in seen_roles:
                    rid = f"{p['id']}-role-{i + 1}-{len(seen_roles)}"
                r["id"] = rid
                seen_roles.add(rid)
            if roles:
                p["roles"] = roles
        self._dedupe("organizations", lambda o: V.norm(o.get("name") or ""))
        self._dedupe("locations", lambda x: V.norm(x.get("name") or ""))
        self._dedupe("conditions", lambda c: (c.get("role"), V.norm(c.get("text") or "")))
        self._dedupe("medications", lambda m: V.norm((m.get("code") or {}).get("text") or ""))
        self._dedupe("specimens", lambda x: next((i.get("value") for i in x.get("identifiers") or [] if i.get("value")), x["id"]))
        self._dedupe("allergies", lambda a: ("nka" if a.get("nka") else V.norm(a.get("text") or a["id"])))
        # cơ sở KCB chính
        fac_id = facility.get("organization")
        orgs = s["organizations"]
        if not fac_id or not any(o["id"] == fac_id for o in orgs):
            cand = [o for o in orgs if o.get("kind") == "facility"]
            cand.sort(key=lambda o: -len(o.get("sources") or []))
            if fac_id and cand:
                self.remap({cand[0]["id"]: fac_id})
                cand[0]["id"] = fac_id
            elif cand:
                fac_id = cand[0]["id"]
        s["meta"]["facility"]["organization"] = fac_id or "org-unknown"
        # mã CSKCB từ cấu hình (khi phiếu không in) -> định danh bắt buộc của Organization cơ sở KCB
        fo = next((o for o in orgs if o["id"] == fac_id), None)
        if fo is not None and facility.get("ma_cskcb") and not any((i or {}).get("value") for i in fo.get("identifiers") or []):
            fo["identifiers"] = [{"type": "CSKCB", "value": str(facility["ma_cskcb"]), "text": "Mã cơ sở KCB (cấu hình)"}]
            s["review_notes"].append("organizations: mã CSKCB lấy từ cấu hình facility.ma_cskcb (không in trên phiếu)")
        elif fo is not None and not any((i or {}).get("value") for i in fo.get("identifiers") or []):
            # Organization VN Core cần ≥1 identifier: định danh nội bộ tạm (cùng quy ước ground truth), ghi rõ để người duyệt thay mã CSKCB
            slug = fac_id.removeprefix("org-")
            base = (facility.get("base_url") or f"http://his.{slug}.vn/fhir").removesuffix("/fhir")
            fo["identifiers"] = [{"type": "LOCAL", "value": slug.upper(), "system": f"{base}/sid/organization",
                                  "text": "Mã cơ sở nội bộ (tạm — mã CSKCB chưa có trong hồ sơ)"}]
            s["review_notes"].append("organizations: phiếu không in mã CSKCB — dùng định danh nội bộ tạm; cấu hình facility.ma_cskcb để thay")
        # chẩn đoán của lượt điều trị
        enc = s.get("encounter") or {}
        if enc:
            seen = set()
            dx = []
            for d in (enc.get("diagnoses") or []) + self.enc_dx:
                c = mapping.get(d.get("condition"), d.get("condition"))
                if c and (c, d.get("use")) not in seen and d.get("use") in ("AD", "DD", "CC", "CM", "pre-op", "post-op", "billing"):
                    seen.add((c, d.get("use")))
                    dx.append({k: v for k, v in dict(d, condition=c).items() if k in ("condition", "use", "rank")})
            if dx:
                enc["diagnoses"] = dx
        # tài liệu: ngày lập
        for d in s["documents"]:
            if not d.get("date"):
                d["date"] = (enc.get("period") or {}).get("start", "")[:10] or s["meta"]["created"][:10]
                d["note"] = (d.get("note") + "; " if d.get("note") else "") + "Ngày lập phiếu không trích được — tạm dùng ngày vào viện"
        self.normalize_values()
        self.drop_dangling()
        self.ensure_required()      # sau drop_dangling: medication_code dự phòng -> Medication + tham chiếu
        self.drop_dangling()
        return s

    def _dedupe(self, block, keyf):
        keep, mapping = {}, {}
        out = []
        for x in self.state[block]:
            try:
                k = keyf(x)
            except Exception:  # noqa: BLE001
                k = x["id"]
            if k and k in keep:
                _fill(keep[k], x)
                mapping[x["id"]] = keep[k]["id"]
            else:
                keep[k] = x
                out.append(x)
        self.state[block] = out
        mapping = {a: b for a, b in mapping.items() if a != b}
        if mapping:
            self.remap(mapping)

    def remap(self, mapping: dict[str, str]):
        if not mapping:
            return

        def walk(node, key=None, in_cost=False):
            if isinstance(node, dict):
                for k, v in list(node.items()):
                    if k in self.ref_keys and not (k == "department" and in_cost):
                        if isinstance(v, str):
                            node[k] = mapping.get(v, v)
                        elif isinstance(v, list):
                            node[k] = [mapping.get(r, r) if isinstance(r, str) else walk(r, k, in_cost) or r for r in v]
                            node[k] = list(dict.fromkeys(node[k])) if all(isinstance(r, str) for r in node[k]) else node[k]
                        else:
                            walk(v, k, in_cost)
                    else:
                        walk(v, k, in_cost or k == "cost_statement")
            elif isinstance(node, list):
                for x in node:
                    walk(x, key, in_cost)
        walk({k: v for k, v in self.state.items() if k not in ("meta",)})

    # ------------------------------------------------------------------ giá trị
    def normalize_values(self):
        V = self.V
        notes = self.state["review_notes"]
        # model hay ghi tường minh null ("org_type": null) -> bỏ khoá (schema coi thiếu = không có)
        for b in LIST_BLOCKS + SINGLE_BLOCKS + ["compositions"]:
            if self.state.get(b):
                self.state[b] = _prune_none(self.state[b])

        def fix_dt(v, key):
            if not isinstance(v, str) or not v:
                return v
            v = v.strip()
            if key in DATE_ONLY_KEYS:
                if ISO_D.match(v[:10]):
                    return v[:10]
                if re.fullmatch(r"\d{4}", v):
                    return f"{v}-01-01"
            if ISO_DT.match(v):
                return v if re.search(r"([+-]\d{2}:\d{2}|Z)$", v) else v + "+07:00"
            if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$", v):
                return (v if len(v) > 16 else v + ":00") + "+07:00"
            if ISO_D.match(v):
                return v
            iso = V.vn_datetime(v)
            if iso:
                return iso[:10] if key in DATE_ONLY_KEYS else iso
            return None

        def walk(node, path):
            if isinstance(node, dict):
                for k, v in list(node.items()):
                    if k in DATE_KEYS and isinstance(v, str):
                        nv = fix_dt(v, k)
                        if nv is None:
                            node.pop(k)
                            notes.append(f"{path}.{k}: giá trị thời gian '{v}' không chuẩn hoá được — đã bỏ")
                        else:
                            node[k] = nv
                    elif isinstance(v, str) and (k in NUM_KEYS or (k == "value" and "unit_raw" in node)):
                        num = _num(v)
                        if num is not None:
                            node[k] = num
                    else:
                        walk(v, f"{path}.{k}")
            elif isinstance(node, list):
                for i, x in enumerate(node):
                    walk(x, f"{path}[{i}]")
        walk({k: v for k, v in self.state.items() if k not in ("meta", "conflicts", "unmapped", "review_notes")}, "")
        for b in ("cost_statement",):
            cs = self.state.get(b)
            if cs and not isinstance(cs, dict):
                self.state[b] = None
                cs = None
            if cs:
                cs["lines"] = [ln for ln in cs.get("lines") or [] if isinstance(ln, dict)]
                if not isinstance(cs.get("totals"), dict):
                    cs.pop("totals", None)
                for ln in cs["lines"]:
                    for k in ("quantity", "hospital_unit_price", "insurance_unit_price", "hospital_amount", "insurance_amount"):
                        if isinstance(ln.get(k), str):
                            ln[k] = _num(ln[k]) or 0
                for k, v in list((cs.get("totals") or {}).items()):
                    if isinstance(v, str):
                        cs["totals"][k] = _num(v)
        # observation: đúng một khoá value
        for o in self.state["observations"]:
            val = o.get("value")
            if isinstance(val, dict):
                keys = [k for k in ("quantity", "string", "integer", "boolean", "datetime", "codeable", "absent") if val.get(k) not in (None, "")]
                if not keys:
                    o.pop("value")
                elif len(keys) > 1:
                    o["value"] = {keys[0]: val[keys[0]]}
                else:
                    o["value"] = {keys[0]: val[keys[0]]}
                q = (o.get("value") or {}).get("quantity")
                if isinstance(q, dict) and not isinstance(q.get("value"), (int, float)):
                    num = _num(str(q.get("value", "")))
                    if num is None:
                        o["value"] = {"string": f"{q.get('value')} {q.get('unit_raw') or ''}".strip()}
                    else:
                        q["value"] = num
            elif val is not None and not isinstance(val, dict):
                o["value"] = {"string": str(val)}
            if o.get("components") and o.get("value"):
                o.pop("value")
            rr = o.get("reference_range")
            if isinstance(rr, dict):
                for k in ("low", "high"):
                    if k in rr and not isinstance(rr[k], (int, float)):
                        n = _num(str(rr[k]))
                        if n is None:
                            rr.pop(k)
                        else:
                            rr[k] = n
                if not any(rr.get(k) not in (None, "") for k in ("low", "high", "text")):
                    o.pop("reference_range")
            if o.get("interpretation"):
                o["interpretation"] = [i for i in o["interpretation"] if i in ("H", "L", "N", "A", "AA", "HH", "LL", "POS", "NEG", "IND", "<", ">")] or None
                if not o["interpretation"]:
                    o.pop("interpretation")
        # mã do LLM ghi: chỉ giữ ICD-10 kiểm chứng được (mã in trên phiếu)
        removed = []

        def strip_codes(node, path):
            if isinstance(node, dict):
                if isinstance(node.get("codings"), list):
                    keep = []
                    for c in node["codings"]:
                        if not isinstance(c, dict):
                            continue
                        if c.get("_harness"):
                            keep.append(c)
                            continue
                        sysk = (c.get("system") or "").lower()
                        if "icd" in sysk and c.get("code"):
                            code, status = V.icd10_verify(c["code"])
                            if status == "vn":
                                keep.append({"system": "icd10vn", "code": code, "display": (V.icd10_lookup(code) or {}).get("vi"), "source": "document",
                                             "verified": True, "confidence": 1.0, "_harness": True})
                                continue
                        removed.append(f"{path}: {c.get('system')}|{c.get('code')}")
                    node["codings"] = keep
                for k, v in node.items():
                    if k != "codings":
                        strip_codes(v, f"{path}.{k}")
            elif isinstance(node, list):
                for i, x in enumerate(node):
                    strip_codes(x, f"{path}[{i}]")
        strip_codes({k: v for k, v in self.state.items() if k not in ("meta", "conflicts", "unmapped", "review_notes")}, "")
        if removed:
            self.state["review_notes"].append(f"Harness đã loại {len(removed)} mã do LLM tự ghi (không in trên phiếu / không kiểm chứng được); mã được tra lại bằng term_lookup.")
        # mã danh mục VN Core
        enc = self.state.get("encounter") or {}
        for fld, cs in (("referral_mode", "vn-referral-mode-cs"), ("treatment_outcome", "vn-treatment-outcome-cs"),
                        ("discharge_disposition", "vn-discharge-disposition-cs"), ("payment_method", "vn-payment-method-cs")):
            if enc.get(fld) is not None:
                enc[fld] = str(enc[fld])
                if V.cs_display(cs, enc[fld]) is None:
                    notes.append(f"encounter.{fld}: mã '{enc[fld]}' không có trong {cs} — đã bỏ")
                    enc.pop(fld)
        if (enc.get("type") or {}).get("code") and V.cs_display("vn-encounter-type-cs", str(enc["type"]["code"])) is None:
            enc.pop("type")
        for o in self.state["organizations"]:
            for fld, cs in (("org_type", "vn-organization-type-cs"), ("org_level", "vn-healthcare-level-cs"),
                            ("facility_care_level", "vn-facility-care-level-cs"), ("department_code", "vn-department-cs")):
                if o.get(fld) and V.cs_display(cs, str(o[fld])) is None:
                    o.pop(fld)
            if o.get("kind") not in ("facility", "department", "payer", "other"):
                o["kind"] = "other"
        for p in self.state["practitioners"]:
            for r in p.get("roles") or []:
                r["specialty"] = [x for x in r.get("specialty") or [] if V.cs_display("vn-specialty-cs", x)]
                if not r["specialty"]:
                    r.pop("specialty")
        pt = self.state.get("patient") or {}
        if (pt.get("ethnicity") or {}).get("code") and V.cs_display("vn-ethnicity-cs", str(pt["ethnicity"]["code"])) is None:
            pt["ethnicity"].pop("code")
        eth = pt.get("ethnicity")
        if eth and not eth.get("code"):      # Builder cần mã: tra theo tên in trên phiếu (khớp đúng), không được thì bỏ
            hit = [c for c in V.cs_search("vn-ethnicity-cs", eth.get("text") or "", 3)
                   if V.norm(c["display"] or "") == V.norm(eth.get("text") or "")]
            if hit:
                eth["code"] = hit[0]["code"]
            else:
                notes.append(f"patient: dân tộc '{eth.get('text')}' không khớp vn-ethnicity-cs — bỏ extension dân tộc")
                pt.pop("ethnicity")
        if pt.get("name"):
            fam, giv = V.split_name(pt["name"].get("text", ""))
            pt["name"].update(family=fam, given=giv)
        if pt.get("gender") not in ("male", "female", "other", "unknown"):
            pt["gender"] = "unknown"
        cov = self.state.get("coverage")
        if cov and not re.fullmatch(r"[A-Z]{2}[0-9]{13}", str(cov.get("bhyt_number", ""))):
            notes.append(f"coverage: số thẻ BHYT '{cov.get('bhyt_number')}' sai định dạng — bỏ Coverage")
            self.state["coverage"] = None
        for loc in self.state["locations"]:
            if loc.get("kind") not in ("clinic", "ward", "bed", "room", "building", "site", "other"):
                loc["kind"] = "other"

    def ensure_required(self):
        """Trường build_fhir đọc bắt buộc (x["code"], x["name"]…) mà model bỏ sót -> điền dạng CHỈ CÓ TEXT (không bịa mã), hoặc bỏ phần tử rỗng."""
        s, V = self.state, self.V
        notes = s["review_notes"]

        def cc(x, key, *texts):
            v = x.get(key)
            if isinstance(v, str):
                v = {"text": v}
            if not isinstance(v, dict):
                v = {}
            if not v.get("text"):
                v["text"] = next((t for t in texts if isinstance(t, str) and t.strip()), None) or "Không ghi rõ"
            v.setdefault("codings", [])
            x[key] = v

        cs = s.get("cost_statement")
        if cs:                               # Builder đọc ln["serviced"]: dòng thiếu ngày -> lấy khoảng thời gian đợt điều trị
            ep = (s.get("encounter") or {}).get("period") or {}
            per = {k: str(ep[k])[:10] for k in ("start", "end") if ep.get(k)}
            miss = [ln for ln in cs.get("lines") or [] if isinstance(ln, dict) and not ln.get("serviced")]
            for ln in miss:
                if per:
                    ln["serviced"] = dict(per)
            if miss and per:
                notes.append(f"cost_statement: {len(miss)} dòng thiếu ngày thực hiện — tạm lấy thời gian đợt điều trị {per}")
            elif miss:
                cs["lines"] = [ln for ln in cs.get("lines") or [] if not isinstance(ln, dict) or ln.get("serviced")]
                notes.append(f"cost_statement: bỏ {len(miss)} dòng thiếu ngày thực hiện (không có thời gian đợt điều trị)")
        for o in s["organizations"]:
            o.setdefault("name", o.get("id"))
        for l in s["locations"]:
            l["name"] = l.get("name") or l["id"]
        for c in s["conditions"]:
            cc(c, "code", c.get("text"))
        for o in s["observations"]:
            cc(o, "code", o.get("text"), o.get("name_raw"))
            for cmp in o.get("components") or []:
                cc(cmp, "code", cmp.get("text"), cmp.get("name_raw"))
        for d in s["diagnostic_reports"]:
            if not d.get("effective") and d.get("issued"):     # phiếu chỉ in 1 thời điểm = thời điểm thực hiện (quy ước GT)
                d["effective"] = d.pop("issued")
            elif d.get("effective") and d.get("effective") == d.get("issued") and d.get("kind") != "lab":
                d.pop("issued")
        for b in ("service_requests", "diagnostic_reports", "medications"):
            for x in s[b]:
                cc(x, "code", x.get("text"), x.get("name_raw"), x.get("conclusion"))
        for a in s["allergies"]:
            if not a.get("nka"):
                cc(a, "code", a.get("text"))
        for f in s["family_history"]:
            for c in f.get("conditions") or []:
                cc(c, "code", c.get("text"), c.get("note"))
        for ci in s["clinical_impressions"]:
            for f in ci.get("findings") or []:
                if not f.get("reference"):
                    cc(f, "code", f.get("text"), f.get("basis"))
        for sp in s["specimens"]:
            t = sp.get("type") if isinstance(sp.get("type"), dict) else {"text": sp.get("type") if isinstance(sp.get("type"), str) else None}
            if t.get("code") not in SPECIMEN_V2:
                txt = V.slug(t.get("text") or sp.get("text") or "").replace("-", " ")
                t["code"] = next((c for k, c in SPECIMEN_KW if k in txt), "BLD")
                t.setdefault("text", t.get("text") or "Máu")
            sp["type"] = t
        # dùng thuốc nội trú đã hoàn tất phải tham chiếu Medication (vn-medadmin-completed-inpatient-reference) -> tạo Medication từ tên thuốc
        meds_by_text = {V.norm((m.get("code") or {}).get("text") or ""): m for m in s["medications"]}
        for m in s["medication_administrations"]:
            if m.get("medication") or not (m.get("medication_code") or {}).get("text"):
                continue
            key = V.norm(m["medication_code"]["text"])
            med = meds_by_text.get(key)
            if med is None:
                mid = _fix_id(V, "med-" + V.slug(m["medication_code"]["text"]))
                while self._id_taken(mid):
                    mid += "-x"
                med = {"id": mid, "code": {"text": m["medication_code"]["text"], "codings": list(m["medication_code"].get("codings") or [])}, "sources": list(m.get("sources") or [])}
                s["medications"].append(med)
                meds_by_text[key] = med
            m["medication"] = med["id"]
            m.pop("medication_code", None)
        for m in s["medication_requests"] + s["medication_administrations"]:
            d = m.get("dosage")
            if isinstance(d, dict) and d.get("dose") and not isinstance(d["dose"], dict):
                d["text"] = d.get("text") or str(d.pop("dose"))
        pt = s.get("patient") or {}
        cov = s.get("coverage")
        if isinstance(cov, dict) and isinstance(cov.get("period"), dict) and not cov["period"].get("start"):
            end = cov["period"].get("end")
            cov.pop("period")
            if end:
                cov["note"] = ((cov.get("note") or "") + f" Thẻ có giá trị đến {end} (không in ngày bắt đầu).").strip()
        if pt:
            pt.setdefault("birth_date", None)
            pt.setdefault("gender", "unknown")
            pt.setdefault("name", {"text": "Không ghi rõ"})
        for k, cp in list((s.get("compositions") or {}).items()):
            if not isinstance(cp, dict):
                s["compositions"].pop(k)
                continue
            cp.setdefault("id", f"composition-{V.slug(k)}")
            t = cp.get("type") if isinstance(cp.get("type"), dict) else {}
            if not t.get("code"):
                t = {"code": "18842-5", "display": "Discharge summary"} if "discharge" in k or "ra-vien" in k or "tom-tat" in k else {"code": "11503-0", "display": "Medical records"}
            cp["type"] = t
            cp["title"] = cp.get("title") or t.get("display")
            cp["date"] = cp.get("date") or ((s.get("encounter") or {}).get("period") or {}).get("end") or s["meta"]["created"]
            # mục cùng key (slice max=1) -> gộp text_lines + entries
            merged = {}
            for sec in cp.get("sections") or []:
                if not isinstance(sec, dict):
                    continue
                sec["title"] = sec.get("title") or sec.get("key") or "Mục"
                if isinstance(sec.get("code"), dict) and not sec["code"].get("code"):
                    sec.pop("code")
                k = sec.get("key") or ("code:" + sec["code"]["code"] if sec.get("code") else "title:" + sec["title"])
                if k in merged:
                    m = merged[k]
                    m["text_lines"] = list(dict.fromkeys((m.get("text_lines") or []) + (sec.get("text_lines") or [])))
                    m["entries"] = list(dict.fromkeys((m.get("entries") or []) + (sec.get("entries") or [])))
                    for kk in ("text_lines", "entries"):
                        if not m[kk]:
                            m.pop(kk)
                else:
                    merged[k] = sec
            cp["sections"] = list(merged.values())
            # identifier bắt buộc: mã bệnh án / số vào viện của lượt điều trị
            if not (cp.get("identifier") or {}).get("value"):
                ids = (s.get("encounter") or {}).get("identifiers") or []
                pick = next((i for i in ids if i.get("type") in ("MA_BENH_AN", "SO_VAO_VIEN", "LOCAL") and i.get("value")), None) or next((i for i in ids if i.get("value")), None)
                cp["identifier"] = {"value": pick["value"], "text": pick.get("text") or pick.get("type") or "Mã bệnh án"} if pick else {"value": s["meta"].get("case_id") or k, "text": "Mã hồ sơ"}
            # final cần người ký xác nhận; không có attester -> preliminary
            if cp.get("status", "final") in ("final", "amended") and not cp.get("attesters"):
                cp["status"] = "preliminary"
                notes.append(f"compositions.{k}: không trích được người ký xác nhận — trạng thái preliminary")

    def drop_dangling(self):
        s = self.state
        ids = {x["id"] for b in LIST_BLOCKS for x in s[b]} | {s[b]["id"] for b in SINGLE_BLOCKS if s.get(b) and s[b].get("id")}
        ids |= {c.get("id") for c in s["compositions"].values() if c.get("id")}
        for p in s["practitioners"]:
            for i, r in enumerate(p.get("roles") or []):
                ids.add(r.get("id") or f"{p['id']}-role-{i + 1}")
        doc_ids = {d["id"] for d in s["documents"]}
        dropped = []

        def walk(node, path, in_cost=False):
            if isinstance(node, dict):
                for k, v in list(node.items()):
                    if k == "sources":
                        node[k] = [x for x in (v or []) if x in doc_ids] or None
                        if node[k] is None:
                            node.pop(k)
                        continue
                    if k in self.ref_keys and not (k == "department" and in_cost):
                        if isinstance(v, str):
                            if v and v not in ids and v not in doc_ids:
                                dropped.append(f"{path}.{k}={v}")
                                node.pop(k)
                        elif isinstance(v, list):
                            keep = []
                            for r in v:
                                if isinstance(r, str):
                                    if r in ids or r in doc_ids:
                                        keep.append(r)
                                    else:
                                        dropped.append(f"{path}.{k}={r}")
                                else:
                                    walk(r, f"{path}.{k}", in_cost)
                                    keep.append(r)
                            node[k] = keep
                        elif isinstance(v, dict):
                            walk(v, f"{path}.{k}", in_cost)
                    else:
                        walk(v, f"{path}.{k}", in_cost or k == "cost_statement")
            elif isinstance(node, list):
                for i, x in enumerate(node):
                    walk(x, f"{path}[{i}]", in_cost)
        walk({k: v for k, v in s.items() if k not in ("meta", "documents", "conflicts", "unmapped", "review_notes")}, "")
        for i, d in enumerate(s["documents"]):          # documents không đi qua walk (sources của phiếu): chỉ gỡ người lập / người ký treo
            for k in ("authors", "authenticator"):
                v = d.get(k)
                if isinstance(v, str) and v not in ids:
                    dropped.append(f"documents[{i}].{k}={v}")
                    d.pop(k)
                elif isinstance(v, list):
                    dropped += [f"documents[{i}].{k}={r}" for r in v if isinstance(r, str) and r not in ids]
                    d[k] = [r for r in v if not isinstance(r, str) or r in ids]
        # mục tham chiếu bắt buộc bị rỗng (encounter.locations[].location, participants[].individual, diagnoses[].condition…)
        enc = s.get("encounter") or {}
        enc["locations"] = [x for x in enc.get("locations") or [] if x.get("location")] or None
        enc["participants"] = [x for x in enc.get("participants") or [] if x.get("individual")] or None
        enc["diagnoses"] = [x for x in enc.get("diagnoses") or [] if x.get("condition")] or None
        for k in ("locations", "participants", "diagnoses"):
            if enc.get(k) is None:
                enc.pop(k, None)
        if s.get("cost_statement"):
            s["cost_statement"]["diagnoses"] = [x for x in s["cost_statement"].get("diagnoses") or [] if x.get("condition")]
        for ma in s["medication_administrations"]:
            if not ma.get("medication") and not ma.get("medication_code"):
                ma["medication_code"] = {"text": (ma.get("dosage") or {}).get("text") or "Thuốc (không xác định)", "codings": []}
        for mr in s["medication_requests"]:
            if not mr.get("medication") and not mr.get("medication_code"):
                mr["medication_code"] = {"text": (mr.get("dosage") or {}).get("text") or "Thuốc (không xác định)", "codings": []}
        for b in LIST_BLOCKS:
            for x in s[b]:
                if not x.get("sources"):
                    x["sources"] = [d for d in self._task_docs(x["id"])] or [s["documents"][0]["id"]]
        if dropped:
            s["review_notes"].append(f"Harness gỡ {len(dropped)} tham chiếu tới id không tồn tại: " + "; ".join(dropped[:20]) + (" …" if len(dropped) > 20 else ""))

    def _task_docs(self, id_):
        t = self.origin.get(id_, "")
        return [t.split(":", 1)[1]] if ":" in t else []


def _fill(dst: dict, src: dict):
    """Gộp src vào dst: điền trường thiếu, hợp nhất sources/list chuỗi."""
    for k, v in src.items():
        if k == "id":
            continue
        if k not in dst or dst[k] in (None, "", [], {}):
            dst[k] = v
        elif k == "sources" or (isinstance(v, list) and isinstance(dst[k], list) and all(isinstance(x, str) for x in v + dst[k])):
            dst[k] = list(dict.fromkeys(dst[k] + v))
        elif k == "roles" and isinstance(v, list):
            for r in v:
                if r not in dst[k]:
                    dst[k].append(r)


def _prune_none(o):
    if isinstance(o, dict):
        return {k: _prune_none(v) for k, v in o.items() if v is not None}
    if isinstance(o, list):
        return [_prune_none(x) for x in o if x is not None]
    return o


def _fix_id(V, id_: str) -> str:
    if isinstance(id_, str) and ID_RE.match(id_):
        return id_
    s = V.slug(str(id_)).replace("_", "-")
    s = re.sub(r"[^a-z0-9.\-]", "-", s).strip("-.")[:64] or "x"
    return s if ID_RE.match(s) else "x-" + s[:60]


def _num(s):
    if isinstance(s, (int, float)):
        return s
    if not isinstance(s, str):
        return None
    t = s.strip().replace(" ", "")
    if not t:
        return None
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+", t):          # 50.600 -> 50600
        t = t.replace(".", "")
    elif re.fullmatch(r"-?\d{1,3}(,\d{3})+", t):        # 50,600 -> 50600
        t = t.replace(",", "")
    elif re.fullmatch(r"-?\d+,\d+", t):                 # 18,43 -> 18.43
        t = t.replace(",", ".")
    try:
        f = float(t)
        return int(f) if f.is_integer() and "." not in t else f
    except ValueError:
        return None
