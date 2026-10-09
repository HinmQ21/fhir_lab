#!/usr/bin/env python3
"""Span mode: mentions.json (LLM viết: line + nguyên văn + thuộc tính) -> dự đoán có vị trí ký tự + mã đã kiểm chứng.

Dùng cho bộ đánh giá trích xuất thực thể dạng span (observation / diagnosis / medication) trên văn bản bệnh án,
ví dụ định dạng airace (groundtruth/HSBA-BN-XXXX.json). Xem references/span-extraction.md.

  python -I span_export.py locate  <input.txt> <mentions.json> <pred.json> [--report r.json] [--offline] [--unverified keep|drop]
  python -I span_export.py view    <input.txt> [--from N --to M]     # in văn bản kèm số dòng (1-based, giống Read)
  python -I span_export.py score   <gt.json> <pred.json>             # chấm nhanh 1 tệp theo đúng công thức evaluate.py

Nguyên tắc (giống skill chính):
  * Vị trí do script tính, không để LLM đếm ký tự.  * Văn bản span = đúng chuỗi con trong input.
  * ICD-10: chỉ nhận mã đúng dạng; kiểm chứng với ICD-10 VN + WHO 2019; mã không kiểm chứng được -> giữ + cờ (hoặc bỏ).
  * RxNorm: LLM chỉ đưa TÊN hoạt chất; mã RxCUI do RxNav (cache) trả về; không thấy -> null.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vnlib  # noqa: E402

ASSERT_ORDER = ["isFamily", "isHistorical", "isNegation"]  # evaluate.py chỉ đọc phần tử ĐẦU -> thứ tự cố định


def canon_assert(a) -> list[str]:
    if not a:
        return []
    if isinstance(a, str):
        a = [a]
    m = {x.lower(): x for x in ASSERT_ORDER}
    got = {m.get(str(x).strip().lower()) for x in a} - {None}
    return [x for x in ASSERT_ORDER if x in got]


def read_text(p) -> str:
    # đọc giống cách bộ chấm/annotator: universal newline, utf-8
    return open(p, encoding="utf-8").read()


class Locator:
    def __init__(self, text: str):
        self.t = text
        self.lines = text.split("\n")
        self.starts, o = [], 0
        for ln in self.lines:
            self.starts.append(o)
            o += len(ln) + 1
        self.claimed: dict[str, list[tuple[int, int]]] = {}

    def _free(self, field, s, e, strict=True):
        # strict: không chồng lấn span đã nhận; lỏng: chỉ cấm trùng y hệt (GT có span lồng nhau, vd 'đau đầu' trong 'đau đầu âm ỉ')
        if strict:
            return all(e <= a or s >= b for a, b in self.claimed.get(field, []))
        return (s, e) not in self.claimed.get(field, [])

    def _whole(self, a, b):
        t = self.t
        left_ok = a == 0 or not (t[a - 1].isalnum() and t[a].isalnum())
        right_ok = b >= len(t) or not (t[b - 1].isalnum() and t[b].isalnum())
        return left_ok and right_ok

    def _seg(self, l0, l1):
        l0 = max(0, l0); l1 = min(len(self.lines) - 1, l1)
        s = self.starts[l0]; e = self.starts[l1] + len(self.lines[l1])
        return s, e

    def _find_all(self, needle, s, e, ci=False, ws=False):
        hay = self.t[s:e]
        if ws:
            pat = r"\s+".join(re.escape(tok) for tok in needle.split())
            if not pat:
                return []
            return [(s + m.start(), s + m.end()) for m in re.finditer(pat, hay, re.I if ci else 0)]
        if ci:
            hay, needle = hay.lower(), needle.lower()
        out, i = [], hay.find(needle)
        while needle and i >= 0:
            out.append((s + i, s + i + len(needle)))
            i = hay.find(needle, i + 1)
        return out

    def locate(self, field: str, text: str, line: int | None):
        text = vnlib.nfc((text or "").strip())
        if not text:
            return None, "empty"
        L = (line - 1) if isinstance(line, int) and line >= 1 else None
        windows = []
        if L is not None and L < len(self.lines):
            windows += [(L, L, "line"), (L, L + 1, "line+1"), (L - 2, L + 2, "near"), (L - 15, L + 15, "far")]
        windows.append((0, len(self.lines) - 1, "doc"))
        # lượt 1: chỉ nhận khớp trọn từ (tránh 'ho' trong 'Khoảng'); lượt 2: cho phép khớp giữa từ
        for whole in (True, False):
            for l0, l1, wname in windows:
                s, e = self._seg(l0, l1)
                for ci, ws, how in ((False, False, "exact"), (True, False, "casefold"), (True, True, "ws")):
                    occ = [o for o in self._find_all(text, s, e, ci, ws) if not whole or self._whole(*o)]
                    for strict in (True, False):
                        for a, b in occ:
                            if self._free(field, a, b, strict):
                                self.claimed.setdefault(field, []).append((a, b))
                                return (a, b), f"{how}@{wname}" + ("" if strict else "+nested") + ("" if whole else "+subword")
        # mờ: cửa sổ trên dòng chỉ định, độ giống >= 0.85
        if L is not None and L < len(self.lines):
            s, e = self._seg(L - 1, L + 1)
            seg = self.t[s:e]
            n = len(text)
            best = (0.0, None)
            for i in range(0, max(1, len(seg) - n // 2)):
                for k in (n - 2, n - 1, n, n + 1, n + 2):
                    if k <= 0 or i + k > len(seg):
                        continue
                    r = difflib.SequenceMatcher(None, text.lower(), seg[i:i + k].lower()).ratio()
                    if r > best[0]:
                        best = (r, (s + i, s + i + k))
            if best[0] >= 0.85 and self._free(field, *best[1]):
                self.claimed.setdefault(field, []).append(best[1])
                return best[1], f"fuzzy{best[0]:.2f}"
        return None, "not-found"


def icd_resolve(code, text, policy, report):
    c, status = vnlib.icd10_verify(code)
    if status in ("vn", "who2019"):
        return c, status
    if status == "parent-only" or (c and status == "invalid"):
        report["icd_unverified"].append({"text": text, "code": c, "status": status})
        return (c if policy == "keep" else None), f"unverified-{status}"
    # không có mã hợp lệ: thử tra từ điển theo tên tiếng Việt (chỉ nhận khớp rất gần)
    cands = vnlib.icd10_search(text, top=1) if text else []
    if cands and cands[0]["ratio"] >= 0.9:
        return vnlib.icd10_dotted(cands[0]["code"]), "lookup"
    return None, "none"


def locate_cmd(a):
    t = read_text(a.input)
    m = json.loads(Path(a.mentions).read_text(encoding="utf-8"))
    if "data" in m and isinstance(m["data"], dict):
        m = m["data"]
    loc = Locator(t)
    rep = {"input": str(a.input), "counts": {}, "not_found": [], "how": {}, "icd": {}, "icd_unverified": [],
           "rx": {}, "rx_unresolved": [], "dupes": 0}
    out = {"observation": [], "diagnosis": [], "medication": []}

    def place(field, item, key):
        span, how = loc.locate(field, item.get(key), item.get("line"))
        rep["how"][how.split("@")[0]] = rep["how"].get(how.split("@")[0], 0) + 1
        if not span:
            rep["not_found"].append({"field": field, "line": item.get("line"), "text": item.get(key)})
        return span

    for it in m.get("observation", []):
        sp = place("observation", it, "text")
        if not sp:
            continue
        typ = (it.get("type") or "").strip().lower()
        out["observation"].append({"type": typ if typ in ("sign", "symptom") else "sign", "text": t[sp[0]:sp[1]],
                                   "position": list(sp), "assertion": canon_assert(it.get("assertion"))})
    for it in m.get("diagnosis", []):
        sp = place("diagnosis", it, "text")
        if not sp:
            continue
        txt = t[sp[0]:sp[1]]
        code, st = icd_resolve(it.get("icd10_code"), txt, a.unverified, rep)
        rep["icd"][st] = rep["icd"].get(st, 0) + 1
        typ = it.get("type")
        typ = typ.upper() if isinstance(typ, str) and typ.upper() in ("AD", "DD") else None
        rank = str(it.get("rank")) if str(it.get("rank")) in ("1", "2") else None
        info = (vnlib.icd10_lookup(code) or vnlib._icd_who().get(code or "") or {}) if code else {}
        out["diagnosis"].append({"icd10_code": code, "assertion": canon_assert(it.get("assertion")), "type": typ, "rank": rank,
                                 "name_en": it.get("name_en") or info.get("en"), "text": txt, "position": list(sp)})
    for it in m.get("medication", []):
        sp = place("medication", it, "name")
        if not sp:
            continue
        rx, ing = None, (it.get("ingredient") or "").strip()
        if ing:
            r = vnlib.rxnorm_ingredient(ing, online=not a.offline)
            if r and r.get("rxcui"):
                rx = str(r["rxcui"])
                rep["rx"]["resolved"] = rep["rx"].get("resolved", 0) + 1
            else:
                rep["rx_unresolved"].append({"name": it.get("name"), "ingredient": ing, "why": (r or {}).get("error", "not-found")})
        else:
            rep["rx"]["no-ingredient"] = rep["rx"].get("no-ingredient", 0) + 1
        out["medication"].append({"name": t[sp[0]:sp[1]], "position": list(sp), "dosage": it.get("dosage"),
                                  "rxnorm_code": rx, "assertion": canon_assert(it.get("assertion")), "type": it.get("type") or "điều trị"})
    for k in out:
        out[k].sort(key=lambda x: x["position"][0])
        rep["counts"][k] = len(out[k])
    vnlib.write_json(out, a.pred)
    if a.report:
        vnlib.write_json(rep, a.report)
    nf = len(rep["not_found"])
    print(f"{Path(a.input).name}: obs {rep['counts']['observation']} | diag {rep['counts']['diagnosis']} | med {rep['counts']['medication']}"
          f" | not-found {nf} | icd {rep['icd']} | rx-unresolved {len(rep['rx_unresolved'])} | how {rep['how']}")
    for x in rep["not_found"][:40]:
        print(f"  NOT FOUND [{x['field']}] line {x['line']}: {x['text']!r}")
    for x in rep["icd_unverified"][:40]:
        print(f"  ICD unverified ({x['status']}): {x['code']} <- {x['text']!r}")
    for x in rep["rx_unresolved"][:40]:
        print(f"  RX unresolved: {x['ingredient']!r} ({x['name']!r}) {x['why']}")
    return 0


def view_cmd(a):
    lines = read_text(a.input).split("\n")
    lo = max(1, a.from_ or 1); hi = min(len(lines), a.to or len(lines))
    for i in range(lo, hi + 1):
        print(f"{i:5d}\t{lines[i - 1]}")
    return 0


# ---- chấm nhanh 1 tệp, sao chép công thức của evaluate.py (airace) để agent tự kiểm khi có GT dev
def _sim(x, y):
    a = str(x or "").strip().lower(); b = str(y or "").strip().lower()
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _first(a):
    if isinstance(a, list):
        a = a[0] if a else None
    return None if not a or str(a).strip().lower() in ("none", "null", "[]") else str(a).strip()


def _eq(g, p, up=True):
    g = str(g or "").strip(); p = str(p or "").strip()
    if up:
        g, p = g.upper(), p.upper()
    g = "" if g in ("NONE", "None") else g; p = "" if p in ("NONE", "None") else p
    return 1.0 if g == p else 0.0


def _attrs(field, g, p):
    if field == "observation":
        return 0.4 * _sim(g.get("text"), p.get("text")) + 0.3 * (1.0 if (g.get("type") or "").lower() == (p.get("type") or "").lower() else 0) + 0.3 * (_first(g.get("assertion")) == _first(p.get("assertion")))
    if field == "diagnosis":
        return (0.4 * _eq(g.get("icd10_code"), p.get("icd10_code")) + 0.2 * _sim(g.get("text"), p.get("text")) + 0.2 * _eq(g.get("rank"), p.get("rank"))
                + 0.1 * (_first(g.get("assertion")) == _first(p.get("assertion"))) + 0.1 * _eq(g.get("type"), p.get("type")))
    return 0.4 * _eq(g.get("rxnorm_code"), p.get("rxnorm_code")) + 0.3 * _sim(g.get("name"), p.get("name")) + 0.3 * _sim(g.get("dosage"), p.get("dosage"))


def score_field(field, G, P):
    if not G and not P:
        return {"score": 1.0, "f1": 1.0, "attr": 1.0, "fp": [], "fn": [], "tp": 0}
    pos = lambda x: x.get("position") or x.get("position_name")
    cand = []
    for i, g in enumerate(G):
        for j, p in enumerate(P):
            gp, pp = pos(g), pos(p)
            if gp and pp:
                inter = max(0, min(gp[1], pp[1]) - max(gp[0], pp[0])); uni = max(gp[1], pp[1]) - min(gp[0], pp[0])
                if uni > 0 and inter / uni >= 0.5:
                    cand.append((inter / uni, i, j))
    cand.sort(reverse=True)
    ug, up, pairs = set(), set(), []
    for iou, i, j in cand:
        if i not in ug and j not in up:
            ug.add(i); up.add(j); pairs.append((i, j))
    tp = len(pairs); pr = tp / len(P) if P else 0; rc = tp / len(G) if G else 0
    f1 = 2 * pr * rc / (pr + rc) if pr + rc else 0
    attr = sum(_attrs(field, G[i], P[j]) for i, j in pairs) / tp if tp else 0
    return {"score": 0.6 * f1 + 0.4 * attr, "f1": f1, "p": pr, "r": rc, "attr": attr, "tp": tp,
            "fn": [G[i] for i in range(len(G)) if i not in ug], "fp": [P[j] for j in range(len(P)) if j not in up],
            "pairs": [(G[i], P[j]) for i, j in pairs]}


def score_cmd(a):
    G = json.loads(Path(a.gt).read_text(encoding="utf-8")); P = json.loads(Path(a.pred).read_text(encoding="utf-8"))
    P = P.get("data", P)
    W = {"diagnosis": 0.4, "medication": 0.3, "observation": 0.3}
    tot, wsum = 0.0, 0.0
    for f in ("observation", "diagnosis", "medication"):
        r = score_field(f, G.get(f) or [], P.get(f) or [])
        w = W[f] if (G.get(f) or []) else 0.0
        tot += w * r["score"]; wsum += w
        print(f"{f:12} score {r['score']:.3f} F1 {r['f1']:.3f} attr {r['attr']:.3f} tp {r['tp']} fp {len(r['fp'])} fn {len(r['fn'])}")
        if a.verbose:
            for x in r["fn"][:a.verbose]:
                print("   FN", json.dumps({k: x.get(k) for k in ('text', 'name', 'position', 'type', 'rank', 'icd10_code', 'assertion')}, ensure_ascii=False))
            for x in r["fp"][:a.verbose]:
                print("   FP", json.dumps({k: x.get(k) for k in ('text', 'name', 'position', 'type', 'rank', 'icd10_code', 'assertion')}, ensure_ascii=False))
    print(f"FILE SCORE {tot / wsum if wsum else 0:.4f}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vendor")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("locate"); s.add_argument("input"); s.add_argument("mentions"); s.add_argument("pred")
    s.add_argument("--report"); s.add_argument("--offline", action="store_true")
    s.add_argument("--unverified", choices=["keep", "drop"], default="keep"); s.set_defaults(f=locate_cmd)
    s = sub.add_parser("view"); s.add_argument("input"); s.add_argument("--from", dest="from_", type=int); s.add_argument("--to", type=int); s.set_defaults(f=view_cmd)
    s = sub.add_parser("score"); s.add_argument("gt"); s.add_argument("pred"); s.add_argument("-v", "--verbose", type=int, default=0); s.set_defaults(f=score_cmd)
    a = ap.parse_args(argv)
    if a.vendor:
        vnlib._VENDOR = vnlib.find_vendor(a.vendor)
    return a.f(a)


if __name__ == "__main__":
    raise SystemExit(main())
