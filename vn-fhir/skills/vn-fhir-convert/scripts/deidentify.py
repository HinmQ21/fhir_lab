"""deidentify.py — ẩn danh hoá clinical JSON v2 (và thư mục FHIR đi kèm) trước khi chia sẻ / làm ví dụ.

Nguyên tắc (PDPL 2023, NĐ 13/2023): dữ liệu sức khoẻ là dữ liệu cá nhân nhạy cảm. Bộ này thay thế NHẤT QUÁN
(cùng một giá trị gốc -> cùng một giá trị giả) nên quan hệ giữa các resource vẫn giữ nguyên:
  * họ tên người bệnh, người nhà, nhân viên y tế  -> tên giả (giữ họ phổ biến, đổi tên đệm/tên)
  * mọi định danh (CCCD, BHYT, MRN, mã bệnh án, mã lượt, số vào viện, mã bệnh phẩm, mã liên kết…) -> cùng dạng, chữ số đổi theo HMAC
  * số điện thoại                                    -> chữ số đổi theo HMAC
  * địa chỉ: dòng chi tiết (số nhà/khu/thôn) bị thay bằng "[đã ẩn]"; xã/tỉnh giữ nguyên (mức ĐVHC, không định danh cá nhân)
  * ngày/giờ: dịch toàn bộ một số ngày cố định (giữ khoảng cách, giờ, thứ tự sự kiện); ngày sinh chỉ giữ năm (01/01)
  * mã băm tài liệu (sha1) -> HMAC (vì có thể dùng để truy ngược bản PDF gốc)
Không ẩn: tên cơ sở KCB, tên khoa, mã CSKCB (thông tin tổ chức công khai) — trừ khi --hide-facility.

Cách chạy:
  python deidentify.py <clinical.json> <out_dir> [--fhir-dir <dir>] [--salt <chuỗi bí mật>] [--shift-days N] [--keep-map] [--hide-facility]

Bản đồ ánh xạ (gốc -> giả) chỉ được ghi ra khi --keep-map (file deid_map.json là dữ liệu nhạy cảm: cất ngoài repo).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import re
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vnlib as V  # noqa: E402

GIVEN_POOL = ["An", "Bình", "Châu", "Dung", "Giang", "Hà", "Hạnh", "Hiền", "Hoa", "Hùng", "Khanh", "Lan", "Linh", "Long", "Mai", "Minh", "Nam", "Nga", "Ngọc", "Phong", "Phương", "Quân", "Quỳnh", "Sơn", "Thảo", "Thắng", "Trang", "Trung", "Tuấn", "Tùng", "Vân", "Việt", "Yến"]
MIDDLE_F = ["Thị", "Thị Thu", "Thị Minh", "Ngọc", "Thanh", "Mai", "Kim", "Hồng", "Thuỳ", "Diệu", "Bảo", "Khánh"]
MIDDLE_M = ["Văn", "Đức", "Hữu", "Quang", "Minh", "Thanh", "Gia", "Bảo", "Khánh", "Phúc", "Nhật", "Công"]
FAMILY_POOL = ["Nguyễn", "Trần", "Lê", "Phạm", "Hoàng", "Vũ", "Đặng", "Bùi", "Đỗ", "Hồ", "Ngô", "Dương"]


class Deid:
    def __init__(self, salt: str, shift_days: int | None, hide_facility: bool):
        self.key = salt.encode("utf-8")
        self.shift = shift_days if shift_days is not None else (self._int("shift", 1000) % 300) + 30  # 30..329 ngày
        self.hide_facility = hide_facility
        self.map: dict[str, str] = {}
        self.names: dict[str, str] = {}

    # ---- primitives
    def _h(self, s: str) -> bytes:
        return hmac.new(self.key, s.encode("utf-8"), hashlib.sha256).digest()

    def _int(self, s: str, mod: int) -> int:
        return int.from_bytes(self._h(s)[:8], "big") % mod

    def digits(self, s: str) -> str:
        """Giữ ký tự không phải số; thay chữ số bằng chữ số HMAC (cùng độ dài). Chữ số đầu khác 0 nếu chuỗi toàn số."""
        h = self._h("id:" + s)
        stream = "".join(str(b % 10) for b in h) * 4
        out, i = [], 0
        for ch in s:
            if ch.isdigit():
                d = stream[i]; i += 1
                if not out and len(s) > 1 and s.isdigit() and d == "0":
                    d = "7"
                out.append(d)
            else:
                out.append(ch)
        return "".join(out)

    def fake_name(self, full: str, gender: str | None) -> str:
        if full in self.names:
            return self.names[full]
        n = self._int("name:" + full, 10 ** 6)
        orig = {V.nfc(t).lower() for t in full.split()} | getattr(self, "real_tokens", set())
        mids = MIDDLE_F if gender == "female" else MIDDLE_M if gender == "male" else MIDDLE_M + MIDDLE_F
        def pick(pool, start):
            for i in range(len(pool)):
                x = pool[(start + i) % len(pool)]
                if all(t.lower() not in orig for t in x.split()):
                    return x
            return pool[start % len(pool)]
        fam, mid = pick(FAMILY_POOL, n), pick(mids, n // 13)
        k = 0
        while True:  # bảo đảm hai người thật không nhận cùng một tên giả
            giv = pick(GIVEN_POOL, n // 97 + k)
            fake = f"{fam} {mid} {giv}"
            if (fake not in self.names.values() and giv not in mid.split()) or k > len(GIVEN_POOL):
                break
            k += 1
        self.names[full] = fake
        return fake

    def shift_date(self, s: str) -> str:
        try:
            d = date.fromisoformat(s[:10])
        except ValueError:
            return s
        return (d + timedelta(days=self.shift)).isoformat() + s[10:]

    def shift_dmy(self, m: re.Match) -> str:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        try:
            nd = date(y, mo, d) + timedelta(days=self.shift)
        except ValueError:
            return m.group(0)
        return f"{nd.day:02d}/{nd.month:02d}/{nd.year}"

    # ---- plan: build replacement map from clinical structure
    def plan(self, clin: dict):
        pt = clin.get("patient", {})
        # mọi token tên thật (tất cả người trong hồ sơ) -> không được xuất hiện trong bất kỳ tên giả nào
        toks: set[str] = set()
        for full in [pt.get("name", {}).get("text"), pt.get("name_raw")] + [p.get("name", {}).get("text") for p in clin.get("practitioners", []) or []]                 + [p.get("name_raw") for p in clin.get("practitioners", []) or []] + [c.get("name") for c in pt.get("contacts", []) or []] + [r.get("name", {}).get("text") for r in clin.get("related_persons", []) or []]:
            if full:
                toks |= {V.nfc(t).lower() for t in re.split(r"[\s\-\.]+", full) if t}
        self.real_tokens = toks - {"bs", "bsckii", "bscki", "bsnt", "ths", "ts", "ktv", "đd", "dd", "khh", "cn", "ds", "ys", "hs", "pgs", "gs"}
        g = pt.get("gender")
        for n in {pt.get("name", {}).get("text"), pt.get("name_raw")} - {None}:
            self.map[n] = self.fake_name(pt["name"]["text"], g)
        for c in pt.get("contacts", []) or []:
            if c.get("name"):
                self.map[c["name"]] = self.fake_name(c["name"], None)
            for t in c.get("telecom", []) or []:
                if t.get("value"):
                    self.map[t["value"]] = self.digits(t["value"])
        for p in clin.get("practitioners", []) or []:
            full = p.get("name", {}).get("text") or p.get("name_raw")
            if not full:
                continue
            fake = self.fake_name(full, None)
            self.map[full] = fake
            if p.get("name_raw") and p["name_raw"] != full:
                self.map[p["name_raw"]] = re.sub(re.escape(full), fake, p["name_raw"], flags=re.I) if re.search(re.escape(full), p["name_raw"], re.I) else fake
            for i in p.get("identifiers", []) or []:
                if i.get("value"):
                    self.map[i["value"]] = V.slug(fake) if not any(ch.isdigit() for ch in i["value"]) else self.digits(i["value"])
        for rp in clin.get("related_persons", []) or []:
            if rp.get("name", {}).get("text"):
                self.map[rp["name"]["text"]] = self.fake_name(rp["name"]["text"], rp.get("gender"))
        # identifiers anywhere
        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k in ("identifiers",) and isinstance(v, list):
                        for i in v:
                            if isinstance(i, dict) and i.get("value") and any(ch.isdigit() for ch in i["value"]):
                                self.map[i["value"]] = self.digits(i["value"])
                    elif k in ("bhyt_number", "ma_lk", "so_bang_ke", "case_id", "accession", "requisition") and isinstance(v, str) and any(ch.isdigit() for ch in v):
                        self.map[v] = self.digits(v)
                    else:
                        walk(v)
            elif isinstance(node, list):
                for x in node:
                    walk(x)
        walk(clin)
        for d in clin.get("documents", []) or []:
            if d.get("sha1_b64"):
                self.map[d["sha1_b64"]] = base64.b64encode(self._h("sha1:" + d["sha1_b64"])[:20]).decode()
        if self.hide_facility:
            for o in clin.get("organizations", []) or []:
                self.map[o["name"]] = f"Cơ sở KCB {self._int('org:' + o['id'], 90) + 10}"
                for a in o.get("aliases", []) or []:
                    self.map[a] = self.map[o["name"]]
        # longest first so that full names win over substrings
        self.ordered = sorted((k for k in self.map if k), key=len, reverse=True)
        self.patterns = [(re.compile(r"(?<![\w])" + re.escape(k) + r"(?![\w])", re.I | re.U), k) for k in self.ordered]

    # ---- apply to arbitrary text (JSON serialized or narrative)
    def apply_text(self, text: str) -> str:
        for pat, k in self.patterns:
            rep = self.map[k]

            def sub(m, rep=rep):
                s = m.group(0)
                if s.islower():
                    return rep.lower()
                if s.isupper() and len(s) > 1:
                    return rep.upper()
                return rep
            text = pat.sub(sub, text)
        text = re.sub(r"\b(\d{4}-\d{2}-\d{2})(T[0-9:+\-Z\.]*)?", lambda m: self.shift_date(m.group(1)) + (m.group(2) or ""), text)
        text = re.sub(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", self.shift_dmy, text)
        text = re.sub(r"\b(?:0|\+84)\d{1,2}[\.\s]?\d{3}[\.\s]?\d{3,4}\b|\b1800[\.\s]?\d{3}[\.\s]?\d{3}\b", lambda m: self.digits(m.group(0)), text)
        return text

    def apply_json(self, obj, path=""):
        """Xử lý có cấu trúc trước (tên, địa chỉ, ngày sinh), rồi thay thế theo text."""
        s = json.dumps(obj, ensure_ascii=False)
        s = self.apply_text(s)
        out = json.loads(s)
        self._structured(out)
        return out

    def _structured(self, clin):
        pt = clin.get("patient") if isinstance(clin, dict) else None
        if isinstance(pt, dict):
            self._fix_name(pt)
            if pt.get("birth_date"):
                pt["birth_date"] = pt["birth_date"][:4] + "-01-01"
            self._addr(pt.get("address"))
            pt.pop("name_raw", None)
        for p in (clin.get("practitioners") or []) if isinstance(clin, dict) else []:
            self._fix_name(p)
            p.pop("name_raw", None)
        for rp in (clin.get("related_persons") or []) if isinstance(clin, dict) else []:
            self._fix_name(rp)
            self._addr(rp.get("address"))
        # FHIR resources
        if isinstance(clin, dict) and clin.get("resourceType") in ("Patient", "Practitioner", "RelatedPerson", "Person"):
            for n in clin.get("name", []) or []:
                if n.get("text"):
                    fam, giv = V.split_name(n["text"])
                    n["family"], n["given"] = fam, giv
            if clin.get("birthDate"):
                clin["birthDate"] = clin["birthDate"][:4] + "-01-01"
            for a in clin.get("address", []) or []:
                self._addr_fhir(a)
        if isinstance(clin, dict) and clin.get("resourceType") == "Bundle":
            for e in clin.get("entry", []) or []:
                if isinstance(e.get("resource"), dict):
                    self._structured(e["resource"])

    @staticmethod
    def _fix_name(p):
        n = p.get("name")
        if isinstance(n, dict) and n.get("text"):
            fam, giv = V.split_name(n["text"])
            n["family"], n["given"] = fam, giv

    @staticmethod
    def _addr(a):
        if isinstance(a, dict):
            if a.get("line"):
                a["line"] = ["[đã ẩn]"]
            parts = [x for x in (a.get("ward"), a.get("city") if a.get("city") != a.get("ward") else None, a.get("district"), a.get("state")) if x]
            a["text"] = ", ".join(["[đã ẩn]"] + parts)

    @staticmethod
    def _addr_fhir(a):
        if a.get("line"):
            a["line"] = ["[đã ẩn]"]
        parts = [x for x in (a.get("city"), a.get("district"), a.get("state")) if x]
        a["text"] = ", ".join(["[đã ẩn]"] + parts)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clinical")
    ap.add_argument("out_dir")
    ap.add_argument("--fhir-dir", help="thư mục FHIR (resources/*.json + bundle-*.json) để ẩn danh song song")
    ap.add_argument("--salt", default=None, help="khoá HMAC; mặc định sinh ngẫu nhiên (không tái lập được)")
    ap.add_argument("--shift-days", type=int, default=None)
    ap.add_argument("--keep-map", action="store_true", help="ghi deid_map.json (NHẠY CẢM) vào out_dir")
    ap.add_argument("--hide-facility", action="store_true")
    a = ap.parse_args(argv)

    salt = a.salt or base64.b64encode(hashlib.sha256(str(datetime.now().timestamp()).encode()).digest()).decode()
    clin = V.read_json(a.clinical)
    d = Deid(salt, a.shift_days, a.hide_facility)
    d.plan(clin)
    out = Path(a.out_dir); out.mkdir(parents=True, exist_ok=True)
    res = d.apply_json(clin)
    res.setdefault("meta", {})["deidentified"] = {"method": "vn-fhir-convert deidentify.py (HMAC-consistent pseudonyms, date shift)", "date_shift_days": d.shift, "note": "Dữ liệu đã ẩn danh — không dùng cho chăm sóc người bệnh"}
    res["meta"]["case_id"] = d.map.get(clin["meta"].get("case_id", ""), res["meta"].get("case_id"))
    V.write_json(res, out / Path(a.clinical).name)
    n_files = 1
    if a.fhir_dir:
        src = Path(a.fhir_dir)
        for f in sorted(src.rglob("*.json")):
            if "validation" in f.parts:
                continue
            rel = f.relative_to(src)
            obj = V.read_json(f)
            obj2 = d.apply_json(obj)
            # id đổi theo text (ví dụ pt-bn001736827 -> pt-bn<số giả>) => đổi tên file cho khớp
            new_name = d.apply_text(rel.name)
            dest = out / "fhir" / rel.parent / new_name
            dest.parent.mkdir(parents=True, exist_ok=True)
            V.write_json(obj2, dest)
            n_files += 1
    if a.keep_map:
        V.write_json({"salt": salt, "shift_days": d.shift, "map": d.map}, out / "deid_map.json")
    print(f"[deidentify] {n_files} tệp -> {out} | dịch ngày {d.shift} | {len(d.map)} giá trị thay thế" + (" | deid_map.json (NHẠY CẢM)" if a.keep_map else ""))


if __name__ == "__main__":
    main()
