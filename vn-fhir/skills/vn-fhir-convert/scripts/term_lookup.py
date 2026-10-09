"""term_lookup.py — tra cứu danh mục mã (offline, từ vendor VN Core + bảng đã kiểm) để CHỌN mã, không bịa mã.

Quy tắc dùng: Claude gọi lệnh này để lấy danh sách ứng viên rồi chọn một (hoặc không chọn). Mỗi ứng viên có
`source` (document/lookup/curated/conceptmap) và `verified`; mã không có trong kết quả => không được ghi vào codings.

  python term_lookup.py icd10 "đau đầu"                      # ICD-10 VN (tên tiếng Việt) -> mã + tên EN (WHO)
  python term_lookup.py icd10-code M50.2
  python term_lookup.py cls "Định lượng Glucose" --unit mg/dL --appendix 02   # QĐ 1227 (vn-cls-cs) + LOINC map
  python term_lookup.py lab "Trung tính" --unit % --panel huyet_hoc           # LOINC xét nghiệm (bảng curated, nhạy đơn vị)
  python term_lookup.py imaging "Chụp cộng hưởng từ sọ não"                   # LOINC CĐHA + CLS
  python term_lookup.py vital "Mạch"                                          # LOINC sinh hiệu + đơn vị cố định + profile
  python term_lookup.py cs vn-department-cs "thần kinh"                       # tìm trong CodeSystem VN Core
  python term_lookup.py cs-code vn-qualification-cs BS-CKII                   # hiển thị 1 mã
  python term_lookup.py vs vn-specialty-vs [--contains XN]                    # thành viên ValueSet (expansion offline)
  python term_lookup.py ucum "G/L"                                            # đơn vị -> mã UCUM
  python term_lookup.py loinc 6690-2 [--online]                               # kiểm tra mã LOINC (check digit, bảng đã kiểm, NLM)
  python term_lookup.py doc-type phieu-dieu-tri | section lab_results         # LOINC loại tài liệu / mục Composition
  python term_lookup.py person "ThS.Bs Vương Quang Uyển"                      # tách tên + chức danh -> vn-qualification-cs
  python term_lookup.py datetime "08h34 02/02/2026"                           # -> ISO 8601 +07:00
  python term_lookup.py list-cs                                               # liệt kê CodeSystem có sẵn trong vendor
Tuỳ chọn chung: --top N, --vendor <dir>, --json (mặc định in JSON).
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vnlib as V  # noqa: E402

ASSETS = Path(__file__).resolve().parent.parent / "assets"


def out(obj, note=None):
    print(json.dumps(obj, ensure_ascii=False, indent=1))
    if note:
        print(f"# {note}", file=sys.stderr)


def cmd_icd10(a):
    rows = V.icd10_search(a.text, a.top)
    out({"query": a.text, "system": "icd10vn", "candidates": [dict(r, source="lookup", verified=True) for r in rows]},
        "chọn mã có ratio cao nhất CHỈ khi tên khớp nghĩa lâm sàng; nếu hồ sơ in sẵn mã ICD thì dùng mã in (source=document)")


def cmd_icd10_code(a):
    c, status = V.icd10_verify(a.code)
    r = V.icd10_lookup(a.code) or (V._icd_who().get(c) if c else None)
    out({"code": c or a.code, "found": status in ("vn", "who2019"), "table": status, **(r or {})},
        "table: vn = ICD-10 VN (QĐ 4469); who2019 = chỉ có trong ICD-10 WHO 2019; parent-only/invalid = không kiểm chứng được")


def cmd_cls(a):
    rows = V.cls_search(a.text, a.appendix, a.top, a.unit)
    out({"query": a.text, "unit": a.unit, "system": "cls", "candidates": [dict(r, source="lookup", verified=True) for r in rows]},
        "QĐ 1227 có khối CBC trùng lặp (1000147/1000170/1000198): dùng khối đầu tiên trừ khi khái niệm chỉ có ở khối sau")


def cmd_lab(a):
    rows = V.lab_loinc_candidates(a.text, a.unit, a.panel)
    verified = V.read_json(ASSETS / "loinc_verified.json") if (ASSETS / "loinc_verified.json").exists() else {}
    for r in rows:
        v = verified.get(r["code"])
        r["loinc_status"] = (v or {}).get("status") if v else ("KHÔNG TỒN TẠI" if r["code"] in verified else "chưa kiểm")
    out({"query": a.text, "unit": a.unit, "panel": a.panel, "system": "loinc", "candidates": rows},
        "ứng viên đầu khớp alias + loại đơn vị + bệnh phẩm; nếu không có ứng viên -> codings=[] và giữ text")


def cmd_imaging(a):
    out({"query": a.text, "candidates": V.imaging_candidates(a.text)})


def cmd_vital(a):
    key = V.VITAL_ALIASES.get(V.norm(a.text)) or next((k for al, k in V.VITAL_ALIASES.items() if V.norm(al) in V.norm(a.text)), None)
    if not key:
        out({"query": a.text, "candidates": []}, "không phải sinh hiệu chuẩn — dùng kind=exam với code.text")
        return
    loinc, en, vi, uc, label, prof = V.VITALS[key]
    out({"query": a.text, "key": key, "loinc": loinc, "display": en, "display_vi": vi, "ucum": uc, "unit_label_fixed": label, "profile": prof, "source": "curated", "verified": True},
        "unit_label_fixed là fixedString của profile VN Core (ví dụ '/min', '°C') — không được dịch sang 'lần/phút'")


def cmd_cs(a):
    rows = V.cs_search(a.cs_id, a.text, a.top)
    out({"codesystem": a.cs_id, "query": a.text, "candidates": [dict(r, source="lookup", verified=True) for r in rows]})


def cmd_cs_code(a):
    d = V.cs_display(a.cs_id, a.code)
    out({"codesystem": a.cs_id, "code": a.code, "display": d, "found": d is not None})


def cmd_vs(a):
    m = V.vs_members(a.vs_id)
    if m is None:
        out({"valueset": a.vs_id, "found": False}, "không có expansion offline — kiểm tra trong CodeSystem nguồn")
        return
    rows = sorted(m)
    if a.contains:
        out({"valueset": a.vs_id, "code": a.contains, "member": any(c == a.contains for _, c in rows)})
    else:
        out({"valueset": a.vs_id, "count": len(rows), "members": [{"system": s, "code": c, "display": V.cs_display(s.rsplit('/', 1)[-1], c)} for s, c in rows[: a.top if a.top != 8 else 400]]})


def cmd_ucum(a):
    code, label = V.ucum(a.text)
    out({"unit_raw": a.text, "ucum": code, "unit": label, "found": code is not None}, None if code else "đơn vị chưa có trong bảng UCUM của vnlib — bổ sung vào vnlib.UCUM nếu chắc chắn")


def cmd_loinc(a):
    verified = V.read_json(ASSETS / "loinc_verified.json") if (ASSETS / "loinc_verified.json").exists() else {}
    r = {"code": a.code, "check_digit_ok": V.loinc_check_digit(a.code), "in_verified_table": a.code in verified, "verified_entry": verified.get(a.code, "n/a")}
    if a.online:
        try:
            r["nlm"] = V.loinc_online(a.code)
        except Exception as e:
            r["nlm_error"] = str(e)
    out(r, "check_digit_ok=False => mã chắc chắn sai; verified_entry=None => NLM xác nhận không tồn tại")


def cmd_doc_type(a):
    t = V.DOC_TYPES.get(a.form)
    out({"form": a.form, "loinc": t[0] if t else None, "display": t[1] if t else None, "known_forms": sorted(V.DOC_TYPES)} if not t else {"form": a.form, "loinc": t[0], "display": t[1]})


def cmd_section(a):
    out({"key": a.key, "loinc": V.SECTIONS.get(a.key), "known_keys": sorted(V.SECTIONS)} if a.key not in V.SECTIONS else {"key": a.key, "loinc": V.SECTIONS[a.key]})


def cmd_person(a):
    p = V.parse_person(a.text)
    fam, giv = V.split_name(p["name"])
    out(dict(p, family=fam, given=giv, qualification_displays={q: V.cs_display("vn-qualification-cs", q) for q in p.get("titles", [])}))


def cmd_datetime(a):
    out({"text": a.text, "iso": V.vn_datetime(a.text)})


def cmd_list_cs(a):
    files = sorted(glob.glob(str(V.vendor() / "vncore" / "package" / "CodeSystem-*.json"))) + sorted(glob.glob(str(V.vendor() / "vncore" / "terminology" / "CodeSystem-*.json")))
    out([Path(f).stem.replace("CodeSystem-", "") for f in files])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vendor")
    ap.add_argument("--top", type=int, default=8)
    common = argparse.ArgumentParser(add_help=False)  # cho phép --top/--vendor đứng sau lệnh con
    common.add_argument("--top", type=int, default=argparse.SUPPRESS)
    common.add_argument("--vendor", default=argparse.SUPPRESS)
    _sub = ap.add_subparsers(dest="cmd", required=True)

    class _S:  # add_parser luôn kèm parents=[common]
        def add_parser(self, name):
            return _sub.add_parser(name, parents=[common])
    sub = _S()
    s = sub.add_parser("icd10"); s.add_argument("text"); s.set_defaults(f=cmd_icd10)
    s = sub.add_parser("icd10-code"); s.add_argument("code"); s.set_defaults(f=cmd_icd10_code)
    s = sub.add_parser("cls"); s.add_argument("text"); s.add_argument("--appendix"); s.add_argument("--unit"); s.set_defaults(f=cmd_cls)
    s = sub.add_parser("lab"); s.add_argument("text"); s.add_argument("--unit"); s.add_argument("--panel"); s.set_defaults(f=cmd_lab)
    s = sub.add_parser("imaging"); s.add_argument("text"); s.set_defaults(f=cmd_imaging)
    s = sub.add_parser("vital"); s.add_argument("text"); s.set_defaults(f=cmd_vital)
    s = sub.add_parser("cs"); s.add_argument("cs_id"); s.add_argument("text"); s.set_defaults(f=cmd_cs)
    s = sub.add_parser("cs-code"); s.add_argument("cs_id"); s.add_argument("code"); s.set_defaults(f=cmd_cs_code)
    s = sub.add_parser("vs"); s.add_argument("vs_id"); s.add_argument("--contains"); s.set_defaults(f=cmd_vs)
    s = sub.add_parser("ucum"); s.add_argument("text"); s.set_defaults(f=cmd_ucum)
    s = sub.add_parser("loinc"); s.add_argument("code"); s.add_argument("--online", action="store_true"); s.set_defaults(f=cmd_loinc)
    s = sub.add_parser("doc-type"); s.add_argument("form"); s.set_defaults(f=cmd_doc_type)
    s = sub.add_parser("section"); s.add_argument("key"); s.set_defaults(f=cmd_section)
    s = sub.add_parser("person"); s.add_argument("text"); s.set_defaults(f=cmd_person)
    s = sub.add_parser("datetime"); s.add_argument("text"); s.set_defaults(f=cmd_datetime)
    s = sub.add_parser("list-cs"); s.set_defaults(f=cmd_list_cs)
    a = ap.parse_args(argv)
    V.find_vendor(a.vendor)
    a.f(a)


if __name__ == "__main__":
    main()
