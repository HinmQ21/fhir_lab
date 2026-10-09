#!/usr/bin/env python3
"""Span mode — học "profile" quy ước của một bộ GT (tập dev) rồi áp lên dự đoán.

Mỗi bộ dữ liệu có thói quen chú thích riêng (mã ICD ưa dùng, độ dài span, chỗ nào gán type/rank, thuốc nào để RxNorm null).
Thay vì nhồi các thói quen đó vào hướng dẫn cho LLM, ta học chúng từ GT của tập dev và áp bằng script — có thể đo, có thể bỏ.

  python -I span_profile.py learn  --gt <gt_dir> --input <input_dir> [--pred <dev_pred_dir>] [--exclude ids.txt|id,id] [--only ...] -o profile.json
  python -I span_profile.py apply  profile.json <pred_in_dir> <pred_out_dir> --input <input_dir> [--parts icd,rx,trim,drop,type,rank]

Thành phần (mỗi phần bật/tắt được bằng --parts):
  icd   : văn bản chẩn đoán (chuẩn hoá) -> mã GT dùng nhiều nhất (≥2 lần, ≥60 %) — chỉ thay mã khi văn bản khớp đúng.
  rx    : tên thuốc (chuẩn hoá) -> RxCUI/null đa số; thuốc phối hợp ('+') chưa gặp -> null.
  trim  : observation không có trong từ vựng GT -> cắt về tiền tố dài nhất có trong từ vựng (vd 'đau bụng thượng vị' -> 'đau bụng').
  drop  : bỏ mention mà trên dev hầu như luôn là FP (precision < 0.2, ≥3 lần) — cần --pred khi learn.
  type  : nhãn vị trí (chữ trước dấu ':' trên dòng, hoặc tiêu đề khối '[...]') -> type GT đa số (≥5 lần, ≥80 %).
  rank  : rank 1 chỉ cho mention có văn bản trùng chẩn đoán chính (ô 'Ra viện…Bệnh chính' nếu có, không thì ô vào viện); gia đình -> null.
Không dùng GT của tệp đang dự đoán: luôn --exclude tập test khi learn.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import span_export as SE  # noqa: E402
import vnlib  # noqa: E402


def nz(s: str | None) -> str:
    s = vnlib.nfc(s or "").lower().replace("\x7f", "")
    s = re.sub(r"\s+", " ", s).strip(" .,;:-–/")
    return s


def words(s: str) -> list[str]:
    return nz(s).split(" ")


def ids_from(arg) -> set[str]:
    if not arg:
        return set()
    p = Path(arg)
    raw = p.read_text(encoding="utf-8") if p.exists() else arg
    return {x.strip()[-4:] for x in re.split(r"[\s,]+", raw) if x.strip()}


def cases(gt_dir: Path, exclude: set[str], only: set[str]):
    for g in sorted(Path(gt_dir).glob("*.json")):
        k = g.stem[-4:]
        if k in exclude or (only and k not in only):
            continue
        yield g.stem, g


def pos(x):
    return x.get("position") or x.get("position_name")


# ---------------------------------------------------------------- ngữ cảnh vị trí
LABEL = re.compile(r"([^:\n]{2,60}):\s*(?:\(tổn thương\)\s*)?[^:\n]*$", re.I)


def ctx_key(t: str, s: int) -> str:
    ls = t.rfind("\n", 0, s) + 1
    pre = t[ls:s]
    if ":" in pre:
        lab = pre[:pre.rfind(":")]
        lab = re.sub(r"[\d\.\-\+\(\)\[\]/]", " ", lab.lower())
        lab = " ".join(lab.split()[-4:])
        return "lab:" + lab
    # đầu dòng: tìm tiêu đề khối [..] hoặc nhãn ở các dòng trước (tối đa 12 dòng)
    j = ls
    for _ in range(12):
        if j <= 0:
            break
        pj = t.rfind("\n", 0, j - 1) + 1
        line = t[pj:j - 1].strip()
        if re.fullmatch(r"\[[^\]]{2,40}\]", line):
            return "blk:" + line.lower()
        if re.search(r"(chẩn đoán|bệnh chính|bệnh kèm theo)[^:]*:\s*$", line.lower()):
            return "lab:" + " ".join(re.sub(r"[\d\.\-\+\(\)\[\]/:]", " ", line.lower()).split()[-4:])
        j = pj
    return "free"


MAIN_DD = re.compile(r"ra viện.*bệnh chính|bệnh chính.*ra viện", re.I)
MAIN_AD = re.compile(r"chẩn đoán vào viện|khi vào khoa|khoa khám bệnh", re.I)


# ---------------------------------------------------------------- learn
def learn(a):
    excl, only = ids_from(a.exclude), ids_from(a.only)
    icd = collections.defaultdict(collections.Counter)
    rx = collections.defaultdict(collections.Counter)
    vocab = collections.Counter()
    tkeys = collections.defaultdict(collections.Counter)
    n = 0
    for stem, g in cases(a.gt, excl, only):
        n += 1
        G = json.loads(g.read_text(encoding="utf-8"))
        t = (Path(a.input) / f"{stem}.txt").read_text(encoding="utf-8")
        for d in G.get("diagnosis") or []:
            icd[nz(d["text"])][(d.get("icd10_code") or "").strip().upper() or None] += 1
            tkeys[ctx_key(t, d["position"][0])][d.get("type")] += 1
        for m in G.get("medication") or []:
            rx[nz(m["name"])][m.get("rxnorm_code")] += 1
        for o in G.get("observation") or []:
            vocab[nz(o["text"])] += 1
    prof = {"_meta": {"n_cases": n, "excluded": sorted(excl)}, "icd": {}, "rx": {}, "vocab": {}, "type": {}, "drop": {}}
    for k, c in icd.items():
        code, m = c.most_common(1)[0]
        if sum(c.values()) >= 2 and m / sum(c.values()) >= 0.6:
            prof["icd"][k] = code
    for k, c in rx.items():
        code, m = c.most_common(1)[0]
        if m / sum(c.values()) >= 0.6:
            prof["rx"][k] = code
    prof["vocab"] = {k: v for k, v in vocab.items() if v >= 2}
    for k, c in tkeys.items():
        ty, m = c.most_common(1)[0]
        if sum(c.values()) >= 5 and m / sum(c.values()) >= 0.8:
            prof["type"][k] = ty
    if a.pred:  # danh sách bỏ: precision trên dev SAU khi trim
        stat = collections.defaultdict(lambda: [0, 0])
        for stem, g in cases(a.gt, excl, only):
            pf = Path(a.pred) / f"{stem}.json"
            if not pf.exists():
                continue
            G = json.loads(g.read_text(encoding="utf-8"))
            t = (Path(a.input) / f"{stem}.txt").read_text(encoding="utf-8")
            P = apply_one(prof, json.loads(pf.read_text(encoding="utf-8")), t, {"trim"})
            for f in ("observation", "diagnosis", "medication"):
                r = SE.score_field(f, G.get(f) or [], P.get(f) or [])
                for _, p in r.get("pairs", []):
                    stat[(f, nz(p.get("text") or p.get("name")))][0] += 1
                for p in r["fp"]:
                    stat[(f, nz(p.get("text") or p.get("name")))][1] += 1
        for (f, k), (tp, fp) in stat.items():
            if tp + fp >= 3 and tp / (tp + fp) < 0.2:
                prof["drop"].setdefault(f, []).append(k)
    vnlib.write_json(prof, a.out)
    print(f"profile <- {n} cases: icd {len(prof['icd'])}, rx {len(prof['rx'])}, vocab {len(prof['vocab'])}, "
          f"type keys {len(prof['type'])}, drop {sum(len(v) for v in prof['drop'].values())} -> {a.out}")
    return 0


# ---------------------------------------------------------------- apply
def trim_obs(prof, t, o):
    k = nz(o["text"])
    voc = prof["vocab"]
    if voc.get(k, 0) >= 2:
        return o
    a, b = o["position"]
    span = t[a:b]
    toks = list(re.finditer(r"\S+", span))
    best = None
    for i in range(len(toks) - 1, 0, -1):
        cand = span[:toks[i - 1].end()]
        if voc.get(nz(cand), 0) >= 3:
            best = cand
            break
    if not best:
        return o
    nb = a + len(best.rstrip(" ,.;:"))
    return dict(o, text=t[a:nb], position=[a, nb])


def apply_one(prof, P, t, parts):
    P = {k: list(P.get(k) or []) for k in ("observation", "diagnosis", "medication")}
    drop = {f: set(v) for f, v in prof.get("drop", {}).items()}
    if "trim" in parts:
        P["observation"] = [trim_obs(prof, t, o) for o in P["observation"]]
    if "drop" in parts:
        P["observation"] = [o for o in P["observation"] if nz(o["text"]) not in drop.get("observation", set())]
        P["diagnosis"] = [d for d in P["diagnosis"] if nz(d["text"]) not in drop.get("diagnosis", set())]
        P["medication"] = [m for m in P["medication"] if nz(m["name"]) not in drop.get("medication", set())]
    for d in P["diagnosis"]:
        k = nz(d["text"])
        if "icd" in parts and k in prof["icd"]:
            d["icd10_code"] = prof["icd"][k]
        if "type" in parts:
            ck = ctx_key(t, d["position"][0])
            if ck in prof["type"]:
                d["type"] = prof["type"][ck]
    if "rank" in parts:
        dg = sorted(P["diagnosis"], key=lambda x: x["position"][0])
        def lab(d):
            ls = t.rfind("\n", 0, d["position"][0]) + 1
            return t[ls:d["position"][0]]
        seed = [d for d in dg if MAIN_DD.search(lab(d))] or [d for d in dg if MAIN_AD.search(lab(d))]
        if seed:
            # ô chính có thể liệt kê nhiều bệnh: lấy mention agent đã đánh rank 1 trong ô, không có thì mention đầu
            first_line = [d for d in seed if t.rfind("\n", 0, d["position"][0]) == t.rfind("\n", 0, seed[0]["position"][0])]
            main = [d for d in first_line if str(d.get("rank")) == "1"] or first_line[:1]
            texts = {nz(d["text"]) for d in main}
            for d in dg:
                if "isFamily" in (d.get("assertion") or []):
                    d["rank"] = None
                else:
                    d["rank"] = "1" if nz(d["text"]) in texts else "2"
    if "rx" in parts:
        for m in P["medication"]:
            k = nz(m["name"])
            if k in prof["rx"]:
                m["rxnorm_code"] = prof["rx"][k]
            elif "+" in m["name"]:
                m["rxnorm_code"] = None
    return P


def apply(a):
    prof = json.loads(Path(a.profile).read_text(encoding="utf-8"))
    parts = set(a.parts.split(","))
    out = Path(a.out_dir); out.mkdir(parents=True, exist_ok=True)
    n = 0
    for pf in sorted(Path(a.in_dir).glob("HSBA-*.json")):
        if pf.name.count(".") != 1:
            continue
        t = (Path(a.input) / f"{pf.stem}.txt").read_text(encoding="utf-8")
        vnlib.write_json(apply_one(prof, json.loads(pf.read_text(encoding="utf-8")), t, parts), out / pf.name)
        n += 1
    print(f"applied [{a.parts}] to {n} files -> {out}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("learn"); s.add_argument("--gt", required=True); s.add_argument("--input", required=True)
    s.add_argument("--pred"); s.add_argument("--exclude"); s.add_argument("--only"); s.add_argument("-o", "--out", required=True)
    s.set_defaults(f=learn)
    s = sub.add_parser("apply"); s.add_argument("profile"); s.add_argument("in_dir"); s.add_argument("out_dir")
    s.add_argument("--input", required=True); s.add_argument("--parts", default="icd,rx,trim,drop,type,rank"); s.set_defaults(f=apply)
    a = ap.parse_args(argv)
    return a.f(a)


if __name__ == "__main__":
    raise SystemExit(main())
