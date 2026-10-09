"""Bước 2–3 — lập kế hoạch nhiệm vụ trích xuất theo loại phiếu và chạy theo đợt (wave), song song trong mỗi đợt.

  wave 1  admin                         đọc toàn bộ hồ sơ: người bệnh, thẻ, lượt điều trị, cơ sở/khoa, buồng/giường, nhân viên y tế
  wave 2  clinical (toàn bộ) + orders   chẩn đoán/tiền sử/hội chẩn; y lệnh CLS + thuốc + chế độ chăm sóc (tờ điều trị, hội chẩn)
  wave 3  results:doc / observe:doc / cost:doc   mỗi phiếu kết quả, phiếu theo dõi, bảng kê là một nhiệm vụ riêng
  wave 4  summary:doc                   Composition tóm tắt bệnh án, tham chiếu mọi resource đã có
Mỗi nhiệm vụ nhận REGISTRY (id + nhãn) của các đợt trước => tham chiếu chéo phiếu nhất quán mà không cần một lời gọi khổng lồ.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from . import kb as KB
from . import prompts as PR

RESULT_FORMS = {"ket-qua-xet-nghiem-huyet-hoc", "ket-qua-xet-nghiem-sinh-hoa-mau", "phieu-xet-nghiem-nuoc-dich", "ket-qua-xet-nghiem-vi-sinh",
                "ket-qua-giai-phau-benh", "phieu-chup-cong-huong-tu", "phieu-chup-cat-lop-vi-tinh", "phieu-chup-x-quang", "phieu-sieu-am-doppler",
                "phieu-dien-nao-do", "phieu-dien-tim", "phieu-noi-soi"}
OBSERVE_FORMS = {"phieu-kham-benh-vao-vien", "phieu-dieu-tri", "phieu-cham-soc", "trich-bien-ban-hoi-chan", "phieu-danh-gia-nguy-co-te-nga",
                 "phieu-sang-loc-dinh-duong", "ban-tom-tat-ho-so-benh-an", "benh-an", "phieu-phau-thuat-thu-thuat", "phieu-gay-me-hoi-suc", "phieu-truyen-mau"}
ORDER_FORMS = {"phieu-dieu-tri", "trich-bien-ban-hoi-chan", "benh-an"}
COST_FORMS = {"bang-ke-chi-phi-kcb"}
SUMMARY_FORMS = {"ban-tom-tat-ho-so-benh-an", "giay-ra-vien"}
AUX_FOR_RESULTS = {"phieu-dieu-tri", "ban-tom-tat-ho-so-benh-an"}


@dataclass
class Task:
    name: str                 # vd "results:doc-03"
    kind: str                 # khoá trong prompts.TASKS
    wave: int
    docs: list[str]
    aux: list[str] = field(default_factory=list)

    @property
    def spec(self):
        return PR.TASKS[self.kind]


def plan(documents: list[dict], texts: dict[str, str]) -> list[Task]:
    have = [d for d in documents if texts.get(d["id"], "").strip()]
    by_form = lambda forms: [d["id"] for d in have if d["form"] in forms]  # noqa: E731
    all_ids = [d["id"] for d in have]
    tasks = [Task("admin", "admin", 1, all_ids), Task("clinical", "clinical", 2, all_ids)]
    if by_form(ORDER_FORMS):
        tasks.append(Task("orders", "orders", 2, by_form(ORDER_FORMS)))
    aux = by_form(AUX_FOR_RESULTS)
    for d in have:
        if d["form"] in RESULT_FORMS:
            tasks.append(Task(f"results:{d['id']}", "results", 3, [d["id"]], aux))
        elif d["form"] in OBSERVE_FORMS:
            tasks.append(Task(f"observe:{d['id']}", "observe", 3, [d["id"]]))
        if d["form"] in COST_FORMS:
            tasks.append(Task(f"cost:{d['id']}", "cost", 3, [d["id"]]))
        if d["form"] in SUMMARY_FORMS:
            tasks.append(Task(f"summary:{d['id']}", "summary", 4, [d["id"]]))
    # phiếu chưa phân loại có chữ => coi như phiếu theo dõi (LLM tự quyết có gì để trích)
    for d in have:
        if d["form"] not in RESULT_FORMS | OBSERVE_FORMS | COST_FORMS | SUMMARY_FORMS | {"phieu-cong-khai-dich-vu"}:
            tasks.append(Task(f"observe:{d['id']}", "observe", 3, [d["id"]]))
    return tasks


# ---------------------------------------------------------------------- registry
def registry_text(state: dict, kinds: set[str]) -> str:
    lines = []

    def add(block, label_fn):
        if block not in kinds:
            return
        items = state.get(block)
        items = items if isinstance(items, list) else ([items] if items else [])
        for x in items:
            if isinstance(x, dict) and x.get("id"):
                lines.append(f"{x['id']} | {block} | {label_fn(x)}")

    add("patient", lambda x: (x.get("name") or {}).get("text", ""))
    add("encounter", lambda x: f"{(x.get('period') or {}).get('start', '')} → {(x.get('period') or {}).get('end', '')}")
    add("coverage", lambda x: x.get("bhyt_number", ""))
    add("organizations", lambda x: f"{x.get('kind')}: {x.get('name')}")
    add("locations", lambda x: f"{x.get('kind')}: {x.get('name')}")
    add("practitioners", lambda x: f"{x.get('name_raw') or (x.get('name') or {}).get('text')} {x.get('qualifications') or ''}")
    add("conditions", lambda x: f"{x.get('role')}: {x.get('text')}")
    add("service_requests", lambda x: f"{x.get('category')}: {(x.get('code') or {}).get('text')} @ {x.get('authored_on')}")
    add("medications", lambda x: (x.get("code") or {}).get("text", ""))
    add("medication_requests", lambda x: f"{x.get('medication')} @ {x.get('authored_on')}")
    add("specimens", lambda x: f"{(x.get('type') or {}).get('text')}")
    add("diagnostic_reports", lambda x: f"{x.get('kind')}: {(x.get('code') or {}).get('text')}")
    add("observations", lambda x: f"{x.get('kind')}: {(x.get('code') or {}).get('text')} @ {x.get('effective')}")
    add("allergies", lambda x: x.get("text") or ("NKA" if x.get("nka") else ""))
    add("family_history", lambda x: x.get("note") or "")
    add("clinical_impressions", lambda x: x.get("code_text") or "")
    add("care_plans", lambda x: x.get("title") or "")
    return "\n".join(lines) if lines else "(trống)"


REGISTRY_KINDS = {
    1: set(),
    2: {"patient", "encounter", "coverage", "organizations", "locations", "practitioners"},
    3: {"patient", "encounter", "coverage", "organizations", "locations", "practitioners", "conditions", "service_requests", "medications", "medication_requests"},
    4: {"patient", "encounter", "coverage", "organizations", "locations", "practitioners", "conditions", "service_requests", "medications", "medication_requests",
        "specimens", "diagnostic_reports", "observations", "allergies", "family_history", "clinical_impressions", "care_plans"},
}


def build_prompt(V, task: Task, documents: list[dict], texts: dict[str, str], state: dict, facility: dict) -> str:
    spec = task.spec
    owns = spec["owns"] + PR.COMMON_TAIL
    dmap = {d["id"]: d for d in documents}
    forms = sorted({dmap[i]["form"] for i in task.docs + task.aux if i in dmap})
    rows = KB.doc_type_rows()
    parts = [f"# NHIỆM VỤ: {spec['title']}", "", spec["instructions"], "",
             "## Trả về JSON với ĐÚNG các khoá sau (hình mẫu — giá trị <…> là chỗ trống, thay bằng dữ liệu trên phiếu; trường không có dữ liệu thì bỏ):",
             PR.shape(owns), ""]
    if facility.get("organization"):
        parts += [f"Cơ sở KCB chính dùng id cố định: `{facility['organization']}` (tên: {facility.get('name', '')}).", ""]
    guide = [f"- `{f}`: {rows[f]}" for f in forms if f in rows]
    if guide:
        parts += ["## Hướng dẫn theo loại phiếu (TT 32/2023)", *guide, ""]
    if task.kind in ("admin", "clinical"):
        parts += [KB.doc_section("Thứ tự ưu tiên"), ""]
    if task.kind in ("results", "observe", "orders"):
        parts += [KB.doc_section("Thời điểm"), ""]
    pits = KB.pitfalls_for(spec.get("pitfalls") or [])
    if pits:
        parts += ["## Cạm bẫy đã gặp trên hồ sơ thật", *[f"- {p}" for p in pits], ""]
    if spec.get("enums"):
        parts += ["## Danh mục mã được phép (mã=tên)", *[KB.enum_text(V, cs) for cs in spec["enums"]], ""]
    parts += ["## REGISTRY — id đã có (tham chiếu tới chúng; KHÔNG tạo lại, KHÔNG đổi id)", registry_text(state, REGISTRY_KINDS[task.wave]), ""]
    parts += ["## TÀI LIỆU ĐƯỢC GIAO"]
    for i in task.docs:
        d = dmap[i]
        parts += [f"### {i} · {d['form']} · {d['file']}", texts.get(i, "").strip() or "(không có lớp văn bản)", ""]
    if task.aux:
        parts += ["## TÀI LIỆU THAM KHẢO (chỉ dùng để đối chiếu / thay thế dòng lỗi font; không trích resource riêng từ đây)"]
        for i in task.aux:
            if i not in task.docs:
                d = dmap[i]
                parts += [f"### {i} · {d['form']} · {d['file']}", texts.get(i, "").strip(), ""]
    parts += [f"Nhắc lại: chỉ trả JSON với các khoá {owns}."]
    return "\n".join(parts)


def run_wave(client, V, tasks: list[Task], documents, texts, state, facility, store, log) -> dict[str, dict]:
    """Chạy các nhiệm vụ cùng đợt song song. store(task, prompt, result|error)."""
    results: dict[str, dict] = {}

    def one(t, prompt):
        """Gọi + kiểm hình dạng: phải là object có ít nhất một khoá của nhiệm vụ và không bị cắt ở max_tokens; sai -> thử lại 1 lần."""
        mt = max(t.spec.get("max_tokens", 16000), 4000)
        user, err = prompt, None
        for attempt in range(2):
            data, meta = client.json(t.name if attempt == 0 else f"{t.name}#retry", PR.SYSTEM, user, max_tokens=mt)
            keys = set(data) if isinstance(data, dict) else set()
            cut = (meta.get("usage") or {}).get("finish_reason") in ("length", "MAX_TOKENS")
            if keys & (set(t.spec["owns"]) | {"documents_patch", "conflicts", "unmapped", "review_notes", "compositions", "cost_statement"}) and not cut:
                return data, meta
            err = ("phản hồi bị cắt ở max_tokens" if cut else f"phản hồi thiếu các khoá của nhiệm vụ (có: {sorted(keys)[:8]})")
            if cut:
                mt = min(int(mt * 1.5), max(client.p.max_tokens, mt))
            user = prompt + ("\n\n# LƯU Ý\nLần trước " + err + ". Trả lời NGAY bằng một JSON object duy nhất theo hình mẫu ở trên, "
                             "không viết phân tích, không lời dẫn, không markdown. Giữ văn bản ngắn gọn (không lặp lại nguyên phiếu).")
        raise ValueError(err)

    with ThreadPoolExecutor(max_workers=max(1, client.p.concurrency)) as ex:
        futs = {}
        for t in tasks:
            prompt = build_prompt(V, t, documents, texts, state, facility)
            futs[ex.submit(one, t, prompt)] = (t, prompt)
        for f in as_completed(futs):
            t, prompt = futs[f]
            try:
                data, meta = f.result()
                if not isinstance(data, dict):
                    raise ValueError(f"phản hồi không phải JSON object: {type(data).__name__}")
                results[t.name] = data
                store(t, prompt, data, meta)
                log(f"  ✓ {t.name:28} {meta.get('latency_s', 0):6.1f}s  {'(cache)' if meta.get('cache_hit') else ''}")
            except Exception as e:  # noqa: BLE001
                store(t, prompt, {"_error": str(e)}, {})
                log(f"  ✗ {t.name:28} LỖI: {e}")
    return results


def dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False, indent=1)
