"""Điều phối một ca: ingest -> extract (4 đợt) -> assemble -> coding -> cổng 1 (+sửa) -> build -> cổng 2 (+sửa) -> review (+compare).

Thư mục ra (mặc định runs/agent/<case>/<run_id>/):
  00_inventory/        manifest.json, inventory.md, 00_raw/*.txt
  01_llm/<task>.json   prompt (cục bộ, đầy đủ) + phản hồi đã parse của từng nhiệm vụ
  clinical.json        clinical v2 cuối cùng;  quality_report.{json,md}
  fhir/                resources/, bundle-*.json, build_report.json, validation/report.{json,md}
  review.md            cổng 3;  scorecard.{json,md} nếu có --gt;  run.json (số đo);  trace.jsonl (mỗi lời gọi LLM, không chứa nội dung)
"""
from __future__ import annotations

import copy
import json
import re
import threading
import time
from pathlib import Path

from . import __version__
from . import extract as EX
from . import gates as G
from . import ingest
from . import kb as KB
from .assemble import Assembler
from .coding import Coder, strip_internal
from .env import expand, load_dotenv
from .llm import Profile, make_client
from .privacy import PrivacyGuard
from .report import review_md


class PolicyError(RuntimeError):
    pass


def load_config(path: str | Path) -> dict:
    import yaml
    p = Path(path)
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    root = p.resolve().parent.parent
    cfg["_root"] = root
    env = cfg.get("env_file")
    if env:
        load_dotenv(root / env)
    return cfg


def profile_for(cfg: dict, name: str | None) -> Profile:
    name = name or cfg.get("default_provider")
    provs = cfg.get("providers") or {}
    if name not in provs:
        raise SystemExit(f"provider '{name}' không có trong config (có: {', '.join(provs)})")
    return Profile.from_dict(name, expand(provs[name]))


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S+07:00")


def run_case(src: str | Path, cfg: dict, provider: str | None = None, *, gt: str | Path | None = None, out: str | Path | None = None,
             case: str | None = None, allow_cloud: bool = False, use_cache: bool = True, repair_rounds: int | None = None,
             replay: str | Path | None = None, replay_live: bool = False, log=print) -> dict:
    root: Path = cfg["_root"]
    src = Path(src)
    prof = profile_for(cfg, provider)
    if prof.privacy == "block":
        raise PolicyError(f"profile {prof.name}: privacy=block — không được gửi hồ sơ tới endpoint này")
    if prof.privacy == "pseudonymize" and not allow_cloud and not (replay and not replay_live):
        raise PolicyError(f"profile {prof.name} là dịch vụ cloud (privacy=pseudonymize). Hồ sơ sẽ được che PII trước khi gửi; "
                          "xác nhận bằng --allow-cloud (PDPL 2023: dữ liệu sức khoẻ là dữ liệu cá nhân nhạy cảm).")
    paths = cfg.get("paths") or {}
    kb = KB.load(root / paths.get("skill", "vn-fhir/skills/vn-fhir-convert"), root / paths.get("vendor", "vendor"))
    case = case or re.sub(r"^HSBA-", "", src.stem)
    run_id = time.strftime("%Y%m%d-%H%M%S") + f"-{prof.name}"
    out = Path(out) if out else root / paths.get("runs", "runs/agent") / case / run_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "01_llm").mkdir(exist_ok=True)
    trace_f = (out / "trace.jsonl").open("a", encoding="utf-8")
    lock = threading.Lock()

    def trace(meta):
        with lock:
            trace_f.write(json.dumps(meta, ensure_ascii=False) + "\n")
            trace_f.flush()

    guard = PrivacyGuard() if prof.privacy == "pseudonymize" else None
    client = make_client(prof, cache_dir=(root / paths.get("cache", "runs/agent/_cache")) if use_cache else None, trace=trace, guard=guard)
    if replay:
        from .llm import ReplayClient
        client = ReplayClient(prof, Path(replay), live=client if replay_live else None, trace=trace)
        log(f"      phát lại phản hồi trích xuất từ {replay}" + (" (choose/repair gọi model thật)" if replay_live else " (không gọi LLM)"))
    q = cfg.get("quality") or {}
    rounds = q.get("repair_rounds", 2) if repair_rounds is None else repair_rounds
    max_items = q.get("repair_max_resources", 40)
    timings = {}
    T = time.time()

    # 1 ingest ------------------------------------------------------------------
    t0 = time.time()
    log(f"[1/7] kiểm kê {src.name}")
    manifest, texts, documents = ingest.run(kb, src, out / "00_inventory")
    log(f"      {len(documents)} tài liệu; có chữ: {sum(1 for t in texts.values() if t.strip())}; cần OCR: {manifest.get('needs_ocr') or 'không'}")
    if manifest.get("gid_font_decoded"):
        log(f"      giải mã font không ToUnicode: phiếu {manifest['gid_font_decoded']}")
    if guard is not None:
        for t in texts.values():
            guard.learn(t)
        for vals in (manifest.get("patient_hint_values") or {}).values():
            for v in vals:
                guard.learn(f"Họ tên: {v}") if not re.search(r"\d", v) else guard._add(v, "ID")
        log(f"      che PII cho {prof.name}: {guard.report()['tokens']}")
    timings["ingest"] = round(time.time() - t0, 1)

    fac_cfg = dict(cfg.get("facility") or {})
    meta = {"schema_version": "2.0", "vncore_version": "0.10.0", "case_id": case, "created": _now(),
            "created_by": f"fhir_agent {__version__} ({prof.name}: {prof.model})", "status": "extracted",
            "facility": {k: fac_cfg.get(k) for k in ("organization", "name", "base_url", "mrn_system", "staff_system", "order_system", "ma_cskcb") if fac_cfg.get(k)},
            "source_archive": {"file": str(src), "documents": len(documents)}}
    asm = Assembler(kb, meta, documents)

    # 2-3 extract -----------------------------------------------------------------
    tasks = EX.plan(documents, texts)
    t0 = time.time()

    def store(task, prompt, data, m):
        (out / "01_llm" / f"{task.name.replace(':', '_')}.json").write_text(
            json.dumps({"task": task.name, "docs": task.docs, "aux": task.aux, "meta": m, "prompt": prompt, "response": data}, ensure_ascii=False, indent=1), encoding="utf-8")

    failed = []
    for wave in sorted({t.wave for t in tasks}):
        ws = [t for t in tasks if t.wave == wave]
        log(f"[2/7] đợt {wave}: {', '.join(t.name for t in ws)}")
        res = EX.run_wave(client, kb.V, ws, asm.state["documents"], texts, asm.state, meta["facility"], store, log)
        for t in ws:
            if t.name in res:
                asm.merge(t, res[t.name])
            else:
                failed.append(t.name)
        asm.interim()
    timings["extract"] = round(time.time() - t0, 1)

    # 4-5 assemble + coding ----------------------------------------------------------
    t0 = time.time()
    log("[3/7] gộp + chuẩn hoá + mã hoá bằng tra cứu")
    clin = asm.finalize(_facility(meta["facility"], asm.state))
    coder = Coder(kb, client, log, texts)
    clin = coder.run(clin)
    if failed:
        clin["review_notes"].append(f"Nhiệm vụ trích xuất lỗi (thiếu dữ liệu từ các phiếu này): {', '.join(failed)}")
    timings["assemble_coding"] = round(time.time() - t0, 1)

    # 6 cổng 1 + sửa -----------------------------------------------------------------
    t0 = time.time()
    clean = strip_internal(clin)
    qrep = G.quality(kb, clean)
    log(f"[4/7] cổng 1 quality_check: lỗi {qrep['errors']}, cảnh báo {qrep['warnings']}")
    for r in range(rounds):
        groups = G.group_qc(clean, qrep)
        if not groups:
            break
        log(f"      vòng sửa {r + 1}: {len(groups)} resource lỗi")
        cand, n = G.repair(client, kb, clin, groups, texts, max_items, log, "qc")
        if n == 0:
            log("      không sửa được resource nào — dừng vòng sửa")
            break
        cand = _renormalize(kb, cand, meta["facility"], coder, clin, log)
        q2 = G.quality(kb, strip_internal(cand))
        cand, q2 = _revert_worse(kb, clin, cand, groups, G.group_qc(strip_internal(cand), q2), q2)
        log(f"      → lỗi {qrep['errors']} → {q2['errors']} (sửa {n})")
        if q2["errors"] < qrep["errors"]:
            clin, qrep = cand, q2
            clean = strip_internal(clin)
        else:
            break
    timings["gate1"] = round(time.time() - t0, 1)

    # 7 build + cổng 2 + sửa -----------------------------------------------------------
    t0 = time.time()
    fhir = out / "fhir"
    brep = vrep = None
    for r in range(rounds + 1):
        try:
            brep = G.build_safe(kb, clin, fhir, clin["review_notes"], prep=strip_internal)
            clean = strip_internal(clin)
        except Exception as e:  # noqa: BLE001
            log(f"[5/7] build_fhir lỗi: {e}")
            clin["review_notes"].append(f"build_fhir lỗi: {e}")
            brep = None
            break
        vrep = G.validate(kb, fhir, case)
        log(f"[5/7] build {brep['resources']} resource; cổng 2 validate_fhir: lỗi {vrep['errors']}, cảnh báo {vrep['warnings']}")
        groups = G.group_fhir(clean, vrep)
        if not groups or r == rounds:
            break
        log(f"      vòng sửa FHIR {r + 1}: {len(groups)} resource lỗi")
        cand, n = G.repair(client, kb, clin, groups, texts, max_items, log, "fhir")
        if n == 0:
            break
        cand = _renormalize(kb, cand, meta["facility"], coder, clin, log)
        q2 = G.quality(kb, strip_internal(cand))
        if q2["errors"] > qrep["errors"]:
            log("      bản sửa làm tăng lỗi cổng 1 — giữ bản cũ")
            break
        clin, qrep = cand, q2
    timings["build_gate2"] = round(time.time() - t0, 1)

    # 8 review -------------------------------------------------------------------------
    clean = strip_internal(clin)
    clean["meta"]["status"] = "extracted"
    kb.V.write_json(clean, out / "clinical.json")
    kb.V.write_json(qrep, out / "quality_report.json")
    kb.qc.write_md(qrep, out)
    score = None
    if gt and brep:
        score = kb.compare.compare(kb.compare.load(fhir), kb.compare.load(Path(gt)))
        kb.V.write_json(score, out / "scorecard.json")
        kb.compare.write_md(score, out, fhir.name, Path(gt).name)
        log(f"[6/7] so với GT: F1 {score['f1']} (P {score['precision']}, R {score['recall']})")
    timings["total"] = round(time.time() - T, 1)
    run = {"version": __version__, "case": case, "src": str(src), "provider": prof.name + (f" (replay {Path(replay).name})" if replay else ""), "kind": prof.kind, "model": prof.model, "out": str(out),
           "finished": _now(), "timings_s": timings, "tasks": [t.name for t in tasks], "failed_tasks": failed, "llm": dict(client.stats), "cost_usd": client.cost_usd(),
           "privacy": (f"{prof.privacy}: " + json.dumps(guard.report()["tokens"])) if guard is not None else f"{prof.privacy} (endpoint nội bộ, gửi nguyên văn)",
           "quality": {"errors": qrep["errors"], "warnings": qrep["warnings"]}, "fhir": brep and {"resources": brep["resources"], "by_type": brep["by_type"]},
           "validation": vrep and {"errors": vrep["errors"], "warnings": vrep["warnings"]}, "score": score and {k: score[k] for k in ("f1", "precision", "recall", "matched", "extra", "missing", "field_diffs")}}
    kb.V.write_json(run, out / "run.json")
    (out / "review.md").write_text(review_md(clean, qrep, vrep, brep, run, coder.decisions, score), encoding="utf-8")
    trace_f.close()
    log(f"[7/7] xong trong {timings['total']}s → {out}")
    return run


def _facility(fac: dict, state: dict) -> dict:
    """Bổ sung meta.facility khi cấu hình không ghi: suy từ tổ chức kind=facility được trích dẫn nhiều nhất."""
    fac = dict(fac)
    orgs = [o for o in state.get("organizations") or [] if o.get("kind") == "facility"]
    orgs.sort(key=lambda o: -len(o.get("sources") or []))
    if not fac.get("organization") and orgs:
        fac["organization"] = orgs[0]["id"]
        fac.setdefault("name", orgs[0].get("name"))
    slug = (fac.get("organization") or "org-unknown").removeprefix("org-")
    fac.setdefault("base_url", f"http://his.{slug}.vn/fhir")
    fac.setdefault("mrn_system", f"http://his.{slug}.vn/sid/mrn")
    fac.setdefault("staff_system", f"http://his.{slug}.vn/sid/staff-id")
    fac.setdefault("order_system", f"http://his.{slug}.vn/sid/y-lenh")
    state["meta"]["facility"].update(fac)
    return fac


def _renormalize(kb, clin, facility, coder, fallback=None, log=print):
    """Chuẩn hoá + mã hoá lại sau vòng sửa. Bản sửa làm hỏng cấu trúc (finalize/coder lỗi) -> bỏ, giữ bản trước."""
    try:
        a = Assembler.from_state(kb, clin)
        c = a.finalize(facility)
        return coder.run(c)
    except Exception as e:  # noqa: BLE001
        log(f"      ! bản sửa không chuẩn hoá được ({type(e).__name__}: {e}) — giữ bản trước")
        return copy.deepcopy(fallback) if fallback is not None else clin


def _revert_worse(kb, old, new, before, after, q2):
    """Resource nào sau khi sửa có NHIỀU lỗi hơn trước -> trả về bản cũ."""
    reverted = False
    for key, msgs in after.items():
        if len(msgs) > len(before.get(key, [])):
            block, id_ = key
            old_r = G.find(old, block, id_)
            if old_r is None:
                continue
            if block in G.SINGLE_BLOCKS:
                new[block] = copy.deepcopy(old_r)
            else:
                new[block] = [copy.deepcopy(old_r) if x.get("id") == id_ else x for x in new[block]]
            reverted = True
    if reverted:
        q2 = G.quality(kb, strip_internal(new))
    return new, q2
