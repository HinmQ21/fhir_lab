"""Bước 5 — mã hoá bằng tra cứu (references/coding-rules.md): "không bịa mã".

Xác định (không LLM):
  sinh hiệu -> vnlib.VITALS (LOINC + UCUM + nhãn đơn vị fixedString của profile); xét nghiệm -> lab_loinc_candidates(tên, đơn vị, panel)
  + CLS QĐ 1227 (ưu tiên dòng có ConceptMap trùng LOINC đã chọn); panel/CĐHA/TDCN -> LAB_PANELS / imaging_candidates;
  đơn vị -> UCUM; đường dùng thuốc -> SNOMED + vn-medication-route-cs; địa chỉ -> vn-province-cs / vn-ward-cs theo tên.
Cần ngữ nghĩa: chẩn đoán chưa in mã -> icd10_search lấy ứng viên, LLM chỉ CHỌN chỉ số ứng viên (hoặc null). Mọi coding mang source/verified.
"""
from __future__ import annotations

import re

import json

from . import prompts as PR

# đơn vị UCUM hợp lệ mà bảng vnlib.ucum chưa có (bổ sung, không thay thế bảng của skill)
UCUM_EXTRA = {"l/l": "L/L", "iu/l": "[IU]/L", "meq/l": "meq/L", "mmol/l": "mmol/L", "s": "s", "giây": "s", "mm/h": "mm/h", "mm/giờ": "mm/h",
              "/mm3": "/mm3", "mui/l": "m[IU]/L", "miu/l": "m[IU]/L", "ui/l": "[IU]/L", "osmol/kg": "osm/kg"}

FORM_PANEL = {"ket-qua-xet-nghiem-huyet-hoc": ("huyet_hoc", "01"), "ket-qua-xet-nghiem-sinh-hoa-mau": ("sinh_hoa", "02"),
              "phieu-xet-nghiem-nuoc-dich": ("nuoc_tieu", "02")}
ROUTES = {"uống": ("26643006", "Oral route"), "tiêm tĩnh mạch": ("47625008", "Intravenous route"), "truyền tĩnh mạch": ("47625008", "Intravenous route"),
          "tiêm bắp": ("78421000", "Intramuscular route"), "tiêm dưới da": ("34206005", "Subcutaneous route"), "nhỏ mắt": ("54485002", "Ophthalmic route"),
          "bôi": ("6064005", "Topical route"), "ngậm": ("37839007", "Sublingual route"), "đặt hậu môn": ("37161004", "Rectal route"),
          "khí dung": ("18679011000001101", "Inhalation"), "nhỏ mũi": ("46713006", "Nasal route")}
BP_COMPONENTS = {"tâm thu": ("8480-6", "Systolic blood pressure"), "tâm trương": ("8462-4", "Diastolic blood pressure")}
EXAM_CURATED = {"glasgow": ("9269-2", "Glasgow coma score total")}


def C(system, code, display, source, confidence=1.0):
    return {"system": system, "code": code, "display": display, "source": source, "verified": True, "confidence": confidence, "_harness": True}


class Coder:
    def __init__(self, kb, client=None, log=print, texts: dict[str, str] | None = None):
        self.kb, self.V = kb, kb.V
        self.texts = texts or {}
        self._names = None
        self.client = client
        self.log = log
        self.decisions: list[dict] = []    # cho review.md

    def run(self, s: dict) -> dict:
        V = self.V
        self.decisions = []
        docs = {d["id"]: d for d in s["documents"]}
        for d in s["documents"]:
            t = V.DOC_TYPES.get(d["form"], V.DOC_TYPES["default"])
            d["type"] = {"code": t[0], "display": t[1]}
        panel_of_obs = {}
        for dr in s["diagnostic_reports"]:
            form = next((docs[x]["form"] for x in dr.get("sources") or [] if x in docs and docs[x]["form"] in FORM_PANEL), None)
            for r in dr.get("results") or []:
                panel_of_obs[r] = form
        self._lab_pending = []
        for o in s["observations"]:
            self._observation(o, panel_of_obs.get(o["id"]) or next((docs[x]["form"] for x in o.get("sources") or [] if x in docs and docs[x]["form"] in FORM_PANEL), None))
        for dr in s["diagnostic_reports"]:
            self._report(dr)
        self._choose_labs()
        # dòng xét nghiệm không có kết quả và không tra được mã (ký hiệu máy, dòng trống) -> unmapped, không thành resource
        junk = {o["id"] for o in s["observations"] if o.get("kind") == "lab" and not (o.get("code") or {}).get("codings")
                and (not o.get("value") or (o.get("value") or {}).get("absent")) and not o.get("components")}
        if junk:
            for o in s["observations"]:
                if o["id"] in junk:
                    s["unmapped"].append({"item": f"observations[{o['id']}] '{(o.get('code') or {}).get('text')}'", "reason": "dòng in trên phiếu không có kết quả, không có mã LOINC/CLS",
                                          "action": "không tạo Observation"})
            s["observations"] = [o for o in s["observations"] if o["id"] not in junk]
            for dr in s["diagnostic_reports"]:
                if dr.get("results"):
                    dr["results"] = [r for r in dr["results"] if r not in junk]
        self.obs_loinc = {c["code"] for o in s["observations"] if o.get("kind") == "lab" for c in (o.get("code") or {}).get("codings") or [] if c.get("system") == "loinc"}
        for sr in s["service_requests"]:
            self._service_request(sr)
        for m in s["medication_requests"] + s["medication_administrations"]:
            self._dosage(m.get("dosage") or {})
            for k in ("dispense",):
                if isinstance(m.get(k), dict):
                    self._qty(m[k])
        for m in s["medications"]:
            for ing in m.get("ingredients") or []:
                if isinstance(ing.get("strength"), dict):
                    self._qty(ing["strength"])
        self._address(s.get("patient") or {})
        self._conditions(s)
        self._verify_names(s)
        return s

    # ------------------------------------------------------------------ họ tên phải có NGUYÊN VĂN trên phiếu
    def _text_names(self):
        """Họ tên xuất hiện trên phiếu (sau học hàm/chức danh hoặc nhãn họ tên) — cùng bộ mẫu với PrivacyGuard."""
        if self._names is None:
            from .privacy import LABEL_NAME, TITLE_NAME
            names = {}
            for t in self.texts.values():
                for rx in (TITLE_NAME, LABEL_NAME):
                    for m in rx.finditer(t):
                        v = " ".join(w.capitalize() if w.isupper() else w for w in m.group(1).split())
                        names[self.V.slug(v)] = v
            self._names = names
        return self._names

    def _verify_names(self, s):
        import difflib
        if not self.texts:
            return
        V = self.V
        blob = V.slug(" ".join(self.texts.values()))
        names = self._text_names()
        for p in s["practitioners"]:
            n = (p.get("name") or {}).get("text") or ""
            sl = V.slug(n)
            if not sl or sl in blob:
                continue
            best = max(names.items(), key=lambda kv: difflib.SequenceMatcher(None, sl, kv[0]).ratio(), default=None)
            r = difflib.SequenceMatcher(None, sl, best[0]).ratio() if best else 0
            if best and r >= 0.8:
                fam, giv = V.split_name(best[1])
                p["name"] = {"text": best[1], "family": fam, "given": giv}
                if p.get("name_raw") and n in p["name_raw"]:
                    p["name_raw"] = p["name_raw"].replace(n, best[1])
                s["review_notes"].append(f"practitioners[{p['id']}]: họ tên '{n}' không có nguyên văn trên phiếu — sửa thành '{best[1]}' (khớp {r:.2f})")
                self.decisions.append({"where": p["id"], "text": n, "codes": [], "rule": f"kiểm họ tên nguyên văn → '{best[1]}'"})
            else:
                s["review_notes"].append(f"practitioners[{p['id']}]: họ tên '{n}' không tìm thấy nguyên văn trên phiếu — người duyệt kiểm tra")

    # ------------------------------------------------------------------ đơn vị
    def _qty(self, q: dict, fixed_label: str | None = None, ucum_code: str | None = None):
        if not isinstance(q, dict):
            return
        if ucum_code:
            q["ucum"], q["unit"] = ucum_code, fixed_label
            q.setdefault("unit_raw", fixed_label)
            return
        if not (q.get("unit_raw") or "").strip() and isinstance(q.get("value"), (int, float)):
            q["ucum"] = "1"          # đại lượng không thứ nguyên (pH, tỷ trọng, chỉ số) — UCUM "1"
            return
        code, label = self.V.ucum(q.get("unit_raw"))
        if code is None:
            code = UCUM_EXTRA.get((q.get("unit_raw") or "").strip().lower())
        q["ucum"] = code
        if label:
            q["unit"] = label
        elif "unit" in q and not q["unit"]:
            q.pop("unit")

    # ------------------------------------------------------------------ observation
    def _observation(self, o: dict, form: str | None):
        V = self.V
        code = o.setdefault("code", {"text": "", "codings": []})
        if isinstance(code, str):
            code = o["code"] = {"text": code, "codings": []}
        code.setdefault("codings", [])
        text = code.get("text") or ""
        n = V.norm(text)
        not_vital = bool(re.search(r"glucose|đường\s*(?:huyết|máu)|mao\s*mạch|ceton|hba1c", n))
        if not not_vital and (o.get("kind") == "vital-sign" or any(re.search(rf"(?<!\w){re.escape(a)}(?!\w)", n) for a in V.VITAL_ALIASES)):
            key = V.VITAL_ALIASES.get(n) or next((k for al, k in V.VITAL_ALIASES.items() if V.norm(al) in n), None)
            if key and o.get("kind") in ("vital-sign", "exam", None):
                loinc, en, vi, uc, label, prof = V.VITALS[key]
                o["kind"] = "vital-sign"
                code["codings"] = [C("loinc", loinc, en, "curated")]
                if key == "huyet_ap":
                    for comp in o.get("components") or []:
                        cn = V.norm((comp.get("code") or {}).get("text") or "")
                        hit = next((v for k2, v in BP_COMPONENTS.items() if k2 in cn), None)
                        if hit:
                            comp["code"] = {"text": comp["code"].get("text"), "codings": [C("loinc", hit[0], hit[1], "curated")]}
                        q = (comp.get("value") or {}).get("quantity")
                        self._qty(q, "mmHg", "mm[Hg]")
                    if not o.get("components") and (o.get("value") or {}).get("string"):
                        self._bp_from_string(o)
                else:
                    self._qty((o.get("value") or {}).get("quantity"), label, uc)
                return
            if o.get("kind") == "vital-sign":
                o["kind"] = "exam"
        if o.get("kind") == "lab":
            q = (o.get("value") or {}).get("quantity") or {}
            panel, appendix = FORM_PANEL.get(form, (None, None))
            cands = V.lab_loinc_candidates(text, q.get("unit_raw"), {"nuoc_tieu": "XN NƯỚC TIỂU"}.get(panel, panel))
            if cands and cands[0]["score"] >= 0.95 and (len(cands) == 1 or cands[0]["score"] - cands[1]["score"] >= 0.1):
                top = cands[0]
                code["codings"] = [C("loinc", top["code"], top["display"], "curated")]
                cls = self._cls_for(text, appendix, q.get("unit_raw"), top["code"])
                if cls:
                    code["codings"].append(cls)
                self.decisions.append({"where": o["id"], "text": text, "loinc": top["code"], "cls": cls and cls["code"], "rule": "curated + đơn vị"})
            else:
                self._lab_pending.append((o, text, q.get("unit_raw"), appendix, cands))
        elif o.get("kind") in ("exam", "survey"):
            hit = next((v for k, v in EXAM_CURATED.items() if k in n), None)
            if hit:
                code["codings"] = [C("loinc", hit[0], hit[1], "curated")]
        for qd in [(o.get("value") or {}).get("quantity")] + [(c.get("value") or {}).get("quantity") for c in o.get("components") or []]:
            if isinstance(qd, dict) and "ucum" not in qd:
                self._qty(qd)
        if (o.get("reference_range") or {}).get("unit") is None and isinstance((o.get("value") or {}).get("quantity"), dict):
            pass

    def _choose_labs(self):
        """Xét nghiệm chưa có LOINC curated chắc chắn: ứng viên = LOINC curated (lab_loinc_candidates) ∪ LOINC ánh xạ từ các dòng CLS
        (cls_search) -> LLM chỉ chọn chỉ số k (hoặc null). Mã luôn đến từ bảng tra cứu, không từ LLM."""
        V = self.V
        jobs = []
        for o, text, unit, appendix, cands in self._lab_pending:
            opts = {}
            for c in cands[:4]:
                if c["score"] > 0.2:
                    opts.setdefault(c["code"], {"loinc": c["code"], "display": c["display"], "via": "curated"})
            for r in V.cls_search(text, appendix, 6, unit):
                for l in r.get("loinc") or []:
                    opts.setdefault(l["code"], {"loinc": l["code"], "display": l.get("display"), "via": f"CLS {r['code']} {r['display']}"[:90]})
            opts = list(opts.values())[:6]
            if opts:
                jobs.append((o, text, unit, appendix, opts))
            else:
                self.decisions.append({"where": o["id"], "text": text, "loinc": None, "rule": "không có ứng viên LOINC/CLS"})
        if not jobs:
            return
        choices = {}
        if self.client is not None:
            items = [{"i": i, "xet_nghiem": text, "don_vi": unit, "candidates": [dict(k=k, **x) for k, x in enumerate(opts)]}
                     for i, (o, text, unit, appendix, opts) in enumerate(jobs)]
            try:
                res, _ = self.client.json("choose:loinc", PR.CHOOSE_SYSTEM,
                                          "Chọn mã LOINC cho từng kết quả xét nghiệm (khớp chất phân tích, mẫu, VÀ đơn vị: % ⇒ tỷ lệ, "
                                          "mmol/L ⇒ mol, g/L ⇒ khối lượng, L/L ⇒ phân số thể tích):\n" + json.dumps(items, ensure_ascii=False, indent=1),
                                          max_tokens=4000)
                choices = {ch.get("i"): ch for ch in (res or {}).get("choices") or [] if isinstance(ch, dict)}
            except Exception as e:  # noqa: BLE001
                self.log(f"  ! choose:loinc lỗi: {e}")
        for i, (o, text, unit, appendix, opts) in enumerate(jobs):
            ch = choices.get(i) or {}
            k = ch.get("k")
            if isinstance(k, int) and 0 <= k < len(opts):
                x = opts[k]
                o["code"]["codings"] = [C("loinc", x["loinc"], x["display"], "lookup", 0.9)]
                cls = self._cls_for(text, appendix, unit, x["loinc"])
                if cls:
                    o["code"]["codings"].append(cls)
                self.decisions.append({"where": o["id"], "text": text, "loinc": x["loinc"], "cls": cls and cls["code"],
                                       "rule": f"lookup ({x['via']}) + LLM chọn", "reason": ch.get("reason")})
            else:
                self.decisions.append({"where": o["id"], "text": text, "loinc": None, "rule": "không ứng viên nào khớp nghĩa", "reason": ch.get("reason")})

    def _bp_from_string(self, o):
        import re
        m = re.search(r"(\d{2,3})\s*/\s*(\d{2,3})", o["value"]["string"])
        if not m:
            return
        o.pop("value")
        o["components"] = [{"code": {"text": "Huyết áp tâm thu", "codings": [C("loinc", "8480-6", "Systolic blood pressure", "curated")]},
                            "value": {"quantity": {"value": int(m.group(1)), "unit_raw": "mmHg", "ucum": "mm[Hg]", "unit": "mmHg"}}},
                           {"code": {"text": "Huyết áp tâm trương", "codings": [C("loinc", "8462-4", "Diastolic blood pressure", "curated")]},
                            "value": {"quantity": {"value": int(m.group(2)), "unit_raw": "mmHg", "ucum": "mm[Hg]", "unit": "mmHg"}}}]

    def _cls_for(self, text, appendix, unit, loinc):
        rows = self.V.cls_search(text, appendix, 8, unit)
        by_map = [r for r in rows if any(x.get("code") == loinc for x in r.get("loinc") or [])]
        pick = by_map[0] if by_map else (rows[0] if rows and rows[0]["ratio"] >= 0.95 and (len(rows) == 1 or rows[0]["ratio"] - rows[1]["ratio"] >= 0.05) else None)
        return C("cls", pick["code"], pick["display"], "lookup") if pick else None

    # ------------------------------------------------------------------ DR / SR
    def _panel(self, text):
        n = self.V.norm(text)
        for k, (loinc, disp, cls) in self.V.LAB_PANELS.items():
            if k in n:
                return [C("loinc", loinc, disp, "curated"), C("cls", cls, next((r["display"] for r in self.V._cls_rows() if r["code"] == cls), None), "lookup")]
        if "tế bào máu" in n or "công thức máu" in n:
            loinc, disp, cls = self.V.LAB_PANELS["tổng phân tích tế bào máu"]
            return [C("loinc", loinc, disp, "curated"), C("cls", cls, next((r["display"] for r in self.V._cls_rows() if r["code"] == cls), None), "lookup")]
        return None

    def _imaging(self, text):
        c = self.V.imaging_candidates(text)
        if not c:
            return None
        top = c[0]
        out = [C("loinc", top["code"], top["display"], "curated")]
        if top.get("cls"):
            out.append(C("cls", top["cls"], next((r["display"] for r in self.V._cls_rows() if r["code"] == top["cls"]), None), "lookup"))
        return out

    def _report(self, dr):
        code = dr.setdefault("code", {"text": "", "codings": []})
        code.setdefault("codings", [])
        text = code.get("text") or ""
        cods = self._panel(text) if dr.get("kind") == "lab" else self._imaging(text)
        if cods:
            code["codings"] = cods
        self.decisions.append({"where": dr["id"], "text": text, "codes": [c["code"] for c in cods or []], "rule": "panel/CĐHA curated" if cods else "text-only (không có panel khớp đúng bộ)"})

    def _service_request(self, sr):
        code = sr.setdefault("code", {"text": "", "codings": []})
        code.setdefault("codings", [])
        text = code.get("text") or ""
        cat = (sr.get("category") or "").upper()
        cods = None
        if cat in ("CDHA", "TDCN"):
            cods = self._imaging(text)
        else:
            cods = self._panel(text)
            if not cods:
                core = re.sub(r"\((?:m[aá]u|M[aá]u|huy[eế]t\s*thanh)\)", "", re.sub(r"^\s*(?:Đ[iị]nh\s*l[uư][oợ]ng|Đo\s*ho[aạ]t\s*đ[oộ]|Đo|X[eé]t\s*nghi[eệ]m)\s+", "", text, flags=re.I)).strip()
                cands = self.V.lab_loinc_candidates(core or text)
                # LOINC của chỉ định = LOINC của kết quả cùng hồ sơ (đơn vị đã quyết định mol/khối lượng); ứng viên phải giao đúng 1 mã
                hit = sorted({c["code"] for c in cands if c["score"] >= 0.8 and c["code"] in self.obs_loinc})
                if len(hit) == 1:
                    top = next(c for c in cands if c["code"] == hit[0])
                    cods = [C("loinc", top["code"], top["display"], "curated")]
                    cls = self._cls_for(text, None, None, top["code"])
                    if cls:
                        cods.append(cls)
                    self.decisions.append({"where": sr["id"], "text": text, "loinc": top["code"], "cls": cls and cls["code"], "rule": "LOINC của kết quả cùng hồ sơ ∩ ứng viên curated"})
                elif cands and cands[0]["score"] >= 1.0 and (len(cands) == 1 or cands[1]["score"] < 1.0):
                    cods = [C("loinc", cands[0]["code"], cands[0]["display"], "curated")]
                    cls = self._cls_for(text, None, None, cands[0]["code"])
                    if cls:
                        cods.append(cls)
                else:
                    rows = self.V.cls_search(text, None, 3)
                    if rows and rows[0]["ratio"] >= 0.95 and (len(rows) == 1 or rows[0]["ratio"] - rows[1]["ratio"] >= 0.05):
                        cods = [C("cls", rows[0]["code"], rows[0]["display"], "lookup")]
        if cods:
            code["codings"] = cods
        if cat and self.V.cs_display("vn-service-request-category-cs", cat) is None:
            sr.pop("category", None)

    # ------------------------------------------------------------------ thuốc
    def _dosage(self, dz: dict):
        if not isinstance(dz, dict):
            return
        r = dz.get("route")
        if isinstance(r, str):
            r = dz["route"] = {"text": r}
        if isinstance(r, dict) and r.get("text"):
            n = self.V.norm(r["text"])
            hit = next((v for k, v in ROUTES.items() if k in n), None)
            if hit:
                r["snomed"], r["snomed_display"] = hit
            if r.get("vn_code") and self.V.cs_display("vn-medication-route-cs", str(r["vn_code"])) is None:
                r.pop("vn_code")
            if not r.get("vn_code"):
                c = self.V.cs_search("vn-medication-route-cs", r["text"], 2)
                if c and c[0]["ratio"] >= 0.95:
                    r["vn_code"] = c[0]["code"]
        if isinstance(dz.get("dose"), dict):
            self._qty(dz["dose"])

    # ------------------------------------------------------------------ địa chỉ
    def _address(self, pt):
        a = pt.get("address")
        if not isinstance(a, dict):
            return
        V = self.V
        if a.get("province_code") and V.cs_display("vn-province-cs", str(a["province_code"])) is None:
            a.pop("province_code")
        if a.get("state") and not a.get("province_code"):
            c = V.cs_search("vn-province-cs", a["state"], 2)
            if c and c[0]["ratio"] >= 0.9:
                a["province_code"] = c[0]["code"]
        if a.get("ward_code") and V.cs_display("vn-ward-cs", str(a["ward_code"])) is None:
            a.pop("ward_code")
        if a.get("ward") and not a.get("ward_code"):
            wn = V.norm(a["ward"])
            exact = [c for c in V.cs_concepts("vn-ward-cs") if V.norm(c["display"] or "") == wn]
            if len(exact) == 1:
                a["ward_code"] = exact[0]["code"]

    # ------------------------------------------------------------------ chẩn đoán
    def _conditions(self, s):
        V = self.V
        pending = []
        for c in s["conditions"]:
            code = c.setdefault("code", {"text": c.get("text") or "", "codings": []})
            code.setdefault("text", c.get("text") or "")
            have = [x for x in code.get("codings") or [] if x.get("system") == "icd10vn"]
            if have:
                self._add_who(code)
                self.decisions.append({"where": c["id"], "text": c.get("text"), "icd10": have[0]["code"], "rule": "mã in trên phiếu (document)"})
                continue
            cands = V.icd10_search(c.get("text") or "", 6)
            cands = [x for x in cands if not x.get("source_table")]
            c["candidates"] = [{"code": x["code"], "display": x["display"], "ratio": x["ratio"]} for x in cands[:3]]
            if cands:
                pending.append((c, cands))
        if pending and self.client is not None:
            items = [{"i": i, "context": f"{c.get('role') or ''}: {c.get('text')}", "candidates": [{"k": k, "code": x["code"], "display": x["display"]} for k, x in enumerate(cands)]}
                     for i, (c, cands) in enumerate(pending)]
            try:
                res, _ = self.client.json("choose:icd10", PR.CHOOSE_SYSTEM, "Chọn mã ICD-10 VN cho từng chẩn đoán:\n" + json.dumps(items, ensure_ascii=False, indent=1), max_tokens=4000)
                choices = {ch.get("i"): ch for ch in (res or {}).get("choices") or [] if isinstance(ch, dict)}
            except Exception as e:  # noqa: BLE001
                self.log(f"  ! choose:icd10 lỗi: {e}")
                choices = {}
            for i, (c, cands) in enumerate(pending):
                ch = choices.get(i) or {}
                k = ch.get("k")
                if isinstance(k, int) and 0 <= k < len(cands):
                    x = cands[k]
                    c["code"]["codings"] = [C("icd10vn", x["code"], x["display"], "lookup", round(min(1.0, x["ratio"]), 2))]
                    self._add_who(c["code"])
                    c["note"] = ((c.get("note") or "") + f" Mã {x['code']} chọn từ từ điển ICD-10 VN (term_lookup) — {ch.get('reason') or ''}".rstrip()).strip()
                    self.decisions.append({"where": c["id"], "text": c.get("text"), "icd10": x["code"], "rule": "lookup + LLM chọn", "reason": ch.get("reason")})
                else:
                    self.decisions.append({"where": c["id"], "text": c.get("text"), "icd10": None, "rule": "không ứng viên nào khớp nghĩa", "reason": ch.get("reason")})
                    s["unmapped"].append({"item": f"conditions[{c['id']}] '{c.get('text')}'", "reason": "không có mã ICD-10 VN khớp nghĩa trong ứng viên tra cứu", "action": "người duyệt chọn mã"})

    def _add_who(self, code):
        vn = next((x for x in code["codings"] if x["system"] == "icd10vn"), None)
        if not vn or any(x["system"] == "icd10who" for x in code["codings"]):
            return
        info = self.V.icd10_lookup(vn["code"]) or {}
        if info.get("en"):
            code["codings"].append(C("icd10who", vn["code"], info["en"], "conceptmap"))


def strip_internal(obj):
    """Bỏ khoá nội bộ (_harness) trước khi ghi clinical.json."""
    if isinstance(obj, dict):
        return {k: strip_internal(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [strip_internal(x) for x in obj]
    return obj
