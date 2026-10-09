"""compare.py — chấm điểm một bộ FHIR (ứng viên) so với bộ ground truth, theo khoá ngữ nghĩa từng loại resource.

Dùng để: (a) đo pipeline/LLM so với GT đã duyệt; (b) hồi quy khi sửa builder; (c) chấm bài test của skill.
Khớp theo khoá (không theo id, vì id có thể khác giữa hai bộ):
  Patient              identifier CCCD/BHYT/MRN; birthDate; gender
  Encounter            MA_LUOT_KCB | period.start; class; period; discharge; dx count
  Condition            ICD-10 VN code (+ use/category nếu có)   | fallback text chuẩn hoá
  Observation          LOINC code + effective (phút) [+ component]; so giá trị, đơn vị, cờ
  ServiceRequest       LOINC/CLS code | text; authoredOn
  DiagnosticReport     LOINC/CLS code | text; effective; số results; conclusion (fuzzy)
  MedicationRequest    medication text (norm); authoredOn; dosage.text
  Claim/Invoice        tổng; số dòng; từng dòng theo (category, text norm): số tiền + phân bổ
  DocumentReference    hash sha1 | title; type LOINC
  Coverage             BHYT; period
  Practitioner         tên chuẩn hoá; qualification codes
Kết quả: scorecard.json + scorecard.md (precision/recall theo loại, F1 tổng, bảng khác biệt).
  python compare.py <candidate_fhir_dir> <gt_fhir_dir> [--out dir]
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vnlib as V  # noqa: E402

LOINC, CLS, ICD = V.SYS["loinc"], V.SYS["cls"], V.SYS["icd10vn"]
IDT = V.VN + "/CodeSystem/vn-identifier-type-cs"


def load(d: Path) -> dict[str, list[dict]]:
    res = defaultdict(list)
    files = glob.glob(str(d / "resources" / "*.json")) or glob.glob(str(d / "*.json"))
    for f in files:
        o = V.read_json(f)
        if isinstance(o, dict) and o.get("resourceType") == "Bundle":
            for e in o.get("entry", []):
                r = e.get("resource")
                if r:
                    res[r["resourceType"]].append(r)
        elif isinstance(o, dict) and o.get("resourceType"):
            res[o["resourceType"]].append(o)
    # dedupe by type/id
    for t, lst in res.items():
        seen, u = set(), []
        for r in lst:
            if r.get("id") not in seen:
                seen.add(r.get("id")); u.append(r)
        res[t] = u
    return res


def code_of(cc: dict | None, *systems) -> str | None:
    for s in systems:
        for c in (cc or {}).get("coding", []) or []:
            if c.get("system") == s and c.get("code"):
                return f"{s.rsplit('/', 1)[-1]}|{c['code']}"
    return None


def ident(r, type_code):
    for i in r.get("identifier", []) or []:
        if any(c.get("code") == type_code for c in (i.get("type") or {}).get("coding", []) or []):
            return i.get("value")
    return None


def norm(s):
    return V.norm(s or "")[:80]


def minute(s):
    return (s or "")[:16]


def q(v):
    if not isinstance(v, dict):
        return None
    return {"value": v.get("value"), "unit": v.get("unit"), "code": v.get("code")}


# --- key + compare field extractors per type
def k_patient(r):
    return ident(r, "CCCD") or ident(r, "BHYT") or ident(r, "MRN") or "patient"
def f_patient(r):
    return {"birthDate": r.get("birthDate"), "gender": r.get("gender"), "name": norm((r.get("name") or [{}])[0].get("text")), "CCCD": ident(r, "CCCD"), "BHYT": ident(r, "BHYT"), "MRN": ident(r, "MRN"),
            "province": next((e["valueCoding"]["code"] for a in r.get("address", []) for e in a.get("extension", []) if e.get("url", "").endswith("vn-ext-province")), None)}
def k_encounter(r):
    return ident(r, "MA_LUOT_KCB") or minute((r.get("period") or {}).get("start"))
def f_encounter(r):
    return {"class": (r.get("class") or {}).get("code"), "start": minute((r.get("period") or {}).get("start")), "end": minute((r.get("period") or {}).get("end")),
            "discharge": code_of((r.get("hospitalization") or {}).get("dischargeDisposition"), V.VN + "/CodeSystem/vn-discharge-disposition-cs"), "dx": len(r.get("diagnosis", []) or []),
            "type": code_of((r.get("type") or [{}])[0], V.VN + "/CodeSystem/vn-encounter-type-cs")}
def k_condition(r):
    return code_of(r.get("code"), ICD) or ("text|" + norm((r.get("code") or {}).get("text")))
def f_condition(r):
    return {"text": norm((r.get("code") or {}).get("text")), "verification": code_of(r.get("verificationStatus"), "http://terminology.hl7.org/CodeSystem/condition-ver-status"), "recorded": minute(r.get("recordedDate"))}
def k_observation(r):
    c = code_of(r.get("code"), LOINC) or ("text|" + norm((r.get("code") or {}).get("text")))
    return f"{c}@{minute(r.get('effectiveDateTime') or (r.get('effectivePeriod') or {}).get('start'))}"
def f_observation(r):
    comps = {code_of(c.get("code"), LOINC): q(c.get("valueQuantity")) for c in r.get("component", []) or []}
    return {"value": q(r.get("valueQuantity")) or r.get("valueString") or (r.get("valueCodeableConcept") or {}).get("text") or code_of(r.get("dataAbsentReason"), "http://terminology.hl7.org/CodeSystem/data-absent-reason"),
            "interp": code_of((r.get("interpretation") or [{}])[0], "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"), "cls": code_of(r.get("code"), CLS), "components": comps or None,
            "ref_low": ((r.get("referenceRange") or [{}])[0].get("low") or {}).get("value"), "ref_high": ((r.get("referenceRange") or [{}])[0].get("high") or {}).get("value")}
def k_sr(r):
    return code_of(r.get("code"), LOINC) or code_of(r.get("code"), CLS) or ("text|" + norm((r.get("code") or {}).get("text")))
def f_sr(r):
    return {"authoredOn": minute(r.get("authoredOn")), "cls": code_of(r.get("code"), CLS), "status": r.get("status")}
def k_dr(r):
    return code_of(r.get("code"), LOINC) or code_of(r.get("code"), CLS) or ("text|" + norm((r.get("code") or {}).get("text")))
def f_dr(r):
    return {"effective": minute(r.get("effectiveDateTime")), "results": len(r.get("result", []) or []), "conclusion": norm(r.get("conclusion")), "status": r.get("status")}
def k_mr(r):
    return norm((r.get("medicationCodeableConcept") or {}).get("text") or (r.get("medicationReference") or {}).get("display"))
def f_mr(r):
    return {"authoredOn": minute(r.get("authoredOn")), "dosage": norm((r.get("dosageInstruction") or [{}])[0].get("text")), "status": r.get("status")}
def k_doc(r):
    att = ((r.get("content") or [{}])[0].get("attachment") or {})
    return att.get("hash") or norm(att.get("title") or r.get("description"))
def f_doc(r):
    return {"type": code_of(r.get("type"), LOINC), "date": minute(r.get("date")), "author": len(r.get("author", []) or [])}
def k_cov(r):
    return ident(r, "BHYT") or "coverage"
def f_cov(r):
    return {"start": (r.get("period") or {}).get("start"), "end": (r.get("period") or {}).get("end"), "card": code_of(next((e.get("valueCodeableConcept") for e in r.get("extension", []) if e.get("url", "").endswith("bhyt-card-type")), None), V.VN + "/CodeSystem/vn-bhyt-card-type-cs")}
def k_pr(r):
    return norm((r.get("name") or [{}])[0].get("text"))
def f_pr(r):
    return {"qualifications": sorted(code_of(qq.get("code"), V.VN + "/CodeSystem/vn-qualification-cs") or "" for qq in r.get("qualification", []) or [])}
def k_claim(r):
    return "claim"
def f_claim(r):
    items = {}
    for it in r.get("item", []) or []:
        cat = code_of(it.get("category"), V.VN + "/CodeSystem/vn-cost-category-cs")
        items[f"{cat}|{norm((it.get('productOrService') or {}).get('text'))}"] = (it.get("net") or {}).get("value")
    return {"total": (r.get("total") or {}).get("value"), "items": len(items), "dx": [code_of(d.get("diagnosisCodeableConcept"), ICD) for d in r.get("diagnosis", []) or []], "lines": items}
def k_invoice(r):
    return "invoice"
def f_invoice(r):
    lines = {}
    for li in r.get("lineItem", []) or []:
        ext = next((e for e in li.get("extension", []) if e.get("url", "").endswith("cost-statement-line")), {})
        cat = next((code_of(s.get("valueCodeableConcept"), V.VN + "/CodeSystem/vn-cost-category-cs") for s in ext.get("extension", []) if s.get("url") == "category"), None)
        base = next((pc for pc in li.get("priceComponent", []) if pc.get("type") == "base"), {})
        lines[f"{cat}|{norm((li.get('chargeItemCodeableConcept') or {}).get('text'))}"] = (base.get("amount") or {}).get("value")
    return {"totalNet": (r.get("totalNet") or {}).get("value"), "lines": lines, "n": len(lines)}


TYPES = {"Patient": (k_patient, f_patient), "Encounter": (k_encounter, f_encounter), "Condition": (k_condition, f_condition), "Observation": (k_observation, f_observation),
         "ServiceRequest": (k_sr, f_sr), "DiagnosticReport": (k_dr, f_dr), "MedicationRequest": (k_mr, f_mr), "DocumentReference": (k_doc, f_doc), "Coverage": (k_cov, f_cov),
         "Practitioner": (k_pr, f_pr), "Claim": (k_claim, f_claim), "Invoice": (k_invoice, f_invoice)}
COUNT_ONLY = ["Organization", "Location", "PractitionerRole", "Specimen", "Medication", "MedicationAdministration", "AllergyIntolerance", "FamilyMemberHistory", "ClinicalImpression", "CarePlan", "Composition", "Account", "Provenance"]


def diff_fields(a: dict, b: dict) -> list[str]:
    out = []
    for k in sorted(set(a) | set(b)):
        va, vb = a.get(k), b.get(k)
        if isinstance(va, dict) and isinstance(vb, dict) and k == "lines":
            for lk in sorted(set(va) | set(vb)):
                if va.get(lk) != vb.get(lk):
                    out.append(f"line[{lk}]: {va.get(lk)} ≠ GT {vb.get(lk)}")
            continue
        if isinstance(va, str) and isinstance(vb, str) and k in ("conclusion", "dosage", "text", "name"):
            if SequenceMatcher(None, va, vb).ratio() < 0.85:
                out.append(f"{k}: '{va[:40]}' ≁ GT '{vb[:40]}'")
            continue
        if va != vb:
            out.append(f"{k}: {va} ≠ GT {vb}")
    return out


def compare(cand: dict, gt: dict) -> dict:
    per = {}
    diffs = []
    tp_all = fp_all = fn_all = 0
    for t, (kf, ff) in TYPES.items():
        c = {kf(r): r for r in cand.get(t, [])}
        g = {kf(r): r for r in gt.get(t, [])}
        tp = sorted(set(c) & set(g)); fp = sorted(set(c) - set(g)); fn = sorted(set(g) - set(c))
        exact = 0
        for k in tp:
            d = diff_fields(ff(c[k]), ff(g[k]))
            if d:
                diffs.append({"type": t, "key": k, "diff": d})
            else:
                exact += 1
        P = len(tp) / len(c) if c else (1.0 if not g else 0.0)
        R = len(tp) / len(g) if g else 1.0
        per[t] = {"gt": len(g), "candidate": len(c), "matched": len(tp), "exact": exact, "missing": fn[:40], "extra": fp[:40], "precision": round(P, 3), "recall": round(R, 3)}
        tp_all += len(tp); fp_all += len(fp); fn_all += len(fn)
    for t in COUNT_ONLY:
        per[t] = {"gt": len(gt.get(t, [])), "candidate": len(cand.get(t, []))}
    P = tp_all / (tp_all + fp_all) if tp_all + fp_all else 1.0
    R = tp_all / (tp_all + fn_all) if tp_all + fn_all else 1.0
    F = 2 * P * R / (P + R) if P + R else 0.0
    return {"precision": round(P, 3), "recall": round(R, 3), "f1": round(F, 3), "matched": tp_all, "extra": fp_all, "missing": fn_all, "field_diffs": len(diffs), "per_type": per, "diffs": diffs}


def write_md(rep, out: Path, cand_name, gt_name):
    md = [f"# Scorecard — {cand_name} so với GT {gt_name}", "", f"**F1 {rep['f1']}** · precision {rep['precision']} · recall {rep['recall']} · khớp {rep['matched']} · thừa {rep['extra']} · thiếu {rep['missing']} · khác trường {rep['field_diffs']}", "",
          "| Loại | GT | Ứng viên | Khớp | Khớp đúng mọi trường | P | R |", "|---|---|---|---|---|---|---|"]
    for t, s in rep["per_type"].items():
        if "matched" in s:
            md.append(f"| {t} | {s['gt']} | {s['candidate']} | {s['matched']} | {s['exact']} | {s['precision']} | {s['recall']} |")
        else:
            md.append(f"| {t} | {s['gt']} | {s['candidate']} | – | – | – | – |")
    for t, s in rep["per_type"].items():
        if s.get("missing") or s.get("extra"):
            md += ["", f"## {t}"]
            if s.get("missing"):
                md += ["Thiếu so với GT:"] + [f"- {k}" for k in s["missing"]]
            if s.get("extra"):
                md += ["Thừa (không có trong GT):"] + [f"- {k}" for k in s["extra"]]
    if rep["diffs"]:
        md += ["", "## Khác biệt trường trên resource đã khớp", ""] + [f"- **{d['type']}** `{d['key']}`: " + "; ".join(d["diff"]) for d in rep["diffs"]]
    (out / "scorecard.md").write_text("\n".join(md) + "\n", encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("candidate")
    ap.add_argument("gt")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    cand, gt = load(Path(a.candidate)), load(Path(a.gt))
    rep = compare(cand, gt)
    out = Path(a.out) if a.out else Path(a.candidate)
    out.mkdir(parents=True, exist_ok=True)
    V.write_json(rep, out / "scorecard.json")
    write_md(rep, out, Path(a.candidate).name, Path(a.gt).name)
    print(f"[compare] F1 {rep['f1']} (P {rep['precision']}, R {rep['recall']}); khớp {rep['matched']}, thừa {rep['extra']}, thiếu {rep['missing']}, khác trường {rep['field_diffs']} → {out / 'scorecard.md'}")


if __name__ == "__main__":
    main()
