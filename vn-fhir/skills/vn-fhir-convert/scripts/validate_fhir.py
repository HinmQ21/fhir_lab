"""validate_fhir.py — kiểm tra FHIR đầu ra so với VN Core IG 0.10.0, hoàn toàn offline (không cần Java/HL7 validator).

Cách dùng:
    python validate_fhir.py <fhir_dir> [--out <report_dir>] [--vendor <dir>] [--strict]
    <fhir_dir> chứa resources/*.json và bundle-*.json (đầu ra của build_fhir.py); có thể trỏ thẳng tới một file .json.

Kiểm tra:
  1. Cardinality (min/max) của mọi element trong snapshot StructureDefinition, kể cả slice (discriminator value/pattern/$this/type)
  2. fixed[x] / pattern[x]
  3. Binding bắt buộc (required) tới ValueSet VN Core (tính offline từ compose/expansion)
  4. Invariant vn-* của VN Core cài lại bằng Python theo key (FHIRPath không có sẵn); key chưa cài -> "not-evaluated"
  5. Thuật ngữ: mã trong CodeSystem VN Core content=complete phải tồn tại; ICD-10 tra từ điển; LOINC: check digit + bảng đã xác minh
     (assets/loinc_verified.json: mã không tồn tại -> error, deprecated -> warning, chưa xác minh -> info)
  6. Bundle: profile của Bundle (slice entry theo type:resource), fullUrl duy nhất, tham chiếu nội bộ, document Bundle bắt đầu bằng Composition
  7. Định dạng dateTime/date/time; narrative có xmlns xhtml

Mã thoát: 1 nếu có error (hoặc có warning khi --strict). Báo cáo: <out>/report.json + report.md.
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vnlib as V  # noqa: E402
from vnlib import SID, SYS, VN  # noqa: E402

ASSETS = Path(__file__).resolve().parent.parent / "assets"
SDS: dict[str, dict] = {}
CODESYSTEMS: dict[str, set[str] | None] = {}
FRAGMENT: set[str] = set()
VALUESETS: dict[str, dict] = {}
EXPANSIONS: dict = {}
LOINC_VERIFIED: dict = {}
_VS_CACHE: dict = {}
NOT_EVAL: set[str] = set()
EXTERNAL = Counter()


def _collect(concepts, acc):
    for c in concepts or []:
        acc.add(c["code"])
        _collect(c.get("concept"), acc)


def load_ig(vendor_dir: Path):
    pkg = vendor_dir / "vncore" / "package"
    for f in glob.glob(str(pkg / "StructureDefinition-*.json")):
        sd = V.read_json(f)
        SDS[sd["url"]] = sd
    for d in ("package", "terminology"):
        for f in glob.glob(str(vendor_dir / "vncore" / d / "CodeSystem-*.json")):
            cs = V.read_json(f)
            if cs["url"] in CODESYSTEMS and CODESYSTEMS[cs["url"]] is not None:
                continue
            if cs.get("content") in ("complete", "fragment") and cs.get("concept"):
                s: set[str] = set()
                _collect(cs["concept"], s)
                CODESYSTEMS[cs["url"]] = s
                if cs.get("content") == "fragment":
                    FRAGMENT.add(cs["url"])
            else:
                CODESYSTEMS[cs["url"]] = None
    icd = V._icd()
    CODESYSTEMS[SYS["icd10vn"]] = set(icd.keys()) | {k.replace(".", "") for k in icd}
    exp = vendor_dir / "vncore" / "expansions" / "expansions-subset.json"
    EXPANSIONS.update(V.read_json(exp) if exp.exists() else {})
    for f in glob.glob(str(pkg / "ValueSet-*.json")):
        vs = V.read_json(f)
        VALUESETS[vs["url"]] = vs
    lv = ASSETS / "loinc_verified.json"
    if lv.exists():
        LOINC_VERIFIED.update({k: v for k, v in V.read_json(lv).items() if k != "_meta"})


def vs_members(url: str):
    url = url.split("|")[0]
    if url in _VS_CACHE:
        return _VS_CACHE[url]
    vs = VALUESETS.get(url)
    vid = url.rsplit("/", 1)[-1]
    out = None

    def from_compose():
        acc = set()
        for inc in vs.get("compose", {}).get("include", []):
            if "concept" in inc:
                acc |= {(inc["system"], c["code"]) for c in inc["concept"]}
            elif "filter" in inc or "valueSet" in inc:
                return None
            elif inc.get("system") in CODESYSTEMS and CODESYSTEMS[inc["system"]] is not None:
                acc |= {(inc["system"], c) for c in CODESYSTEMS[inc["system"]]}
            else:
                return None
        return acc
    if vs and "expansion" in vs:
        out = {(c["system"], c["code"]) for c in vs["expansion"].get("contains", [])}
    elif vs and from_compose() is not None:
        out = from_compose()
    elif vid in EXPANSIONS:
        out = {(c["system"], c["code"]) for c in EXPANSIONS[vid].get("contains", [])}
    _VS_CACHE[url] = out
    return out


# ---------------------------------------------------------------- tiện ích
DT_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?)?)?)?$")
DATE_ONLY_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}(\.\d+)?$")
DT_KEYS = {"effectiveDateTime", "issued", "authoredOn", "recordedDate", "date", "start", "end", "created", "timestamp", "creation", "collectedDateTime", "receivedTime", "time",
           "onsetDateTime", "lastUpdated", "occurredDateTime", "recorded", "valueDateTime", "occurrenceDateTime", "whenHandedOver", "dateAsserted"}
DATE_KEYS = {"birthDate", "servicedDate", "valueDate", "expirationDate", "billablePeriod"}
ID_RE = re.compile(r"^[A-Za-z0-9\-\.]{1,64}$")


def as_list(v):
    return [] if v is None else (v if isinstance(v, list) else [v])


def pattern_match(pattern, value) -> bool:
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
    def __init__(self, sd: dict, res: dict, issues: list, stats: Counter):
        self.sd, self.res, self.issues, self.stats = sd, res, issues, stats
        self.rid = f"{res['resourceType']}/{res.get('id')}"
        els = sd["snapshot"]["element"]
        self.by_id = {e["id"]: e for e in els}
        self.children = defaultdict(list)
        self.slices = defaultdict(list)
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

    @staticmethod
    def get_vals(parents, name):
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

    def slice_matches(self, slice_el, base_el, item, key_name):
        disc = (base_el.get("slicing") or {}).get("discriminator") or []
        if not disc:
            return None
        verdicts = []
        for d in disc:
            dtype, dpath = d.get("type"), d.get("path")
            if dpath == "$this":
                if dtype == "type":
                    verdicts.append(key_name == slice_el.get("sliceName", "") if key_name else None)
                else:
                    kind, val = fixed_of(slice_el)
                    verdicts.append(pattern_match(val, item) if kind else None)
            elif dtype == "type":
                child = self.by_id.get(f"{slice_el['id']}.{dpath}")
                types = (child or {}).get("type") or []
                if not types or not isinstance(item, dict):
                    verdicts.append(None)
                    continue
                target = item.get(dpath)
                want = set()
                for t in types:
                    for prof in t.get("profile") or []:
                        sd = SDS.get(prof)
                        want.add(sd["type"] if sd else t["code"])
                    if not t.get("profile"):
                        want.add(t["code"])
                verdicts.append(isinstance(target, dict) and target.get("resourceType") in want)
            elif dtype in ("value", "pattern"):
                child = self.by_id.get(f"{slice_el['id']}.{dpath}")
                kind, val = fixed_of(child) if child else (None, None)
                if not kind and dpath == "url":
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

    def walk(self, eid, parents, label):
        for el in self.children.get(eid, []):
            name = el["id"].rsplit(".", 1)[1]
            per_parent = [self.get_vals([p], name) for p in parents if isinstance(p, dict)]
            vals = [v for pp in per_parent for v in pp]
            path = f"{label}.{name}"
            self.check_element(el, per_parent, path)
            if el["id"] in self.slices and vals:
                for sl in self.slices[el["id"]]:
                    per_parent_m, undecided = [], False
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

    def check_constraints(self, el, vals, path):
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
                try:
                    ok = fn(v, self.res)
                except Exception as e:  # noqa
                    ok = None
                    self.err("warning", path, f"không đánh giá được {c['key']}: {e}", c["key"])
                if ok is False:
                    self.err(c.get("severity", "error"), path, f"vi phạm {c['key']}: {c.get('human', '')[:160]}", c["key"])
            self.stats["invariant-evaluated"] += 1

    def check_element(self, el, per_parent, path, is_slice=False):
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


def _ext(node, url):
    return [e for e in (node or {}).get("extension", []) if e.get("url") == url]


def _sub(ext, url):
    return [e for e in (ext or {}).get("extension", []) if e.get("url") == url]


def _subval(ext, url, key):
    s = _sub(ext, url)
    return s[0].get(key) if s else None


def _has_value(o):
    return any(k.startswith("value") for k in o)


def _moneys(node, acc=None):
    acc = [] if acc is None else acc
    if isinstance(node, dict):
        if "currency" in node and "value" in node and len(node) <= 3:
            acc.append(node)
        for v in node.values():
            _moneys(v, acc)
    elif isinstance(node, list):
        for x in node:
            _moneys(x, acc)
    return acc


U = lambda n: f"{VN}/StructureDefinition/{n}"  # noqa: E731
ROLE_URL = U("vn-ext-cost-statement-attestation-role")


def _roles(r):
    out = []
    for a in r.get("attester", []):
        for e in _ext(a, ROLE_URL):
            out.append((_subval(e, "role", "valueCode"), a, _subval(e, "attestedAt", "valueDateTime")))
    return out


def _line_ext(li):
    e = _ext(li, U("vn-ext-cost-statement-line"))
    return e[0] if e else None


def _base(li):
    return [p for p in li.get("priceComponent", []) if p.get("type") == "base"]


def _segment_seqs(r):
    return [_subval(e, "sequence", "valuePositiveInt") for e in _ext(r, U("vn-ext-cost-statement-segment"))]


def _allocation_sum(li):
    a = _ext(li, U("vn-ext-line-payment-allocation"))
    if not a:
        return None
    return sum((e.get("valueMoney") or {}).get("value", 0) for e in a[0].get("extension", []))


INVARIANTS = {
    # Patient / Practitioner / Organization / Location / Specimen / Document
    "vn-patient-identifier-minimum": lambda v, r: any(_has_type(i, c) and i.get("value") for i in r.get("identifier", []) for c in ("CCCD", "HC", "GKS")) or (any(_has_type(i, "MRN") and i.get("value") for i in r.get("identifier", [])) and bool(_ext(r, U("vn-ext-force-majeure-reason")))),
    "vn-patient-mrn-issuer-scoped": lambda v, r: all("assigner" in i for i in _ids(r, SID("mrn"))),
    "vn-patient-gks-issuer-scoped": lambda v, r: all("assigner" in i for i in _ids(r, SID("gks"))),
    "vn-cccd-format": lambda v, r: not isinstance(v, dict) or not v.get("value") or bool(re.fullmatch(r"[0-9]{12}", v["value"])),
    "vn-bhxh-format": lambda v, r: not isinstance(v, dict) or not v.get("value") or bool(re.fullmatch(r"[0-9]{10}", v["value"])),
    "vn-bhyt-format": lambda v, r: not isinstance(v, str) or bool(re.fullmatch(r"[0-9]{10}|[A-Z]{2}[0-9]{13}", v)),
    "vn-encounter-visit-code-issuer-scoped": lambda v, r: all("assigner" in i for i in _ids(r, SID("ma-luot-kcb"))),
    "vn-encounter-telehealth-two-axes": lambda v, r: (r.get("class", {}).get("code") == "VR") == any(c.get("system") == f"{VN}/CodeSystem/vn-encounter-type-cs" and c.get("code") == "14" for t in r.get("type", []) for c in t.get("coding", [])),
    "vn-organization-local-id-issuer-scoped": lambda v, r: all(i.get("assigner", {}).get("reference") for i in _ids(r, SID("organization-local-id"))),
    "vn-org-ma-gtin-13-digit": lambda v, r: not isinstance(v, str) or bool(re.fullmatch(r"[0-9]{13}", v)),
    "vn-org-dept-partof-required": lambda v, r: bool(r.get("partOf")) and r["partOf"].get("reference") != f"Organization/{r.get('id')}",
    "vn-location-local-id-issuer-scoped": lambda v, r: all(i.get("assigner", {}).get("reference") for i in _ids(r, SID("location-local-id"))),
    "vn-specimen-local-id-issuer-scoped": lambda v, r: all("assigner" in i for i in _ids(r, SID("specimen-local-id"))),
    "vn-document-number-issuer-scoped": lambda v, r: all("assigner" in i for i in _ids(r, SID("document-number"))),
    "vn-paper-conversion-official-doc-signature": lambda v, r: not (_subval(v, "officialDocument", "valueBoolean") is True) or bool(_sub(v, "signatureEvidence")),
    "vn-practitioner-regulated-title-license": lambda v, r: not any(c.get("system") == f"{VN}/CodeSystem/vn-regulated-practitioner-title-cs" for q in r.get("qualification", []) for c in q.get("code", {}).get("coding", []))
    or any(any(_subval(e, "status", "valueCoding") and _subval(e, "status", "valueCoding").get("code") in ("active", "renewal-pending-continued", "partially-suspended") for e in _ext(i, U("vn-ext-practice-license-status"))) for i in r.get("identifier", []) if i.get("system") in (SID("gphn"), SID("cchn")) and i.get("value")),
    "vn-role-exemption-bounded": lambda v, r: not _ext(r, U("vn-ext-practice-license-exemption-role")) or (bool(r.get("organization")) and bool(r.get("period", {}).get("start")) and bool(r.get("period", {}).get("end"))),
    "vn-role-exemption-trainee-supervised": lambda v, r: all(not (_subval(e, "basis", "valueCoding") or {}).get("code") == "trainee-under-supervision" or _sub(e, "supervisor") for e in _ext(r, U("vn-ext-practice-license-exemption-role"))),
    # Composition / Bundle
    "vn-composition-final-requires-attester": lambda v, r: r.get("status") not in ("final", "amended") or bool(r.get("attester")),
    "vn-bundle-document-first": lambda v, r: r.get("type") != "document" or (bool(r.get("entry")) and r["entry"][0].get("resource", {}).get("resourceType") == "Composition"),
    # Coverage
    "vn-coverage-subscriberid-consistency": lambda v, r: not r.get("subscriberId") or not _ids(r, SID("bhyt")) or r["subscriberId"] == _ids(r, SID("bhyt"))[0].get("value"),
    "vn-coverage-benefit-stage-level-range": lambda v, r: 0 <= (_subval(v, "benefitLevel", "valueDecimal") or 0) <= 100,
    # Claim
    "vn-claim-ma-lk-issuer-scoped": lambda v, r: all("assigner" in i for i in _ids(r, SID("ma-lk"))),
    "vn-claim-single-focal-insurance": lambda v, r: sum(1 for i in r.get("insurance", []) if i.get("focal")) == 1,
    "vn-claim-diagnosis-has-icd10": lambda v, r: all(any(c.get("system") == SYS["icd10vn"] for c in d.get("diagnosisCodeableConcept", {}).get("coding", [])) for d in r.get("diagnosis", [])),
    "vn-claim-procedure-has-icd9cm": lambda v, r: all(any(c.get("system") == f"{VN}/CodeSystem/vn-icd9-cm-cs" for c in p.get("procedureCodeableConcept", {}).get("coding", [])) for p in r.get("procedure", [])),
    "vn-claim-post-service-has-item": lambda v, r: r.get("use") != "claim" or bool(r.get("item")),
    "vn-line-payment-allocation-vnd": lambda v, r: all((e.get("valueMoney") or {}).get("currency") == "VND" for e in v.get("extension", []) if "valueMoney" in e),
    "vn-line-payment-allocation-nonnegative": lambda v, r: all((e.get("valueMoney") or {}).get("value", 0) >= 0 for e in v.get("extension", []) if "valueMoney" in e),
    "vn-other-fund-breakdown-money-vnd-value": lambda v, r: all("value" in (e.get("valueMoney") or {}) and e["valueMoney"].get("currency") == "VND" for e in v.get("extension", []) if "valueMoney" in e),
    "vn-insurance-cost-ratio-and-lexical": lambda v, r: all(0 <= (_subval(v, k, "valueDecimal") if _subval(v, k, "valueDecimal") is not None else 0) <= 100 for k in ("paymentRatio", "servicePaymentRatio", "benefitLevel"))
    and (not _sub(v, "paymentRatioSourceLexical") or bool(_sub(v, "paymentRatio"))) and (not _sub(v, "paymentCeilingPeriod") or bool(_sub(v, "paymentCeiling"))),
    # Account
    "vn-account-period-covers-encounters": lambda v, r: not r.get("servicePeriod") or bool(r["servicePeriod"].get("start")),
    "vn-account-coverage-priority-required": lambda v, r: all("priority" in c for c in r.get("coverage", [])),
    "vn-account-coverage-priority-distinct": lambda v, r: len({c.get("priority") for c in r.get("coverage", [])}) == len(r.get("coverage", [])),
    # Cost statement composition
    "vn-cost-statement-composition-entry-required": lambda v, r: all(bool(s.get("entry")) for s in r.get("section", [])),
    "vn-cost-statement-attestation-role-mode": lambda v, r: all((a.get("mode") == "professional") if role in ("preparer", "billing-confirmation") else (a.get("mode") == "personal") if role == "patient-confirmation" else True for role, a, _ in _roles(r)),
    "vn-cost-statement-attestation-role-unique": lambda v, r: all(sum(1 for role, _, _ in _roles(r) if role == k) <= 1 for k in ("preparer", "billing-confirmation", "patient-confirmation")),
    "vn-cost-statement-composition-three-signatures": lambda v, r: r.get("status") != "final" or all(sum(1 for role, _, _ in _roles(r) if role == k) == 1 for k in ("preparer", "billing-confirmation", "patient-confirmation")),
    "vn-cost-statement-attestation-time-consistent": lambda v, r: all(len({x for x in [a.get("time"), at] if x}) <= 1 for _, a, at in _roles(r) if a.get("time")),
    # Cost statement invoice
    "vn-cost-statement-segment-sequence-unique": lambda v, r: len(_segment_seqs(r)) == len(set(_segment_seqs(r))),
    "vn-cost-statement-uninsured-no-benefit": lambda v, r: bool(_sub(v, "coverage")) or not _sub(v, "benefitLevel"),
    "vn-cost-statement-segment-benefit-range": lambda v, r: all(0 <= s.get("valueDecimal", 0) <= 100 for s in _sub(v, "benefitLevel")),
    "vn-cost-statement-line-segment-exists": lambda v, r: all(_subval(_line_ext(li), "segmentSequence", "valuePositiveInt") in _segment_seqs(r) for li in r.get("lineItem", []) if _line_ext(li)),
    "vn-cost-statement-line-unit-price-required": lambda v, r: all(bool(_sub(_line_ext(li), "unit")) and bool(_sub(_line_ext(li), "hospitalUnitPrice")) for li in r.get("lineItem", []) if "chargeItemReference" not in li),
    "vn-cost-statement-money-vnd": lambda v, r: all(m.get("currency") == "VND" for m in _moneys(r)),
    "vn-cost-statement-line-quantity-amount-required": lambda v, r: all(any("factor" in p and "value" in (p.get("amount") or {}) for p in _base(li)) for li in r.get("lineItem", [])),
    "vn-cost-statement-total-sum": lambda v, r: "value" not in (r.get("totalNet") or {}) or abs(r["totalNet"]["value"] - sum((p.get("amount") or {}).get("value", 0) for li in r.get("lineItem", []) for p in _base(li))) < 0.5,
    "vn-cost-statement-line-allocation-sum": lambda v, r: _allocation_sum(v) is None or any(abs((p.get("amount") or {}).get("value", 0) - _allocation_sum(v)) < 0.5 for p in _base(v)),
    "vn-cost-statement-issued-allocation-complete": lambda v, r: r.get("status") not in ("issued", "balanced") or all(bool(_ext(li, U("vn-ext-line-payment-allocation"))) for li in r.get("lineItem", [])),
    "vn-cost-statement-related-service-exists": lambda v, r: all(_subval(_line_ext(li), "relatedServiceSequence", "valuePositiveInt") in [x.get("sequence") for x in r.get("lineItem", [])] for li in r.get("lineItem", []) if _line_ext(li) and _sub(_line_ext(li), "relatedServiceSequence")),
    "vn-cost-statement-package-component-category": lambda v, r: all(not _sub(_line_ext(li), "packageComponent") or any(c.get("code") == "10" for c in (_subval(_line_ext(li), "category", "valueCodeableConcept") or {}).get("coding", [])) for li in r.get("lineItem", []) if _line_ext(li)),
    "vn-cost-statement-transport-line-category": lambda v, r: all(any(c.get("code") == "patient-transport" for cc in [_subval(e, "lineRole", "valueCodeableConcept") or {} for e in _ext(li, U("vn-ext-cost-calculation-context"))] for c in cc.get("coding", []))
                                                                == any(c.get("code") == "11" for c in (_subval(_line_ext(li), "category", "valueCodeableConcept") or {}).get("coding", [])) for li in r.get("lineItem", [])),
    "vn-cost-statement-uninsured-line-defaults": lambda v, r: True,  # chỉ áp dụng cho phân đoạn không thẻ (không có trong dữ liệu BHYT); giữ True, quality_check kiểm tra riêng
    "vn-cost-calculation-exemption-needs-cap": lambda v, r: True,
    "vn-cost-calculation-stent-ordinal": lambda v, r: True,
    "vn-cost-calculation-transport-basis-coherent": lambda v, r: True,
    # Cost statement document bundle
    "vn-cost-statement-document-ma-lk": lambda v, r: all(i.get("value") == (r.get("identifier") or {}).get("value") for e in r.get("entry", []) if e.get("resource", {}).get("resourceType") in ("Composition", "Invoice", "Account")
                                                       for i in as_list(e["resource"].get("identifier")) if i.get("system") == SID("ma-lk")),
    "vn-cost-statement-document-same-subject": lambda v, r: (lambda comps, invs: bool(comps) and bool(invs) and all(c in invs for c in comps))(
        [e["resource"].get("subject", {}).get("reference") for e in r.get("entry", []) if e.get("resource", {}).get("resourceType") == "Composition"],
        [e["resource"].get("subject", {}).get("reference") for e in r.get("entry", []) if e.get("resource", {}).get("resourceType") == "Invoice"]),
    "vn-cost-statement-document-signature-complete": lambda v, r: not r.get("signature") or all(k in r["signature"] for k in ("type", "when", "who")),
    # Observation
    "vn-vital-value-or-dar": lambda v, r: _has_value(v) != bool(v.get("dataAbsentReason", {}).get("coding")),
    "vn-bp-component-value-or-dar": lambda v, r: _has_value(v) != bool(v.get("dataAbsentReason", {}).get("coding")),
    "vn-lab-final-result-complete": lambda v, r: r.get("status") not in ("final", "amended", "corrected") or bool(r.get("hasMember")) or (bool(r.get("component")) and all(_has_value(c) != bool(c.get("dataAbsentReason", {}).get("coding")) for c in r["component"])) or (_has_value(r) != bool(r.get("dataAbsentReason", {}).get("coding"))),
    "vn-lab-observation-ucum": lambda v, r: all(q.get("system") == SYS["ucum"] and q.get("code") for q in [x.get("valueQuantity") for x in [r] + r.get("component", [])] if q),
    "vn-lab-observation-code-system": lambda v, r: any(c.get("system") in (SYS["loinc"], SYS["cls"]) and c.get("code") for c in v.get("coding", [])),
    # Medication / CarePlan
    "vn-medadmin-completed-inpatient-context": lambda v, r: r.get("status") != "completed" or not any(c.get("code") == "inpatient" for c in r.get("category", {}).get("coding", [])) or bool(r.get("context", {}).get("reference") or r.get("context", {}).get("identifier")),
    "vn-medadmin-completed-inpatient-reference": lambda v, r: r.get("status") != "completed" or not any(c.get("code") == "inpatient" for c in r.get("category", {}).get("coding", [])) or bool(r.get("medicationReference")),
    "vn-careplan-activity-actionable": lambda v, r: r.get("status") != "active" or all(bool(a.get("detail") or a.get("reference")) for a in r.get("activity", [])),
}


# ---------------------------------------------------------------- duyệt JSON: thuật ngữ, ngày giờ, tham chiếu, narrative
def walk_json(node, path, res_id, issues, refs):
    if isinstance(node, dict):
        if "system" in node and isinstance(node.get("code"), str) and "coding" not in node and node.get("system") != SYS["ucum"]:
            sys_, code = node["system"], node["code"]
            if sys_ in CODESYSTEMS:
                if CODESYSTEMS[sys_] is None:
                    EXTERNAL[sys_.rsplit("/", 1)[-1]] += 1
                elif code not in CODESYSTEMS[sys_]:
                    issues.append(Issue("warning" if sys_ in FRAGMENT else "error", res_id, path, f"mã '{code}' không có trong CodeSystem {sys_.rsplit('/', 1)[-1]}" + (" (fragment)" if sys_ in FRAGMENT else ""), "terminology"))
                else:
                    EXTERNAL["(đã kiểm) " + sys_.rsplit("/", 1)[-1]] += 1
            elif sys_ == SYS["loinc"]:
                if not V.loinc_check_digit(code):
                    issues.append(Issue("error", res_id, path, f"LOINC '{code}' sai chữ số kiểm tra (mã không thể tồn tại)", "loinc"))
                elif code in LOINC_VERIFIED:
                    v = LOINC_VERIFIED[code]
                    if v is None:
                        issues.append(Issue("error", res_id, path, f"LOINC '{code}' KHÔNG tồn tại (đã tra NLM)", "loinc"))
                    elif v.get("status") == "DEPRECATED":
                        issues.append(Issue("warning", res_id, path, f"LOINC '{code}' đã deprecated: {v.get('display')}", "loinc"))
                    else:
                        EXTERNAL["(đã kiểm) loinc"] += 1
                else:
                    issues.append(Issue("info", res_id, path, f"LOINC '{code}' chưa có trong bảng đã xác minh (assets/loinc_verified.json) — tra NLM rồi bổ sung", "loinc"))
            else:
                EXTERNAL[sys_] += 1
        if isinstance(node.get("reference"), str):
            refs.append((res_id, path, node["reference"]))
        for k, v in node.items():
            p = f"{path}.{k}"
            if k in DT_KEYS and isinstance(v, str):
                ok = TIME_RE.match(v) if k == "time" and "T" not in v and "-" not in v else DT_RE.match(v)
                if not ok:
                    issues.append(Issue("error", res_id, p, f"định dạng dateTime không hợp lệ: {v}", "datetime"))
            if k in DATE_KEYS and isinstance(v, str) and not DATE_ONLY_RE.match(v):
                issues.append(Issue("error", res_id, p, f"định dạng date không hợp lệ: {v}", "datetime"))
            if k == "timeOfDay" and isinstance(v, list):
                for t in v:
                    if not TIME_RE.match(t):
                        issues.append(Issue("error", res_id, p, f"time không hợp lệ: {t}", "datetime"))
            if k == "div" and isinstance(v, str) and not v.startswith('<div xmlns="http://www.w3.org/1999/xhtml">'):
                issues.append(Issue("error", res_id, p, "narrative div thiếu xmlns xhtml", "narrative"))
            if k == "valueQuantity" and isinstance(v, dict) and "value" in v and v.get("system") == SYS["ucum"] and not v.get("code"):
                issues.append(Issue("warning", res_id, p, "Quantity có system UCUM nhưng thiếu code", "ucum"))
            walk_json(v, p, res_id, issues, refs)
    elif isinstance(node, list):
        for i, x in enumerate(node):
            walk_json(x, f"{path}[{i}]", res_id, issues, refs)


def check_resource(r, issues, stats, per_profile, ids_known):
    rid = f"{r['resourceType']}/{r.get('id')}"
    if not ID_RE.match(str(r.get("id", ""))):
        issues.append(Issue("error", rid, "id", "id không hợp lệ (A-Za-z0-9-. tối đa 64)", "id"))
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
    refs = []
    walk_json(r, r["resourceType"], rid, issues, refs)
    if "text" not in r and r["resourceType"] != "Bundle":
        issues.append(Issue("warning", rid, "text", "thiếu narrative (khuyến nghị cho hồ sơ bệnh án)", "narrative"))
    return refs


def check_bundle(b, issues, stats, per_profile):
    bid = f"Bundle/{b['id']}"
    refs = check_resource(b, issues, stats, per_profile, set())
    urls = [e.get("fullUrl") for e in b.get("entry", [])]
    if len(urls) != len(set(urls)):
        issues.append(Issue("error", bid, "entry.fullUrl", "fullUrl trùng nhau", "bundle"))
    ids = {f"{e['resource']['resourceType']}/{e['resource']['id']}" for e in b.get("entry", [])}
    for i, e in enumerate(b.get("entry", [])):
        if b["type"] == "transaction" and not e.get("request"):
            issues.append(Issue("error", bid, f"entry[{i}]", "transaction entry thiếu request", "bundle"))
        if not e.get("fullUrl"):
            issues.append(Issue("error", bid, f"entry[{i}]", "thiếu fullUrl", "bundle"))
    for rid, path, ref in refs:
        if ref.startswith("#") or ref.startswith("urn:") or ref.startswith("http"):
            continue
        if ref not in ids:
            issues.append(Issue("error", f"{bid}", path, f"tham chiếu không giải quyết được trong bundle: {ref}", "reference"))
    if b["type"] == "document":
        if not b.get("entry") or b["entry"][0]["resource"]["resourceType"] != "Composition":
            issues.append(Issue("error", bid, "entry[0]", "document Bundle phải bắt đầu bằng Composition", "vn-bundle-document-first"))
        else:
            comp = b["entry"][0]["resource"]
            for s in comp.get("section", []):
                for en in s.get("entry", []):
                    if en["reference"] not in ids:
                        issues.append(Issue("error", f"Composition/{comp['id']}", f"section[{s.get('title')}].entry", f"entry không có trong bundle: {en['reference']}", "composition"))
        if not b.get("identifier") or not b.get("timestamp"):
            issues.append(Issue("warning", bid, "identifier/timestamp", "document Bundle nên có identifier và timestamp", "bundle"))


def validate(fhir_dir: Path, out_dir: Path | None, case: str = "") -> dict:
    issues: list[Issue] = []
    stats = Counter()
    per_profile = Counter()
    single = fhir_dir.is_file()
    sibling_ids: set[str] = set()
    if single:
        files = [fhir_dir]
        near = list(fhir_dir.parent.glob("*.json"))
        if fhir_dir.parent.name != "resources":
            near += list((fhir_dir.parent / "resources").glob("*.json"))
        for f in near:
            if f != fhir_dir:
                try:
                    o = V.read_json(f)
                    if isinstance(o, dict) and o.get("resourceType") and o.get("id"):
                        sibling_ids.add(f"{o['resourceType']}/{o['id']}")
                except Exception:
                    pass
    else:
        files = sorted(Path(p) for p in glob.glob(str(fhir_dir / "resources" / "*.json")))
    resources = [V.read_json(f) for f in files]
    bundles = []
    if fhir_dir.is_dir():
        bundles = [V.read_json(f) for f in sorted(glob.glob(str(fhir_dir / "bundle-*.json")))]
    for r in list(resources):
        if r.get("resourceType") == "Bundle":
            resources.remove(r)
            bundles.append(r)
    ids = {f"{r['resourceType']}/{r['id']}" for r in resources}
    for r in resources:
        rid = f"{r['resourceType']}/{r['id']}"
        refs = check_resource(r, issues, stats, per_profile, ids)
        for _, path, ref in refs:
            if ref.startswith("#") or ref.startswith("urn:") or ref.startswith("http"):
                continue
            if ref not in ids:
                if single and ref in sibling_ids:
                    continue  # chế độ một tệp: tham chiếu tới resource ở tệp bên cạnh
                issues.append(Issue("warning" if single else "error", rid, path, f"tham chiếu không tồn tại{' (ngoài tệp, chưa kiểm được)' if single else ''}: {ref}", "reference"))
    for b in bundles:
        check_bundle(b, issues, stats, per_profile)
    # loại trùng (cùng resource xuất hiện trong nhiều bundle)
    seen, uniq = set(), []
    for i in issues:
        k = (i.sev, i.res, i.path, i.msg)
        if k not in seen:
            seen.add(k)
            uniq.append(i)
    issues = uniq
    errors = [i for i in issues if i.sev == "error"]
    warns = [i for i in issues if i.sev == "warning"]
    infos = [i for i in issues if i.sev == "info"]
    by_key = Counter((i.sev, i.key) for i in issues)
    report = {"case": case, "resources": len(resources), "bundles": len(bundles), "profiles_checked": dict(sorted(per_profile.items())), "errors": len(errors), "warnings": len(warns), "infos": len(infos),
              "issues_by_key": {f"{s}:{k}": n for (s, k), n in sorted(by_key.items())}, "stats": dict(stats), "invariants_not_evaluated": sorted(NOT_EVAL),
              "external_terminologies": dict(EXTERNAL), "issues": [i.d() for i in issues]}
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        V.write_json(report, out_dir / "report.json")
        md = [f"# Kiểm tra VN Core 0.10.0 — {case}", "", f"- Resources: {len(resources)} (+{len(bundles)} bundle); **lỗi: {len(errors)}**; cảnh báo: {len(warns)}; info: {len(infos)}",
              "- Profiles đã kiểm: " + ", ".join(f"{k} ({v})" for k, v in sorted(per_profile.items())), f"- Thống kê: {dict(stats)}",
              f"- Invariant chưa cài (bỏ qua): {sorted(NOT_EVAL) or 'không'}", f"- Thuật ngữ ngoài / đã kiểm: {dict(EXTERNAL)}", "", "| Mức | Resource | Path | Thông điệp |", "|---|---|---|---|"]
        md += [f"| {i.sev} | {i.res} | {i.path} | {i.msg} |" for i in issues]
        (out_dir / "report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fhir_dir")
    ap.add_argument("--out")
    ap.add_argument("--vendor")
    ap.add_argument("--case", default="")
    ap.add_argument("--strict", action="store_true", help="mã thoát 1 cả khi chỉ có cảnh báo")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    load_ig(V.find_vendor(a.vendor))
    fd = Path(a.fhir_dir)
    out = Path(a.out) if a.out else (fd / "validation" if fd.is_dir() else None)
    rep = validate(fd, out, a.case or fd.name)
    print(f"[validate_fhir] {rep['resources']} resources, {rep['bundles']} bundles — lỗi: {rep['errors']}, cảnh báo: {rep['warnings']}, info: {rep['infos']}" + (f" → {out}/report.md" if out else ""))
    if not a.quiet:
        for i in rep["issues"]:
            if i["severity"] in ("error", "warning"):
                print(f"  {i['severity'][:4].upper()} {i['resource']} {i['path']}: {i['message']}")
    sys.exit(1 if rep["errors"] or (a.strict and rep["warnings"]) else 0)


if __name__ == "__main__":
    main()
