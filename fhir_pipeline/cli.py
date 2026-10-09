"""CLI: python -m fhir_pipeline run data/sample/HSBA-BN001736827.zip --config config/pipeline.yaml

Lệnh:
  run <zip> [--steps extract,normalize,fhir,validate] [--case ID]   chạy pipeline (mặc định đủ 4 bước)
  resume <case> --steps normalize,fhir,validate                     chạy lại từ output đã có (không cần zip)
  validate <fhir_dir>                                               chỉ kiểm tra một thư mục 03_fhir
  llm-check                                                         gọi thử model bằng một yêu cầu JSON nhỏ
Tuỳ chọn chung: --config <yaml|json>, --set llm.model=... (ghi đè cấu hình, lặp lại được), -v
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import load_config
from .pipeline import ALL_STEPS, run_case


def _overrides(items):
    out: dict = {}
    for it in items or []:
        k, _, v = it.partition("=")
        cur = out
        parts = k.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        if v.lower() in ("true", "false"):
            v = v.lower() == "true"
        elif v.lower() in ("null", "none"):
            v = None
        else:
            try:
                v = int(v)
            except ValueError:
                try:
                    v = float(v)
                except ValueError:
                    pass
        cur[parts[-1]] = v
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="fhir_pipeline", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", "-c", default=None, help="file cấu hình YAML/JSON")
    ap.add_argument("--set", action="append", default=[], help="ghi đè cấu hình, vd --set llm.model=gpt-4o-mini")
    ap.add_argument("-v", "--verbose", action="store_true")
    sp = ap.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("run"); r.add_argument("zip"); r.add_argument("--steps", default=",".join(ALL_STEPS)); r.add_argument("--case")
    s = sp.add_parser("resume"); s.add_argument("case"); s.add_argument("--steps", default="normalize,fhir,validate")
    v = sp.add_parser("validate"); v.add_argument("fhir_dir"); v.add_argument("--out", default=None)
    sp.add_parser("llm-check")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config(a.config, _overrides(a.set))

    def progress(seq, n, name, form):
        print(f"[{seq:02d}/{n}] {form:<34} {name}", flush=True)

    if a.cmd == "run":
        res = run_case(a.zip, cfg, case=a.case, steps=tuple(a.steps.split(",")), progress=progress)
    elif a.cmd == "resume":
        res = run_case(None, cfg, case=a.case, steps=tuple(a.steps.split(",")), progress=progress)
    elif a.cmd == "validate":
        from .pipeline import prepare
        from .validate import validate_dir
        prepare(cfg)
        rep = validate_dir(a.fhir_dir, a.out, cfg.path(cfg.paths.vendor))
        res = {"errors": rep["errors"], "warnings": rep["warnings"], "issues_by_key": rep["issues_by_key"], "issues": rep["issues"][:30]}
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 1 if rep["errors"] else 0
    else:  # llm-check
        from .llm import make_llm
        llm = make_llm(cfg.llm)
        out = llm.complete_json("Trả về JSON.", 'Trả về đúng {"ok": true, "model_hint": "<tên model bạn nghĩ mình là>"}', max_tokens=200, cache=False)
        print(json.dumps({"provider": llm.name, "model": llm.s.model, "response": out}, ensure_ascii=False))
        return 0
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    return 1 if res.get("validation", {}).get("errors") else 0


if __name__ == "__main__":
    sys.exit(main())
