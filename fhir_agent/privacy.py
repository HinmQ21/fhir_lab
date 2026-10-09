"""PrivacyGuard — che PII trước khi gửi văn bản tới LLM cloud, trả lại sau khi nhận (PDPL 2023, NĐ 13/2023).

Thay thế NHẤT QUÁN: cùng giá trị gốc -> cùng token, nên model vẫn liên kết được "người ký phiếu A = người ký phiếu B".
  * họ tên (người bệnh, người nhà, nhân viên y tế)  -> [[P1]]   (biến thể slug trong id: pii-p1)
  * định danh (CCCD/CMND, BHYT, mã BN, mã bệnh án, số vào viện, mã lượt, số điện thoại…) -> [[ID1]]
  * ngày sinh -> [[D1]];  phần chi tiết của địa chỉ (số nhà, thôn, khu) -> [[A1]] (giữ xã/huyện/tỉnh để tra mã ĐVHC)
Không che: ngày giờ khám/điều trị, kết quả xét nghiệm, tên cơ sở/khoa (thông tin tổ chức).
Đây là che giảm rủi ro, không phải ẩn danh hoá tuyệt đối: báo cáo `report()` liệt kê số token theo loại để người vận hành duyệt.
"""
from __future__ import annotations

import re
import threading
import unicodedata

SURNAMES = ("Nguyễn Trần Lê Phạm Hoàng Huỳnh Phan Vũ Võ Đặng Bùi Đỗ Hồ Ngô Dương Đinh Đào Lương Trịnh Tạ Lâm Phùng Đoàn "
            "Vương Quách Hứa Triệu Lại Nông Khổng Đàm Lưu Mạc Nghiêm Thạch Tống Trương Lý Kiều Chu Doãn Lò Bế Diệp Khúc").split()
# từ thường gặp trong tiêu đề phiếu / thuật ngữ — chuỗi chứa chúng không phải họ tên ("Hồ Sơ Bệnh Án", "Lý Do Vào Viện")
NAME_STOP = set("Sơ Do Bệnh Án Viện Khoa Phiếu Xét Nghiệm Huyết Áp Đường Chẩn Đoán Điều Trị Chi Phí Kết Luận Tổng Cộng Số Mã Ngày Giờ "
                "Tháng Năm Kỳ Thuốc Dịch Vụ Máu Tim Phổi Não Thần Kinh Cột Sống Hồi Sức Cấp Cứu Khám Chữa Tỉnh Huyện Xã Phường".split())
UP = "A-ZÀÁẢÃẠĂẰẮẲẴẶÂẦẤẨẪẬĐÈÉẺẼẸÊỀẾỂỄỆÌÍỈĨỊÒÓỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢÙÚỦŨỤƯỪỨỬỮỰỲÝỶỸỴ"
LO = "a-zàáảãạăằắẳẵặâầấẩẫậđèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵ"
WORD = rf"[{UP}][{LO}]+"
WORD_UP = rf"[{UP}]{{1,7}}"
# một dấu cách giữa các chữ (cột bảng cách nhau nhiều dấu cách) + biên từ, để không nuốt chữ của cột kế tiếp
# nhãn vai trò đứng liền sau tên trên cùng dòng ("BSCKII Hà Xuân Tài Thư ký: …") không được gộp vào tên
ROLE_NEXT = r"(?!(?i:th[uư]\s*k[yý]|ch[uủ]\s*t[oọ]a|ng[uư][oờ]i|b[aá]c\s*s[iỹĩ]|[đd]i[eề]u\s*d[uư][oỡ]ng|tr[uư][oở]ng\s*(?:khoa|ph[oò]ng)|th[aà]nh\s*vi[eê]n)(?![\w]))"
NAME_TC = rf"{WORD}(?: {ROLE_NEXT}{WORD}){{1,4}}(?![\w])"                      # Nguyễn Văn An
NAME_UC = rf"{WORD_UP}(?![\w])(?: {ROLE_NEXT}{WORD_UP}(?![\w])){{1,4}}"        # NGUYỄN VĂN AN
SURN = "|".join(sorted({s for s in SURNAMES}, key=len, reverse=True))

LABEL_NAME = re.compile(
    rf"(?:H[oọ]\s*(?:v[aà]\s*)?t[eê]n(?:\s*(?:ng[uư][oờ]i\s*b[eệ]nh|NB|BN|ng[uư][oờ]i\s*nh[aà]|b[oố]|m[eẹ]|v[oợ]|ch[oồ]ng|con))?|"
    rf"Ng[uư][oờ]i\s*(?:b[eệ]nh|nh[aà]|li[eê]n\s*h[eệ]|b[aả]o\s*l[aã]nh|đại\s*diện|[KkTt][yý]|[Tt]h[uự]c\s*hi[eệ]n|[Ll][aậ]p(?:\s*b[aả]ng|\s*phi[eế]u)?|"
    rf"[Dd]uy[eệ]t|[Đđ][oọ]c(?:\s*k[eế]t\s*qu[aả])?|[Gg]iao|[Nn]h[aậ]n|[Cc]h[iỉ]\s*[đd][iị]nh|[Tt]r[aả]\s*k[eế]t\s*qu[aả])|K[yý]\s*t[eê]n|"
    rf"Tr[uư][oở]ng\s*(?:khoa|ph[oò]ng|t[uua]a)|B[aá]c\s*s[iỹĩ]\s*(?:[đd]i[eề]u\s*tr[iị]|kh[aá]m(?:\s*b[eệ]nh)?|ch[iỉ]\s*[đd][iị]nh|[đd][oọ]c)|"
    rf"[ĐD]i[eề]u\s*d[uư][oỡ]ng(?:\s*vi[eê]n)?|K[yỹ]\s*thu[aậ]t\s*vi[eê]n|T[eê]n\s*(?:ng[uư][oờ]i\s*b[eệ]nh|BN))(?:\s*\([^)\n]{{0,25}}\))?\s*[:\.]\s*({NAME_UC}|{NAME_TC})")
TITLE_NAME = re.compile(
    rf"(?<![\w])(?:PGS|GS|TS|ThS|Th\.S|BS\.?\s?CKII|BS\.?\s?CKI|BSCKII|BSCKI|BSNT|BS|Bs|bs|KTV|ĐD|CNĐD|DS|CN|YS|HS|KTV\.?)"
    rf"(?:[\.\s\-]*(?:BS|Bs|CKII|CKI|BSCKII|BSCKI))*[\.\s\-:]+({NAME_TC}|{NAME_UC})")
SURNAME_NAME = re.compile(rf"(?<![\w])((?:{SURN})(?: {ROLE_NEXT}{WORD}){{1,3}})(?![\w])")

ID_PATTERNS = [
    re.compile(r"(?<![\w])([A-Z]{2}\d{13})(?![\w])"),                     # thẻ BHYT
    re.compile(r"(?<![\d])(0\d{11})(?![\d])"),                            # CCCD
    re.compile(r"(?<![\d])(0[35789]\d{8})(?![\d])"),                      # số điện thoại di động
    re.compile(r"(?<![\w])(BN\d{6,12})(?![\w])"),                         # mã người bệnh
]
LABEL_ID = re.compile(
    r"(?:M[aã]\s*(?:BN|b[eệ]nh\s*nh[aâ]n|ng[uư][oờ]i\s*b[eệ]nh|b[eệ]nh\s*[aá]n|l[uư][oợ]t(?:\s*KCB)?|LK|li[eê]n\s*k[eế]t|y\s*t[eế]|l[uư]u\s*tr[uữ])|"
    r"S[oố]\s*(?:v[aà]o\s*vi[eệ]n|b[eệ]nh\s*[aá]n|l[uư]u\s*tr[uữ]|th[eẻ]|CCCD|CMND|[dđ]i[eệ]n\s*tho[aạ]i|ĐT|h[oồ]\s*s[oơ])|"
    r"S[oổ]\s*l[uư]u\s*tr[uữ]|CCCD|CMND|Đi[eệ]n\s*tho[aạ]i|ĐT|SĐT)\s*[:\.]?\s*([A-Za-z0-9][A-Za-z0-9/\-\.]{3,24})")
DOB = re.compile(r"(?:Ng[aà]y\s*(?:,\s*th[aá]ng\s*,\s*n[aă]m\s*)?sinh|Sinh\s*ng[aà]y|N[aă]m\s*sinh)\s*[:\.]?\s*(\d{1,2}\s*[/\-\.]\s*\d{1,2}\s*[/\-\.]\s*\d{2,4}|\d{4})")
ADDR = re.compile(r"(?:Đ[iị]a\s*ch[iỉ](?:\s*(?:th[uư][oờ]ng\s*tr[uú]|li[eê]n\s*h[eệ]))?)\s*[:\.]\s*([^\n]{3,200})")
AFTER_UNIT = re.compile(r"(?:X[aã]|Ph[uư][oờ]ng|Th[iị]\s*tr[aấ]n|Huy[eệ]n|Qu[aậ]n|Th[iị]\s*x[aã]|T[iỉ]nh|Th[aà]nh\s*ph[oố]|TP\.?|Th[oô]n|X[oó]m|"
                        r"Ấp|B[aả]n|T[oổ]\s*d[aâ]n\s*ph[oố]|Đ[uư][oờ]ng|Ph[oố]|Khu|B[eệ]nh\s*vi[eệ]n|Tr[aạ]m\s*y\s*t[eế])\s+$", re.I)
ADMIN_UNIT = re.compile(r"(?:X[aã]|Ph[uư][oờ]ng|Th[iị]\s*tr[aấ]n|Huy[eệ]n|Qu[aậ]n|Th[iị]\s*x[aã]|T[iỉ]nh|Th[aà]nh\s*ph[oố]|TP\.?)\s", re.I)


def _nfc(s):
    return unicodedata.normalize("NFC", s)


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFD", _nfc(s).replace("đ", "d").replace("Đ", "D"))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


NOT_NAMES = {"việt nam", "phú thọ", "hà nội", "ra viện", "vào viện", "bệnh viện", "tổng cộng"}


class PrivacyGuard:
    def __init__(self):
        self.vault: dict[str, str] = {}      # giá trị gốc -> token text "[[P1]]"
        self.back: dict[str, str] = {}       # token -> giá trị gốc
        self.kinds: dict[str, int] = {}
        self.lock = threading.Lock()
        self._rx = None

    # ------------------------------------------------------------------ học từ văn bản
    def learn(self, text: str):
        text = _nfc(text)
        for m in LABEL_NAME.finditer(text):
            self._add(m.group(1), "P")
        for m in TITLE_NAME.finditer(text):
            self._add(m.group(1), "P")
        for m in SURNAME_NAME.finditer(text):
            before = text[max(0, m.start() - 22):m.start()]
            if AFTER_UNIT.search(before):      # "Phường Nông Trang", "Khoa Lê Lợi"… là địa danh / tổ chức, không phải người
                continue
            if not (set(m.group(1).split()[1:]) & NAME_STOP):
                self._add(m.group(1), "P")
        for m in DOB.finditer(text):
            self._add(m.group(1), "D")
        for m in ADDR.finditer(text):
            val = m.group(1).strip()
            u = ADMIN_UNIT.search(val)
            detail = val[:u.start()].strip(" ,;-") if u else val.strip(" ,;-")
            if len(detail) >= 3:
                self._add(detail, "A")
        for m in LABEL_ID.finditer(text):
            v = m.group(1).strip(".-/")
            if re.search(r"\d", v) and len(v) >= 4:
                self._add(v, "ID")
        for rx in ID_PATTERNS:
            for m in rx.finditer(text):
                self._add(m.group(1), "ID")

    def _add(self, value: str, kind: str):
        v = re.sub(r"\s+", " ", _nfc(value)).strip()
        if not v or v.lower() in NOT_NAMES:
            return
        if kind == "P":
            words = v.split()
            if len(words) < 2 or len(v) < 5:
                return
            v_tc = " ".join(w.capitalize() if w.isupper() else w for w in words)
        with self.lock:
            key = v if kind != "P" else v_tc
            if key in self.vault:
                tok = self.vault[key]
            else:
                n = self.kinds.get(kind, 0) + 1
                self.kinds[kind] = n
                tok = f"[[{kind}{n}]]"
                self.vault[key] = tok
                self.back[tok] = key
            if kind == "P":
                self.vault.setdefault(v_tc.upper(), tok)
                self.vault.setdefault(v, tok)
            self._rx = None

    # ------------------------------------------------------------------ che / trả lại
    def _regex(self):
        if self._rx is None:
            keys = sorted(self.vault, key=len, reverse=True)
            self._rx = re.compile("|".join(rf"(?<![\w]){re.escape(k)}(?![\w])" for k in keys)) if keys else None
            self._slugs = {}
            for k, tok in self.vault.items():
                if tok.startswith("[[P"):
                    s = _slug(k)
                    if len(s) >= 5:
                        self._slugs[s] = "pii-" + tok[2:-2].lower()
            self._slug_rx = re.compile("|".join(re.escape(s) for s in sorted(self._slugs, key=len, reverse=True))) if self._slugs else None
        return self._rx

    def mask(self, text: str) -> str:
        if not text:
            return text
        text = _nfc(text)
        for rx in ID_PATTERNS:  # định danh còn sót (vd xuất hiện lần đầu trong registry)
            for m in rx.finditer(text):
                if m.group(1) not in self.vault:
                    self._add(m.group(1), "ID")
        rx = self._regex()
        if rx:
            text = rx.sub(lambda m: self.vault[m.group(0)], text)
        if self._slug_rx:
            text = self._slug_rx.sub(lambda m: self._slugs[m.group(0)], text)
        return text

    def unmask(self, obj):
        if isinstance(obj, str):
            if "[[" in obj:
                obj = re.sub(r"\[\[([A-Z]+\d+)\]\]", lambda m: self.back.get(f"[[{m.group(1)}]]", m.group(0)), obj)
            if "pii-" in obj:
                obj = re.sub(r"pii-([a-z]+\d+)", lambda m: _slug(self.back.get(f"[[{m.group(1).upper()}]]", m.group(0))) or m.group(0), obj)
            return obj
        if isinstance(obj, list):
            return [self.unmask(x) for x in obj]
        if isinstance(obj, dict):
            return {self.unmask(k) if isinstance(k, str) else k: self.unmask(v) for k, v in obj.items()}
        return obj

    def report(self) -> dict:
        return {"tokens": dict(self.kinds), "values": len(self.back),
                "note": "che theo mẫu (nhãn họ tên, học hàm + tên, họ phổ biến, mã định danh, ngày sinh, địa chỉ chi tiết); tên không theo mẫu có thể còn sót"}
