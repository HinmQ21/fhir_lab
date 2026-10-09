"""Bước 6–7 — hai cổng chất lượng của skill + vòng sửa có giới hạn.

Cổng 1  quality_check.Q(clinical).run()      schema, tham chiếu, mã, sinh hiệu, cờ H/L, dòng thời gian, số học bảng kê, sources
Cổng 2  build_fhir.Builder -> validate_fhir    profile VN Core 0.10.0 (cardinality, slice, binding, ~40 invariant vn-*)
Lỗi => gom theo resource -> LLM sửa ĐÚNG resource đó (kèm lỗi + văn bản phiếu nguồn) -> chuẩn hoá + mã hoá lại -> kiểm lại.
Chỉ nhận bản sửa khi số lỗi của resource giảm; không bao giờ "làm sạch" bằng cách xoá trừ khi LLM chứng minh không có trên phiếu (drop + unmapped).
"""
from __future__ import annotations

import copy
import json
import re
from collections import defaultdict
from pathlib import Path

from . import prompts as PR
from .assemble import LIST_BLOCKS, SINGLE_BLOCKS

LOC = re.compile(r"^/?([a-z_]+)(?:\[([^\]]+)\]|/(\d+))?")


def quality(kb, clin: dict) -> dict:
    return kb.qc.Q(copy.deepcopy(clin)).run()


def locate(clin: dict, where: str):
    """'observations[3].value' | 'observations[obs-x]' | 'observations/3/value' | 'encounter.period' -> (block, id)"""
    m = LOC.match(where or "")
    if not m:
        return None
    block, key = m.group(1), m.group(2) or m.group(3)
    if block in SINGLE_BLOCKS:
        return (block, (clin.get(block) or {}).get("id"))
    if block not in LIST_BLOCKS or key is None:
        return None
    items = clin.get(block) or []
    if key.isdigit() and int(key) < len(items):
        return (block, items[int(key)].get("id"))
    if any(x.get("id") == key for x in items):
        return (block, key)
    return None


def find(clin, block, id_):
    if block in SINGLE_BLOCKS:
        return clin.get(block)
    return next((x for x in clin.get(block) or [] if x.get("id") == id_), None)


def group_qc(clin, rep) -> dict[tuple, list[str]]:
    g = defaultdict(list)
    for i in rep["issues"]:
        if i["severity"] != "error":
            continue
        loc = locate(clin, i["where"])
        if loc:
            g[loc].append(f"{i['where']}: {i['message']}")
    return g


def build(kb, clin: dict, out: Path) -> dict:
    V = kb.V
    b = kb.build.Builder(copy.deepcopy(clin))
    b.build()
    rd = out / "resources"
    if rd.exists():
        for f in rd.glob("*.json"):
            f.unlink()
    for k in b.order:
        V.write_json(b.res[k], rd / f"{k.replace('/', '-')}.json")
    bundles = b.bundles()
    for name, bd in bundles.items():
        V.write_json(bd, out / name)
    counts = defaultdict(int)
    for k in b.order:
        counts[k.split("/")[0]] += 1
    rep = {"resources": len(b.order), "by_type": dict(sorted(counts.items())), "bundles": {k: len(v["entry"]) for k, v in bundles.items()},
           "unsourced_resources": b.unsourced, "warnings": b.warnings}
    V.write_json(rep, out / "build_report.json")
    return rep


def build_safe(kb, clin: dict, out: Path, notes: list, prep=lambda c: c, tries: int = 8) -> dict:
    """build(); nếu Builder báo tham chiếu treo (khoá chưa được drop_dangling biết) -> gỡ tham chiếu đó, ghi chú, dựng lại."""
    last = None
    for _ in range(tries):
        try:
            return build(kb, prep(clin), out)
        except kb.build.BuildError as e:
            m = re.search(r"không tồn tại[^:]*:\s*(\S+)", str(e))
            if not m or m.group(1) == last:
                raise
            last = m.group(1)
            n = _strip_ref(clin, last)
            notes.append(f"build_fhir: gỡ {n} tham chiếu tới id không tồn tại '{last}'")
    return build(kb, prep(clin), out)


def _strip_ref(node, id_) -> int:
    n = 0
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if k == "id":
                continue
            if v == id_:
                node.pop(k)
                n += 1
            elif isinstance(v, list) and id_ in v:
                node[k] = [x for x in v if x != id_]
                n += 1
            else:
                n += _strip_ref(v, id_)
    elif isinstance(node, list):
        for x in node:
            n += _strip_ref(x, id_)
    return n


def validate(kb, fhir_dir: Path, case: str) -> dict:
    return kb.validate.validate(fhir_dir, fhir_dir / "validation", case)


def group_fhir(clin, rep) -> dict[tuple, list[str]]:
    """Lỗi FHIR 'Type/id' -> resource clinical cùng id (builder giữ id)."""
    idx = {}
    for b in LIST_BLOCKS:
        for x in clin.get(b) or []:
            idx[x["id"]] = (b, x["id"])
    for b in SINGLE_BLOCKS:
        if clin.get(b) and clin[b].get("id"):
            idx[clin[b]["id"]] = (b, clin[b]["id"])
    cs = clin.get("cost_statement") or {}
    g = defaultdict(list)
    for i in rep["issues"]:
        if i["severity"] != "error":
            continue
        rid = (i.get("resource") or "").split("/")[-1]
        loc = idx.get(rid)
        if not loc and cs and rid.startswith(("claim-", "invoice-", "account-", "composition-")):
            loc = ("cost_statement", None)
        if loc:
            g[loc].append(f"{i.get('path')}: {i.get('message')}")
    return g


def repair(client, kb, clin: dict, groups: dict, texts: dict[str, str], max_items: int, log, stage: str) -> tuple[dict, int]:
    """Một vòng sửa: mỗi resource lỗi = 1 lời gọi. Trả (clinical mới, số resource đã sửa)."""
    from concurrent.futures import ThreadPoolExecutor
    jobs = list(groups.items())[:max_items]
    if not jobs:
        return clin, 0
    new = copy.deepcopy(clin)

    def one(job):
        (block, id_), msgs = job
        res = find(new, block, id_)
        if res is None:
            return None
        srcs = res.get("sources") or []
        docs_txt = "\n\n".join(f"### {d}\n{texts.get(d, '')[:12000]}" for d in srcs if texts.get(d))
        user = "\n".join([f"# SỬA resource `{block}` id `{id_}` (cổng {stage})", "", "## Lỗi bộ kiểm tra báo", *[f"- {m}" for m in msgs[:25]], "",
                          "## Hình mẫu khối", PR.shape([block] if block in PR.SHAPES else []), "",
                          "## Resource hiện tại", json.dumps(_clean(res), ensure_ascii=False, indent=1), "",
                          "## Văn bản phiếu nguồn", docs_txt or "(không có)", "",
                          'Trả {"resource": {...}} (giữ nguyên id) hoặc {"drop": true, "reason": "..."}.'])
        try:
            out, _ = client.json(f"repair:{stage}:{id_ or block}", PR.REPAIR_SYSTEM, user, max_tokens=12000)
        except Exception as e:  # noqa: BLE001
            log(f"    ! sửa {id_} lỗi: {e}")
            return None
        return (block, id_, out)

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, client.p.concurrency)) as ex:
        for r in ex.map(one, jobs):
            if not r:
                continue
            block, id_, out = r
            if not isinstance(out, dict):
                continue
            if out.get("drop"):
                if block in LIST_BLOCKS:
                    new[block] = [x for x in new[block] if x.get("id") != id_]
                    new["unmapped"].append({"item": f"{block}[{id_}]", "reason": out.get("reason") or "không hợp lệ và không đủ dữ liệu trên phiếu", "action": "đã bỏ trong vòng sửa — người duyệt kiểm tra"})
                    done += 1
                continue
            fixed = out.get("resource")
            if not isinstance(fixed, dict):
                continue
            if block in SINGLE_BLOCKS:
                fixed.setdefault("id", id_) if id_ else None
                new[block] = fixed
            else:
                fixed["id"] = id_
                new[block] = [fixed if x.get("id") == id_ else x for x in new[block]]
            done += 1
    return new, done


def _clean(o):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items() if not k.startswith("_")}
    if isinstance(o, list):
        return [_clean(x) for x in o]
    return o
