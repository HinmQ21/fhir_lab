"""CLI:
  python -m fhir_agent providers                      liệt kê profile (không in khoá)
  python -m fhir_agent ping --provider gemini         gửi 1 câu hỏi tổng hợp (không chứa dữ liệu bệnh nhân) để kiểm tra kết nối + JSON
  python -m fhir_agent run <zip|pdf-dir|txt-dir> --provider glm [--gt gt/<case>/fhir] [--allow-cloud] [--out DIR] [--no-cache]
  python -m fhir_agent run <src> --replay runs/agent/<case>/<run> [--replay-live]   chạy lại các bước sau trích xuất, không tốn LLM
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .env import scrub
from .runner import PolicyError, load_config, profile_for, run_case

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(prog="fhir_agent", description="Harness agent: hồ sơ bệnh án (HSBA) -> clinical.json v2 -> FHIR VN Core 0.10.0")
    ap.add_argument("--config", default=str(ROOT / "config" / "agent.yaml"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("providers")
    p = sub.add_parser("ping")
    p.add_argument("--provider")
    r = sub.add_parser("run")
    r.add_argument("src")
    r.add_argument("--provider")
    r.add_argument("--gt", help="thư mục FHIR ground truth để chấm (compare.py)")
    r.add_argument("--out")
    r.add_argument("--case")
    r.add_argument("--allow-cloud", action="store_true", help="đồng ý gửi hồ sơ ĐÃ CHE PII tới provider cloud")
    r.add_argument("--no-cache", action="store_true")
    r.add_argument("--repair-rounds", type=int)
    r.add_argument("--replay", help="thư mục một lần chạy trước: dùng lại phản hồi trích xuất (01_llm), không gọi LLM cho bước trích xuất")
    r.add_argument("--replay-live", action="store_true", help="khi --replay: vẫn gọi model thật cho choose/repair")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)

    if a.cmd == "providers":
        for name, d in (cfg.get("providers") or {}).items():
            prof = profile_for(cfg, name)
            ok = "có khoá" if prof.api_key else ("không cần khoá" if prof.kind == "mock" else "THIẾU khoá")
            print(f"{name:14s} {prof.kind:13s} {prof.model or '-':28s} privacy={prof.privacy:12s} {ok}"
                  + (f"  fallback={','.join(prof.fallback_models)}" if prof.fallback_models else ""))
        return 0

    if a.cmd == "ping":
        from .llm import make_client
        prof = profile_for(cfg, a.provider)
        client = make_client(prof)
        t = time.time()
        try:
            out, meta = client.json("ping", "Trả lời bằng JSON.", 'Chuyển "Huyết áp 120/80 mmHg" thành {"systolic": số, "diastolic": số, "unit": chuỗi}.', max_tokens=2000, cache=False)
        except Exception as e:  # noqa: BLE001
            print(f"{prof.name}: LỖI {scrub(str(e))}")
            return 1
        print(f"{prof.name} ({meta.get('model')}): {json.dumps(out, ensure_ascii=False)} — {time.time() - t:.1f}s, token {client.stats['prompt_tokens']}/{client.stats['completion_tokens']}")
        return 0

    try:
        run = run_case(a.src, cfg, a.provider, gt=a.gt, out=a.out, case=a.case, allow_cloud=a.allow_cloud, use_cache=not a.no_cache, repair_rounds=a.repair_rounds,
                       replay=a.replay, replay_live=a.replay_live)
    except PolicyError as e:
        print(f"Từ chối: {e}")
        return 2
    print(json.dumps({k: run[k] for k in ("out", "quality", "validation", "score", "llm", "timings_s") if k in run}, ensure_ascii=False, indent=1))
    return 0
