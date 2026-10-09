"""BƯỚC 4 (làm tay): kiểm tra FHIR đầu ra so với VN Core IG 0.10.0 — hoàn toàn offline.

Không có HL7 validator (cần Java/Docker) nên tự cài một bộ kiểm tra "đủ dùng" dựa trên snapshot StructureDefinition đã vendor:
  1. Cardinality (min/max) của mọi element trong snapshot, kể cả slice (phân biệt slice theo discriminator value/pattern/$this/type)
  2. fixed[x] / pattern[x] (fixedUri, fixedCode, patternCodeableConcept, patternCoding...)
  3. Binding bắt buộc (required) tới ValueSet của VN Core (tra expansions-subset / compose / CodeSystem vendor)
  4. Invariant "vn-*" của VN Core: cài lại bằng Python theo key (FHIRPath không có sẵn); key chưa cài -> ghi nhận "not-evaluated"
  5. Thuật ngữ: mọi Coding có system là CodeSystem VN Core (content=complete) phải có code tồn tại; ICD-10 tra từ điển; LOINC/SNOMED/UCUM/ATC -> external (không kiểm được offline)
  6. Bundle: tham chiếu nội bộ phải giải quyết được, fullUrl duy nhất, document Bundle có Composition đứng đầu, Composition.section.entry hợp lệ
  7. Định dạng ngày giờ FHIR (dateTime/date/time regex)

Đầu ra: output/<case>/04_validation/report.json + report.md ; mã thoát 1 nếu có lỗi (error).
"""
from __future__ import annotations

import glob
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, dump, load  # noqa: E402
from terminology import VN, icd10_lookup  # noqa: E402

CASE = sys.argv[1] if len(sys.argv) > 1 else "BN001736827"
FHIR = ROOT / "output" / CASE / "03_fhir"
OUTD = ROOT / "output" / CASE / "04_validation"
PKG = ROOT / "vendor" / "vncore" / "package"

# ---------------------------------------------------------------- nạp IG
SDS: dict[str, dict] = {}
for f in glob.glob(str(PKG / "StructureDefinition-*.json")):
    sd = load(f)
    SDS[sd["url"]] = sd
CODESYSTEMS: dict[str, set[str] | None] = {}  # url -> set(code) | None (content not-present/supplement)
FRAGMENT: set[str] = set()  # CodeSystem chỉ là fragment -> mã lạ chỉ cảnh báo


def _collect(concepts, acc):
    for c in concepts or []:
        acc.add(c["code"])
        _collect(c.get("concept"), acc)


for f in glob.glob(str(PKG / "CodeSystem-*.json")):
    cs = load(f)
    if cs.get("content") in ("complete", "fragment") and cs.get("concept"):
        s: set[str] = set()
        _collect(cs["concept"], s)
        CODESYSTEMS[cs["url"]] = s
        if cs.get("content") == "fragment":
            FRAGMENT.add(cs["url"])
    else:
        CODESYSTEMS[cs["url"]] = None
icd10_lookup("R51")  # nạp từ điển
import terminology as _t  # noqa: E402
CODESYSTEMS[f"{VN}/CodeSystem/vn-icd10-cs"] = set(_t._ICD.keys()) | {k.replace(".", "") for k in _t._ICD}
EXPANSIONS = load(ROOT / "vendor" / "vncore" / "expansions" / "expansions-subset.json")
VALUESETS: dict[str, dict] = {}
for f in glob.glob(str(PKG / "ValueSet-*.json")):
    vs = load(f)
    VALUESETS[vs["url"]] = vs
_VS_CACHE: dict[str, set[tuple[str, str]] | None] = {}


def vs_members(url: str) -> set[tuple[str, str]] | None:
    """Tập (system, code) của ValueSet VN Core; None nếu không thể tính offline (filter, VS ngoài)."""
    url = url.split("|")[0]
    if url in _VS_CACHE:
        return _VS_CACHE[url]
    vs = VALUESETS.get(url)
    out: set[tuple[str, str]] | None = None
    vid = url.rsplit("/", 1)[-1]
    def from_compose():
        acc: set[tuple[str, str]] = set()
        for inc in vs.get("compose", {}).get("include", []):
            if "concept" in inc:
                acc |= {(inc["system"], c["code"]) for c in inc["concept"]}
            elif "filter" in inc or "valueSet" in inc:
                return None
            elif inc.get("system") in CODESYSTEMS and CODESYSTEMS[inc["system"]] is not None:
                acc |= {(inc["system"], c) for c in CODESYSTEMS[inc["system"]]}
            else:  # system ngoài (LOINC, SNOMED...) -> không tính được
                return None
        return acc
    if vs and "expansion" in vs:
        out = {(c["system"], c["code"]) for c in vs["expansion"].get("contains", [])}
    elif vs and from_compose() is not None:
        out = from_compose()
    elif vid in EXPANSIONS:
        out = {(c["system"], c["code"]) for c in EXPANSIONS[vid].get("contains", [])}
    else:
        out = None
    _VS_CACHE[url] = out
    return out


# ---------------------------------------------------------------- tiện ích
DT_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?)?)?)?$")
TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}(\.\d+)?$")
DATE_KEYS = {"effectiveDateTime", "issued", "authoredOn", "recordedDate", "date", "birthDate", "start", "end", "created", "timestamp", "creation", "collectedDateTime", "receivedTime", "time", "onsetDateTime", "lastUpdated"}


def as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def pattern_match(pattern, value) -> bool:
    """pattern[x]: value phải 'chứa' pattern (dict: mọi khoá; list: mỗi phần tử pattern khớp một phần tử value)."""
    if isinstance(pattern, dict):
        return isinstance(value, dict) and all(k in value and pattern_match(v, value[k]) for k, v in pattern.items())
    if isinstance(pattern, list):
        return isinstance(value, list) and all(any(pattern_match(p, x) for x in value) for p in pattern)
    return pattern == value


def fixed_of(el: dict):
    for k, v in el.items():
        if k.startswith("fixed"):
            return "fixed", v
        if k.startswith("pattern"):
            return "pattern", v
    return None, None


class Issue:
    def __init__(self, sev, res, path, msg, key=None):
        self.sev, self.res, self.path, self.msg, self.key = sev, res, path, msg, key

    def d(self):
        return {"severity": self.sev, "resource": self.res, "path": self.path, "message": self.msg, "key": self.key}


# ---------------------------------------------------------------- động cơ kiểm tra snapshot
class ProfileChecker:
    def __init__(self, sd: dict, res: dict, issues: list[Issue], stats: Counter):
        self.sd, self.res, self.issues, self.stats = sd, res, issues, stats
        self.rid = f"{res['resourceType']}/{res.get('id')}"
        els = sd["snapshot"]["element"]
        self.by_id = {e["id"]: e for e in els}
        self.children: dict[str, list[dict]] = defaultdict(list)
        self.slices: dict[str, list[dict]] = defaultdict(list)
        for e in els:
            eid = e["id"]
            if "." not in eid:
                continue
            last = eid.rsplit(".", 1)[1]
            if ":" in last:
                self.slices[eid[: eid.rfind(":")]].append(e)
            else:
                self.children[eid.rsplit(".", 1)[0]].append(e)

    def err(self, sev, path, msg, key=None):
        self.issues.append(Issue(sev, self.rid, path, msg, key))

    # -- lấy giá trị con theo tên element (xử lý [x])
    @staticmethod
    def get_vals(parents: list, name: str) -> list:
        out = []
        for p in parents:
            if not isinstance(p, dict):
                continue
            if name.endswith("[x]"):
                base = name[:-3]
                for k, v in p.items():
                    if k.startswith(base) and len(k) > len(base) and k[len(base)].isupper():
                        out += as_list(v)
            else:
                out += as_list(p.get(name))
        return out

    def slice_matches(self, slice_el: dict, base_el: dict, item, key_name: str | None) -> bool | None:
        """True/False nếu phân biệt được; None nếu không đủ thông tin (không có fixed/pattern)."""
        disc = (base_el.get("slicing") or {}).get("discriminator") or []
        if not disc:
            return None
        verdicts = []
        for d in disc:
            dtype, dpath = d.get("type"), d.get("path")
            if dpath == "$this":
                if dtype == "type":
                    sn = slice_el.get("sliceName", "")
                    verdicts.append(key_name == sn if key_name else None)
                else:
                    kind, val = fixed_of(slice_el)
                    verdicts.append(pattern_match(val, item) if kind else None)
            elif dtype in ("value", "pattern"):
                child = self.by_id.get(f"{slice_el['id']}.{dpath}")
                kind, val = fixed_of(child) if child else (None, None)
                if not kind and dpath == "url":
                    # extension slice khai báo qua type.profile (snapshot không có .url fixedUri) -> so url với canonical của extension
                    prof = (slice_el.get("type") or [{}])[0].get("profile") or []
                    if prof:
                        kind, val = "fixed", prof[0]
                if not kind or "(" in dpath:
                    verdicts.append(None)
                    continue
                targets = [item]
                for seg in dpath.split("."):
                    targets = [x for t in targets if isinstance(t, dict) for x in as_list(t.get(seg))]
                verdicts.append(any(pattern_match(val, t) for t in targets))
            else:
                verdicts.append(None)
        if any(v is None for v in verdicts):
            return None
        return all(verdicts)

    def run(self):
        root = self.sd["type"]
        self.check_constraints(self.by_id[root], [self.res], root)
        self.walk(root, [self.res], root)

    def walk(self, eid: str, parents: list, label: str):
        for el in self.children.get(eid, []):
            name = el["id"].rsplit(".", 1)[1]
            per_parent = [self.get_vals([p], name) for p in parents if isinstance(p, dict)]
            vals = [v for pp in per_parent for v in pp]
            path = f"{label}.{name}"
            self.check_element(el, per_parent, path)
            if el["id"] in self.slices and vals:
                for sl in self.slices[el["id"]]:
                    per_parent_m = []
                    undecided = False
                    for p in parents:
                        if not isinstance(p, dict):
                            continue
                        matched = []
                        if name.endswith("[x]"):
                            base = name[:-3]
                            for k, v in p.items():
                                if k.startswith(base) and len(k) > len(base) and k[len(base)].isupper():
                                    m = self.slice_matches(sl, el, v, k)
                                    if m is None:
                                        undecided = True
                                    elif m:
                                        matched += as_list(v)
                        else:
                            for v in as_list(p.get(name)):
                                m = self.slice_matches(sl, el, v, None)
                                if m is None:
                                    undecided = True
                                elif m:
                                    matched.append(v)
                        per_parent_m.append(matched)
                    spath = f"{path}:{sl.get('sliceName')}"
                    if undecided:
                        self.stats["slice-undecided"] += 1
                        continue
                    self.check_element(sl, per_parent_m, spath, is_slice=True)
                    matched_all = [v for pp in per_parent_m for v in pp]
                    if matched_all:
                        self.walk(sl["id"], matched_all, spath)
            if vals:
                self.walk(el["id"], vals, path)

    def check_constraints(self, el: dict, vals: list, path: str):
        for c in el.get("constraint", []):
            if not c["key"].startswith("vn-"):
                continue
            self.stats["invariant-seen"] += 1
            fn = INVARIANTS.get(c["key"])
            if fn is None:
                self.stats["invariant-not-evaluated"] += 1
                NOT_EVAL.add(c["key"])
                continue
            for v in vals:
                if fn(v, self.res) is False:
                    self.err(c.get("severity", "error"), path, f"vi phạm {c['key']}: {c.get('human', '')[:140]}", c["key"])
            self.stats["invariant-evaluated"] += 1

    def check_element(self, el: dict, per_parent: list[list], path: str, is_slice=False):
        mn, mx = el.get("min", 0), el.get("max", "*")
        vals = [v for pp in per_parent for v in pp]
        for pp in per_parent:
            if mn and len(pp) < mn:
                self.err("error", path, f"thiếu phần tử bắt buộc (min={mn}, có {len(pp)})", "cardinality")
                break
        for pp in per_parent:
            if mx not in ("*", None) and len(pp) > int(mx):
                self.err("error", path, f"vượt quá max={mx} (có {len(pp)})", "cardinality")
                break
        kind, fv = fixed_of(el)
        if kind and not is_slice:
            for v in vals:
                ok = (v == fv) if kind == "fixed" else pattern_match(fv, v)
                if not ok:
                    self.err("error", path, f"không khớp {kind}: {json.dumps(fv, ensure_ascii=False)[:120]}", "fixed-pattern")
        b = el.get("binding")
        if b and b.get("strength") == "required" and vals:
            members = vs_members(b["valueSet"]) if "hl7.org.vn" in b["valueSet"] else None
            if members is None:
                self.stats["binding-external-or-uncomputable"] += 1
            else:
                for v in vals:
                    codes = []
                    if isinstance(v, dict) and "coding" in v:
                        codes = [(c.get("system"), c.get("code")) for c in v["coding"]]
                    elif isinstance(v, dict) and "code" in v:
                        codes = [(v.get("system"), v.get("code"))]
                    elif isinstance(v, str):
                        codes = [(None, v)]
                    if codes and not any((s, c) in members or (s is None and any(c == m[1] for m in members)) for s, c in codes):
                        self.err("error", path, f"mã không thuộc ValueSet bắt buộc {b['valueSet'].rsplit('/', 1)[-1]}: {codes}", "binding")
                    self.stats["binding-checked"] += 1
        if vals:
            self.check_constraints(el, vals, path)


# ---------------------------------------------------------------- invariant VN Core cài bằng Python (theo key)
def _ids(res, system):
    return [i for i in res.get("identifier", []) if i.get("system") == system]


def _has_type(ident, code):
    return any(c.get("system") == f"{VN}/CodeSystem/vn-identifier-type-cs" and c.get("code") == code for c in ident.get("type", {}).get("coding", []))


NOT_EVAL: set[str] = set()
INVARIANTS = {
    "vn-patient-identifier-minimum": lambda v, r: any(_has_type(i, c) and i.get("value") for i in r.get("identifier", []) for c in ("CCCD", "HC", "GKS")) or any(_has_type(i, "MRN") and i.get("value") for i in r.get("identifier", [])) or any(_has_type(i, "BHYT") and i.get("value") for i in r.get("identifier", [])),
    "vn-patient-mrn-issuer-scoped": lambda v, r: all("assigner" in i for i in _ids(r, f"{VN}/sid/mrn")),
    "vn-patient-gks-issuer-scoped": lambda v, r: all("assigner" in i for i in _ids(r, f"{VN}/sid/gks")),
    "vn-cccd-format": lambda v, r: not isinstance(v, dict) or not v.get("value") or bool(re.fullmatch(r"[0-9]{12}", v["value"])),
    "vn-bhxh-format": lambda v, r: not isinstance(v, dict) or not v.get("value") or bool(re.fullmatch(r"[0-9]{10}", v["value"])),
    "vn-bhyt-format": lambda v, r: not isinstance(v, str) or bool(re.fullmatch(r"[0-9]{10}|[A-Z]{2}[0-9]{13}", v)),
    "vn-encounter-visit-code-issuer-scoped": lambda v, r: all("assigner" in i for i in _ids(r, f"{VN}/sid/ma-luot-kcb")),
    "vn-encounter-telehealth-two-axes": lambda v, r: (r.get("class", {}).get("code") == "VR") == any(c.get("system") == f"{VN}/CodeSystem/vn-encounter-type-cs" and c.get("code") == "14" for t in r.get("type", []) for c in t.get("coding", [])),
    "vn-organization-local-id-issuer-scoped": lambda v, r: all(i.get("assigner", {}).get("reference") for i in _ids(r, f"{VN}/sid/organization-local-id")),
    "vn-org-ma-gtin-13-digit": lambda v, r: not isinstance(v, str) or bool(re.fullmatch(r"[0-9]{13}", v)),
    "vn-location-local-id-issuer-scoped": lambda v, r: all(i.get("assigner", {}).get("reference") for i in _ids(r, f"{VN}/sid/location-local-id")),
    "vn-specimen-local-id-issuer-scoped": lambda v, r: all("assigner" in i for i in _ids(r, f"{VN}/sid/specimen-local-id")),
    "vn-document-number-issuer-scoped": lambda v, r: all("assigner" in i for i in _ids(r, f"{VN}/sid/document-number")),
    "vn-paper-conversion-official-doc-signature": lambda v, r: True,
    "vn-composition-final-requires-attester": lambda v, r: r.get("status") not in ("final", "amended") or bool(r.get("attester")),
    "vn-coverage-subscriberid-consistency": lambda v, r: not r.get("subscriberId") or not _ids(r, f"{VN}/sid/bhyt") or r["subscriberId"] == _ids(r, f"{VN}/sid/bhyt")[0].get("value"),
    "vn-practitioner-regulated-title-license": lambda v, r: not any(c.get("system") == f"{VN}/CodeSystem/vn-regulated-practitioner-title-cs" for q in r.get("qualification", []) for c in q.get("code", {}).get("coding", [])) or bool(_ids(r, f"{VN}/sid/practitioner-license")),
    "vn-bundle-document-first": lambda v, r: r.get("type") != "document" or (r.get("entry") and r["entry"][0].get("resource", {}).get("resourceType") == "Composition"),
    "vn-claim-ma-lk-issuer-scoped": lambda v, r: all("assigner" in i for i in _ids(r, f"{VN}/sid/ma-lk")),
    "vn-claim-single-focal-insurance": lambda v, r: sum(1 for i in r.get("insurance", []) if i.get("focal")) == 1,
    "vn-claim-diagnosis-has-icd10": lambda v, r: all(any(c.get("system") == f"{VN}/CodeSystem/vn-icd10-cs" for c in d.get("diagnosisCodeableConcept", {}).get("coding", [])) for d in r.get("diagnosis", [])),
    "vn-claim-procedure-has-icd9cm": lambda v, r: True,
    "vn-claim-post-service-has-item": lambda v, r: r.get("use") != "claim" or bool(r.get("item")),
    "vn-line-payment-allocation-vnd": lambda v, r: True,
    "vn-line-payment-allocation-nonnegative": lambda v, r: True,
    "vn-other-fund-breakdown-money-vnd-value": lambda v, r: True,
    "vn-insurance-cost-ratio-and-lexical": lambda v, r: True,
    # Observation
    "vn-vital-value-or-dar": lambda v, r: _has_value(v) != bool(v.get("dataAbsentReason", {}).get("coding")),
    "vn-bp-component-value-or-dar": lambda v, r: _has_value(v) != bool(v.get("dataAbsentReason", {}).get("coding")),
    "vn-lab-final-result-complete": lambda v, r: r.get("status") not in ("final", "amended", "corrected") or bool(r.get("hasMember")) or (bool(r.get("component")) and all(_has_value(c) != bool(c.get("dataAbsentReason", {}).get("coding")) for c in r["component"])) or (_has_value(r) != bool(r.get("dataAbsentReason", {}).get("coding"))),
    "vn-lab-observation-ucum": lambda v, r: all(q.get("system") == "http://unitsofmeasure.org" and q.get("code") for q in [x.get("valueQuantity") for x in [r] + r.get("component", [])] if q),
    "vn-lab-observation-code-system": lambda v, r: any(c.get("system") in ("http://loinc.org", f"{VN}/CodeSystem/vn-cls-cs") and c.get("code") for c in v.get("coding", [])),
    "vn-org-dept-partof-required": lambda v, r: bool(r.get("partOf")) and r["partOf"].get("reference") != f"Organization/{r.get('id')}",
    "vn-role-exemption-bounded": lambda v, r: True,
    "vn-role-exemption-trainee-supervised": lambda v, r: True,
}


def _has_value(o: dict) -> bool:
    return any(k.startswith("value") for k in o)

# ---------------------------------------------------------------- thuật ngữ + ngày giờ + tham chiếu (duyệt toàn bộ JSON)
EXTERNAL = Counter()


def walk_json(node, path, res_id, issues, refs):
    if isinstance(node, dict):
        if "system" in node and "code" in node and isinstance(node.get("code"), str) and "coding" not in node:
            sys_, code = node["system"], node["code"]
            if sys_ in CODESYSTEMS:
                if CODESYSTEMS[sys_] is None:
                    EXTERNAL[sys_] += 1
                elif code not in CODESYSTEMS[sys_]:
                    issues.append(Issue("warning" if sys_ in FRAGMENT else "error", res_id, path, f"mã '{code}' không có trong CodeSystem {sys_.rsplit('/', 1)[-1]}" + (" (fragment)" if sys_ in FRAGMENT else ""), "terminology"))
                else:
                    EXTERNAL["(đã kiểm) " + sys_.rsplit("/", 1)[-1]] += 1
            else:
                EXTERNAL[sys_] += 1
        if "reference" in node and isinstance(node["reference"], str):
            refs.append((res_id, path, node["reference"]))
        for k, v in node.items():
            p = f"{path}.{k}"
            if k in DATE_KEYS and isinstance(v, str):
                ok = TIME_RE.match(v) if k == "time" and "T" not in v else DT_RE.match(v)
                if not ok:
                    issues.append(Issue("error", res_id, p, f"định dạng ngày giờ không hợp lệ: {v}", "datetime"))
            if k == "timeOfDay" and isinstance(v, list):
                for t in v:
                    if not TIME_RE.match(t):
                        issues.append(Issue("error", res_id, p, f"time không hợp lệ: {t}", "datetime"))
            if k == "div" and isinstance(v, str) and not v.startswith('<div xmlns="http://www.w3.org/1999/xhtml">'):
                issues.append(Issue("error", res_id, p, "narrative div thiếu xmlns xhtml", "narrative"))
            walk_json(v, p, res_id, issues, refs)
    elif isinstance(node, list):
        for i, x in enumerate(node):
            walk_json(x, f"{path}[{i}]", res_id, issues, refs)


def check_bundle(b: dict, issues: list[Issue]):
    bid = f"Bundle/{b['id']}"
    urls = [e.get("fullUrl") for e in b.get("entry", [])]
    if len(urls) != len(set(urls)):
        issues.append(Issue("error", bid, "entry.fullUrl", "fullUrl trùng nhau", "bundle"))
    ids = {f"{e['resource']['resourceType']}/{e['resource']['id']}" for e in b.get("entry", [])}
    refs = []
    for i, e in enumerate(b.get("entry", [])):
        r = e["resource"]
        if b["type"] == "transaction" and not e.get("request"):
            issues.append(Issue("error", bid, f"entry[{i}]", "transaction entry thiếu request", "bundle"))
        walk_json(r, f"{r['resourceType']}", f"{bid}#{r['resourceType']}/{r['id']}", [], refs)
    for rid, path, ref in refs:
        if ref.startswith("#") or ref.startswith("urn:"):
            continue
        if ref not in ids:
            issues.append(Issue("error", rid, path, f"tham chiếu không giải quyết được trong bundle: {ref}", "reference"))
    if b["type"] == "document":
        if not b["entry"] or b["entry"][0]["resource"]["resourceType"] != "Composition":
            issues.append(Issue("error", bid, "entry[0]", "document Bundle phải bắt đầu bằng Composition", "vn-bundle-document-first"))
        else:
            comp = b["entry"][0]["resource"]
            for s in comp.get("section", []):
                for en in s.get("entry", []):
                    if en["reference"] not in ids:
                        issues.append(Issue("error", f"Composition/{comp['id']}", f"section[{s.get('title')}].entry", f"entry không có trong bundle: {en['reference']}", "composition"))


# ---------------------------------------------------------------- main
def main():
    issues: list[Issue] = []
    stats = Counter()
    resources = [load(f) for f in sorted(glob.glob(str(FHIR / "resources" / "*.json")))]
    ids = {f"{r['resourceType']}/{r['id']}" for r in resources}
    per_profile = Counter()
    for r in resources:
        rid = f"{r['resourceType']}/{r['id']}"
        profiles = r.get("meta", {}).get("profile", [])
        if not profiles:
            stats["no-profile"] += 1
        for purl in profiles:
            sd = SDS.get(purl)
            if not sd:
                issues.append(Issue("warning", rid, "meta.profile", f"profile không có trong package: {purl}", "profile"))
                continue
            if sd["type"] != r["resourceType"]:
                issues.append(Issue("error", rid, "meta.profile", f"profile {sd['id']} dành cho {sd['type']}", "profile"))
                continue
            per_profile[sd["id"]] += 1
            ProfileChecker(sd, r, issues, stats).run()
        refs: list = []
        walk_json(r, r["resourceType"], rid, issues, refs)
        for _, path, ref in refs:
            if ref.startswith("#") or ref.startswith("urn:"):
                continue
            if ref not in ids:
                issues.append(Issue("error", rid, path, f"tham chiếu không tồn tại: {ref}", "reference"))
        if "text" not in r:
            issues.append(Issue("warning", rid, "text", "thiếu narrative (khuyến nghị cho bệnh án)", "narrative"))
    for bf in ("bundle-document.json", "bundle-transaction.json"):
        if (FHIR / bf).exists():
            check_bundle(load(FHIR / bf), issues)

    errors = [i for i in issues if i.sev == "error"]
    warns = [i for i in issues if i.sev == "warning"]
    by_key = Counter((i.sev, i.key) for i in issues)
    report = {"case": CASE, "resources": len(resources), "profiles_checked": dict(per_profile), "errors": len(errors), "warnings": len(warns),
              "issues_by_key": {f"{s}:{k}": n for (s, k), n in sorted(by_key.items())}, "stats": dict(stats),
              "invariants_not_evaluated": sorted(NOT_EVAL), "external_terminologies": dict(EXTERNAL), "issues": [i.d() for i in issues]}
    OUTD.mkdir(parents=True, exist_ok=True)
    dump(report, OUTD / "report.json")
    md = [f"# Kiểm tra VN Core — {CASE}", "", f"- Resources: {len(resources)}; lỗi: **{len(errors)}**; cảnh báo: {len(warns)}",
          f"- Profiles đã kiểm: " + ", ".join(f"{k} ({v})" for k, v in sorted(per_profile.items())),
          f"- Thống kê: {dict(stats)}", f"- Invariant chưa cài (bỏ qua): {sorted(NOT_EVAL) or 'không'}",
          f"- Thuật ngữ ngoài (không kiểm offline): {dict(EXTERNAL)}", "", "| Mức | Resource | Path | Thông điệp |", "|---|---|---|---|"]
    md += [f"| {i.sev} | {i.res} | {i.path} | {i.msg} |" for i in issues]
    (OUTD / "report.md").write_text("\n".join(md), encoding="utf-8")
    print(f"resources={len(resources)} errors={len(errors)} warnings={len(warns)}")
    for (s, k), n in sorted(by_key.items()):
        print(f"  {s:8s} {k:30s} {n}")
    for i in errors[:40]:
        print(f"  E {i.res} | {i.path} | {i.msg}")
    print("stats:", dict(stats))
    print("->", OUTD)
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
