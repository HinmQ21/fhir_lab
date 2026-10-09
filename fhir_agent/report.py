"""Cổng 3 — review.md theo template của skill (references/quality-loop.md): nguồn, quyết định mã, conflicts, unmapped,
review_notes, kết quả 2 cổng, cảnh báo PII, và số đo của lần chạy (provider, token, thời gian, chi phí, điểm so với GT)."""
from __future__ import annotations

from collections import Counter, defaultdict

from .assemble import LIST_BLOCKS, SINGLE_BLOCKS


def review_md(clin: dict, qrep: dict, vrep: dict | None, brep: dict | None, run: dict, decisions: list[dict], score: dict | None) -> str:
    case = clin["meta"].get("case_id")
    L = [f"# Review — {case} (fhir_agent, {run['provider']} / {run['model']})", "",
         f"Tạo bởi harness `fhir_agent` {run['version']} lúc {run['finished']}. Trạng thái: **{clin['meta'].get('status')}** — cần người duyệt trước khi dùng.", ""]
    # 0 tóm tắt
    L += ["## 0. Tóm tắt", "", "| Chỉ số | Giá trị |", "|---|---|",
          f"| Tài liệu | {len(clin['documents'])} (trích xuất {sum(1 for d in clin['documents'] if d.get('extracted'))}) |",
          f"| Resource clinical | " + ", ".join(f"{b} {len(clin.get(b) or [])}" for b in LIST_BLOCKS if clin.get(b)) + " |",
          f"| Cổng 1 quality_check | lỗi {qrep['errors']}, cảnh báo {qrep['warnings']}, info {qrep['infos']} |"]
    if brep:
        L.append(f"| FHIR | {brep['resources']} resource; bundle " + ", ".join(f"{k} ({v})" for k, v in brep["bundles"].items()) + " |")
    if vrep:
        L.append(f"| Cổng 2 validate_fhir | lỗi {vrep['errors']}, cảnh báo {vrep['warnings']}; profile {len(vrep.get('profiles_checked') or {})} |")
    if score:
        L.append(f"| So với ground truth | **F1 {score['f1']}** (P {score['precision']}, R {score['recall']}); khớp {score['matched']}, thừa {score['extra']}, thiếu {score['missing']} |")
    L.append(f"| LLM | {run['llm']['calls']} lời gọi (+{run['llm']['cache_hits']} cache), {run['llm']['prompt_tokens']:,} token vào, "
             f"{run['llm']['completion_tokens'] + run['llm']['thought_tokens']:,} token ra; thử lại {run['llm']['retries']}; dự phòng {run['llm']['fallbacks']}; "
             f"chi phí ước tính ${run['cost_usd']} |")
    L.append(f"| Thời gian | " + ", ".join(f"{k} {v}s" for k, v in run["timings_s"].items()) + " |")
    L.append(f"| Quyền riêng tư | {run['privacy']} |")
    L.append("")
    # 1 nguồn
    per = defaultdict(Counter)
    for b in LIST_BLOCKS + SINGLE_BLOCKS:
        items = clin.get(b)
        items = items if isinstance(items, list) else ([items] if items else [])
        for x in items:
            for s in x.get("sources") or []:
                per[s][b] += 1
    L += ["## 1. Nguồn từng khối dữ liệu", "", "| Tài liệu | form | trích xuất | resource trích từ phiếu |", "|---|---|---|---|"]
    for d in clin["documents"]:
        L.append(f"| {d['id']} | {d['form']} | {'✓' if d.get('extracted') else '✗ ' + (d.get('note') or '')} | " + ", ".join(f"{k} {v}" for k, v in per[d["id"]].items()) + " |")
    L.append("")
    # 2 mã hoá
    L += ["## 2. Quyết định mã hoá", "", "Mã chỉ đến từ phiếu in sẵn (document), bảng curated của vnlib (curated) hoặc tra cứu term_lookup (lookup); "
          "LLM chỉ chọn trong danh sách ứng viên. Khái niệm không có ứng viên chắc chắn giữ text (codings rỗng).", "",
          "| Resource | Văn bản | Mã | Quy tắc |", "|---|---|---|---|"]
    for d in decisions:
        code = d.get("icd10") or d.get("loinc") or ", ".join(d.get("codes") or []) or "—"
        if d.get("cls"):
            code += f" / CLS {d['cls']}"
        L.append(f"| {d['where']} | {str(d.get('text'))[:60]} | {code} | {d['rule']}{(' — ' + d['reason']) if d.get('reason') else ''} |")
    L.append("")
    # 3-5
    L += ["## 3. Mâu thuẫn (conflicts)", ""]
    for c in clin.get("conflicts") or []:
        vals = "; ".join(f"{v.get('value')} ({v.get('source')})" for v in c.get("values") or [] if isinstance(v, dict))
        L.append(f"- **{c.get('field')}**: {vals} → {c.get('resolution')} — {c.get('rationale') or ''}")
    L += ["", "## 4. Không ánh xạ được (unmapped)", ""] + [f"- {u.get('item')}: {u.get('reason')} → {u.get('action')}" for u in clin.get("unmapped") or []]
    L += ["", "## 5. Suy luận / ghi chú (review_notes)", ""] + [f"- {n}" for n in clin.get("review_notes") or []]
    # 6 cổng
    L += ["", "## 6. Kết quả các cổng", "", f"Cổng 1: lỗi {qrep['errors']}, cảnh báo {qrep['warnings']} (chi tiết `quality_report.md`)."]
    for i in qrep["issues"]:
        if i["severity"] in ("error", "warning"):
            L.append(f"- {i['severity']} `{i['where']}`: {i['message']}")
    if vrep:
        L += ["", f"Cổng 2: lỗi {vrep['errors']}, cảnh báo {vrep['warnings']} (chi tiết `fhir/validation/report.md`); binding không kiểm offline: "
                  f"{(vrep.get('stats') or {}).get('binding-external-or-uncomputable', 0)}."]
        for i in vrep["issues"]:
            if i["severity"] == "error":
                L.append(f"- error `{i['resource']}` {i['path']}: {i['message']}")
    if score:
        L += ["", "## 7. So với ground truth (compare.py)", "", "| Loại | GT | Ứng viên | Khớp | P | R |", "|---|---|---|---|---|---|"]
        for t, s in score["per_type"].items():
            if "matched" in s:
                L.append(f"| {t} | {s['gt']} | {s['candidate']} | {s['matched']} | {s['precision']} | {s['recall']} |")
    L += ["", "## 8. PII", "", "Đầu ra chứa dữ liệu cá nhân thật (họ tên, CCCD, BHYT…) — chỉ dùng nội bộ. Trước khi chia sẻ: "
          "`python -I vn-fhir/skills/vn-fhir-convert/scripts/deidentify.py` (PDPL 2023, NĐ 13/2023)."]
    return "\n".join(L) + "\n"
