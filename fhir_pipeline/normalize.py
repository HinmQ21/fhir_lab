"""Bước 2: JSON tài liệu (TT32) -> clinical JSON (định dạng trung gian như data/sample/clinical.json).

Nguyên tắc:
- Mapper xác định (deterministic) theo từng loại phiếu; quy tắc ưu tiên khi nhiều phiếu cùng cho một trường
  (bảng kê chi phí > tóm tắt HSBA > phiếu khám vào viện > các phiếu khác).
- Mã hoá: tra bảng trước (sinh hiệu LOINC VN Core, LAB_LOINC curated, CLS->LOINC, ICD-10 VN), chỉ hỏi LLM phần còn thiếu
  (ICD-10 cho chẩn đoán chữ, LOINC XN/CĐHA lạ, ATC thuốc) rồi kiểm tra lại mã ICD với từ điển.
- Giữ nguyên văn (text/original_text) + mã + UCUM + thời gian ISO để bước 3 dựng FHIR không cần đọc lại phiếu.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .terminology import (ICD_RE, LAB_PANELS, SYS, VITALS, VITAL_ALIASES, icd10_lookup, imaging_loinc, lab_loinc, loinc_check_digit,
                          nfc, norm, parse_person, parse_ref_range, to_number, ucum, vn_datetime)
from .util import load

VERSION = "0.10.0"  # VN Core IG version dùng làm 'version' của coding


def first(*vals):
    for v in vals:
        if v not in (None, "", [], {}):
            return v
    return None


def clean(s):
    if isinstance(s, str):
        s = nfc(s).strip()
        return s or None
    return s


def pos(text: str | None, hay: str | None = None):
    if not text:
        return [0, 0]
    if hay and text in hay:
        i = hay.index(text)
        return [i, i + len(text)]
    return [0, len(text)]


# ---------------------------------------------------------------- tải tài liệu
def load_docs(out_dir):
    """Đọc manifest + JSON tài liệu của một ca từ thư mục output/<case>."""
    out_dir = Path(out_dir)
    man = load(out_dir / "manifest.json")
    docs = {}
    for d in man["documents"]:
        if d.get("data_file"):
            docs.setdefault(d["form"], []).append({**d, "data": load(out_dir / "01_documents" / d["data_file"])})
    return man, docs


def one(docs, form):
    return docs.get(form, [{}])[0].get("data") if docs.get(form) else None


# ---------------------------------------------------------------- mappers
def map_patient(docs):
    kb = one(docs, "phieu-kham-benh-vao-vien") or {}
    tt = one(docs, "ban-tom-tat-ho-so-benh-an") or {}
    bk = one(docs, "bang-ke-chi-phi-kcb") or {}
    hc = kb.get("I. HÀNH CHÍNH", {})
    tthc = tt.get("HÀNH CHÍNH", {})
    bkhc = bk.get("I. Phần hành chính", {})
    gender_box = hc.get("Giới tính") or tthc.get("Giới tính") or {}
    gioi = "Nữ" if gender_box.get("Nữ") else "Nam" if gender_box.get("Nam") else clean(bkhc.get("Giới tính"))
    dia_chi_day_du = first(clean(tthc.get("Địa chỉ")), clean(bkhc.get("Địa chỉ hiện tại")), clean(hc.get("Địa chỉ")))
    parts = [p.strip() for p in (dia_chi_day_du or "").split(",") if p.strip()]
    xa = next((p for p in parts if re.match(r"(Xã|Phường|Thị trấn)", p, re.I)), None)
    tinh = first(clean(hc.get("Tỉnh, thành phố")), next((p for p in parts if re.match(r"(Tỉnh|Thành phố|TP)", p, re.I)), None))
    thon = parts[0] if parts and parts[0] != xa else None
    return {
        "ho_ten": first(clean(hc.get("Họ và tên")), clean(tthc.get("Họ tên")), clean(bkhc.get("Họ và tên người bệnh"))),
        "ma_benh_nhan": first(clean(kb.get("THÔNG TIN BIỂU MẪU", {}).get("Mã bệnh nhân")), clean(bk.get("THÔNG TIN BIỂU MẪU", {}).get("Mã số người bệnh"))),
        "ma_y_te": clean(kb.get("THÔNG TIN BIỂU MẪU", {}).get("Số vào viện")),
        "ngay_thang_nam_sinh": first(clean(hc.get("Sinh ngày")), clean(tthc.get("Ngày sinh")), clean(bkhc.get("Ngày tháng năm sinh"))),
        "tuoi": to_number(re.sub(r"\D", "", str(first(hc.get("Tuổi"), tthc.get("Tuổi")) or ""))),
        "gioi_tinh": gioi,
        "so_cccd": clean(tthc.get("Số căn cước/hộ chiếu/mã định danh cá nhân")),
        "nghe_nghiep": clean(hc.get("Nghề nghiệp")),
        "ma_nghe_nghiep": clean(hc.get("Mã nghề nghiệp")),
        "dan_toc": first(clean(hc.get("Dân tộc")), clean(tthc.get("Dân tộc"))),
        "ma_dan_toc": clean(hc.get("Mã dân tộc")),
        "ngoai_kieu": clean(hc.get("Ngoại kiều")),
        "ma_ngoai_kieu": clean(hc.get("Mã ngoại kiều")),
        "noi_lam_viec": clean(hc.get("Nơi làm việc")),
        "dia_chi": {
            "so_nha": None, "thon_pho": thon, "xa_phuong": xa, "huyen_q_tx": clean(hc.get("Huyện (Q, Tx)")),
            "ma_huyen_q_tx": clean(hc.get("Mã huyện (Q, Tx)")), "tinh_thanh_pho": tinh,
            "ma_tinh_thanh_pho": clean(hc.get("Mã tỉnh, thành phố")), "ma_xa_phuong_his": clean(hc.get("Mã địa chỉ")),
            "dia_chi_day_du": dia_chi_day_du,
        },
    }


def map_related_person(docs):
    hc = (one(docs, "phieu-kham-benh-vao-vien") or {}).get("I. HÀNH CHÍNH", {})
    return {"ho_ten_dia_chi_nguoi_nha": clean(hc.get("Họ tên, địa chỉ người nhà khi cần báo tin")),
            "dien_thoai_nguoi_nha": clean(hc.get("Số điện thoại")), "dia_chi_nguoi_nha": clean(hc.get("Địa chỉ người nhà")), "quan_he": None}


def map_organization(docs):
    kb = one(docs, "phieu-kham-benh-vao-vien") or {}
    bk = one(docs, "bang-ke-chi-phi-kcb") or {}
    tt = one(docs, "ban-tom-tat-ho-so-benh-an") or {}
    return {
        "so_y_te": first(clean(bk.get("THÔNG TIN BIỂU MẪU", {}).get("Sở Y tế")), clean(kb.get("THÔNG TIN BIỂU MẪU", {}).get("Sở Y tế"))),
        "benh_vien": first(clean(bk.get("THÔNG TIN BIỂU MẪU", {}).get("Cơ sở khám chữa bệnh")), clean(tt.get("THÔNG TIN BIỂU MẪU", {}).get("Bệnh viện"))),
        "benh_vien_viet_tat": clean(kb.get("THÔNG TIN BIỂU MẪU", {}).get("Bệnh viện")),
        "ma_cskcb": None,  # không in trên bộ phiếu này; cần cấu hình cơ sở (5 ký tự, QĐ BYT)
    }


def map_encounter(docs):
    kb = one(docs, "phieu-kham-benh-vao-vien") or {}
    tt = one(docs, "ban-tom-tat-ho-so-benh-an") or {}
    bk = one(docs, "bang-ke-chi-phi-kcb") or {}
    ck = one(docs, "phieu-cong-khai-dich-vu") or {}
    bkhc = bk.get("I. Phần hành chính", {})
    tthc = tt.get("HÀNH CHÍNH", {})
    vao_vien = first(clean(bkhc.get("Đến khám")), clean(ck.get("THÔNG TIN BỆNH NHÂN", {}).get("Ngày vào viện")), clean(kb.get("I. HÀNH CHÍNH", {}).get("Đến khám bệnh lúc")))
    vao_khoa = first(clean(bkhc.get("Điều trị từ")), clean(tthc.get("Vào viện ngày")))
    ra_vien = first(clean(bkhc.get("Kết thúc điều trị")), clean(tthc.get("Ra viện ngày")), clean(ck.get("THÔNG TIN BỆNH NHÂN", {}).get("Ngày ra viện")))
    khoa = first(clean(bk.get("THÔNG TIN BIỂU MẪU", {}).get("Khoa")), clean(kb.get("IV. KHÁM XÉT", {}).get("Cho vào điều trị tại khoa")), clean(tt.get("THÔNG TIN BIỂU MẪU", {}).get("Khoa")))
    tuyen = None
    for k, code in (("Đúng tuyến", "1"), ("Cấp cứu", "2"), ("Trái tuyến", "3"), ("Thông tuyến", "4")):
        if bkhc.get(k) is True:
            tuyen = {"text": k, "ma": code}
    phan_loai = kb.get("THÔNG TIN BIỂU MẪU", {}).get("Phân loại", {})
    return {
        "so_kham_benh": clean(bk.get("THÔNG TIN BIỂU MẪU", {}).get("Số khám bệnh")),
        "so_vao_vien": first(clean(kb.get("THÔNG TIN BIỂU MẪU", {}).get("Số vào viện")), *[clean(d["data"].get("THÔNG TIN BIỂU MẪU", {}).get("Số vào viện")) for d in docs.get("phieu-cham-soc", []) + docs.get("trich-bien-ban-hoi-chan", [])]),
        "loai_kcb": {"text": "Điều trị nội trú", "ma": "03"},
        "cap_cuu": bool(phan_loai.get("Cấp cứu")),
        "vao_vien": vao_vien, "vao_vien_iso": vn_datetime(vao_vien),
        "vao_khoa": {"khoa": khoa, "ma_khoa": clean(bk.get("THÔNG TIN BIỂU MẪU", {}).get("Mã khoa")), "thoi_gian": vao_khoa, "thoi_gian_iso": vn_datetime(vao_khoa)},
        "ra_vien": ra_vien, "ra_vien_iso": vn_datetime(ra_vien),
        "tong_so_ngay_dieu_tri": clean(bkhc.get("Tổng số ngày điều trị")),
        "ly_do_vao_vien": first(clean(kb.get("II. LÝ DO VÀO VIỆN")), clean(tt.get("TÓM TẮT QUÁ TRÌNH ĐIỀU TRỊ", {}).get("Lý do vào viện"))),
        "tuyen": tuyen,
        "phong_kham": clean(kb.get("THÔNG TIN BIỂU MẪU", {}).get("Phòng khám")),
    }


def map_coverage(docs):
    bk = one(docs, "bang-ke-chi-phi-kcb") or {}
    tt = one(docs, "ban-tom-tat-ho-so-benh-an") or {}
    kb = one(docs, "phieu-kham-benh-vao-vien") or {}
    bkhc = bk.get("I. Phần hành chính", {})
    the = first(clean(bkhc.get("Mã thẻ BHYT")), clean(tt.get("HÀNH CHÍNH", {}).get("Số thẻ BHYT")))
    the = re.sub(r"\s+", "", the) if the else None
    doi_tuong_box = kb.get("I. HÀNH CHÍNH", {}).get("Đối tượng", {})
    doi_tuong_phieu_kham = next((k for k, v in doi_tuong_box.items() if v), None)
    return {
        "doi_tuong": "BHYT" if the else doi_tuong_phieu_kham,
        "doi_tuong_phieu_kham": doi_tuong_phieu_kham,  # ô tích trên phiếu khám (có thể khác bảng kê)
        "so_the_bhyt": the,
        "ma_doi_tuong_the": the[:2] if the and the[:2].isalpha() else None,
        "muc_huong_bhyt": clean(bk.get("II. Phần chi phí", {}).get("Mức hưởng")),
        "bhyt_gia_tri_tu": clean(bkhc.get("Giá trị từ")),
        "bhyt_gia_tri_den": first(clean(bkhc.get("Giá trị đến")), clean(kb.get("I. HÀNH CHÍNH", {}).get("BHYT giá trị đến"))),
        "noi_đk_kcb_bhyt": {"ma": clean(bkhc.get("Mã nơi ĐK KCB ban đầu")), "text": clean(bkhc.get("Nơi ĐK KCB BHYT ban đầu"))},
    }


def map_location(docs, enc):
    cs = docs.get("phieu-cham-soc", [])
    dt = docs.get("phieu-dieu-tri", [])
    giuong = first(*[clean(d["data"].get("THÔNG TIN BỆNH NHÂN", {}).get("Giường")) for d in cs],
                   *[clean(d["data"].get("THÔNG TIN BỆNH NHÂN", {}).get("Giường")) for d in dt])
    phong = first(*[clean(d["data"].get("THÔNG TIN BỆNH NHÂN", {}).get("Phòng")) for d in cs])
    return {"khoa": enc["vao_khoa"]["khoa"], "ma_khoa": enc["vao_khoa"]["ma_khoa"], "phong": phong, "giuong": giuong}


def map_practitioner(docs):
    kb = one(docs, "phieu-kham-benh-vao-vien") or {}
    hc = one(docs, "trich-bien-ban-hoi-chan") or {}
    tt = one(docs, "ban-tom-tat-ho-so-benh-an") or {}
    dts = docs.get("phieu-dieu-tri", [])
    bs_dt = first(*[clean(r.get("BÁC SỸ ĐIỀU TRỊ", {}).get("Họ tên")) for d in dts for r in d["data"].get("Diễn biến điều trị", [])])
    out = {
        "bac_si_kham_benh": parse_person(kb.get("XÁC NHẬN KÝ", {}).get("Họ tên") or ""),
        "bac_si_dieu_tri": parse_person(bs_dt or ""),
        "truong_khoa": parse_person(hc.get("THÔNG TIN BỆNH NHÂN", {}).get("Chủ tọa") or ""),
        "thu_ky_hoi_chan": parse_person(hc.get("THÔNG TIN BỆNH NHÂN", {}).get("Thư ký") or ""),
        "thanh_vien_hoi_chan": [parse_person(x) for x in re.split(r"[;\n]", hc.get("THÔNG TIN BỆNH NHÂN", {}).get("Thành viên tham gia") or "") if x.strip()],
        "nguoi_ky_tom_tat": parse_person(tt.get("XÁC NHẬN", {}).get("Họ tên") or ""),
        "bac_si_lam_benh_an": None,
        "nguoi_tra_ket_qua": {},
    }
    for form, key in (("ket-qua-xet-nghiem-huyet-hoc", "huyet_hoc"), ("ket-qua-xet-nghiem-sinh-hoa-mau", "sinh_hoa_mau"), ("phieu-xet-nghiem-nuoc-dich", "nuoc_tieu")):
        d = one(docs, form)
        if d:
            signer = next((s for s in d.get("XÁC NHẬN KÝ", []) if "TRẢ" in (s.get("Vai trò") or "").upper()), None) or (d.get("XÁC NHẬN KÝ") or [{}])[-1]
            out["nguoi_tra_ket_qua"][key] = parse_person(signer.get("Họ tên") or "")
    for form in ("phieu-chup-cong-huong-tu", "phieu-sieu-am-doppler", "phieu-dien-nao-do"):
        for d in docs.get(form, []):
            signers = d["data"].get("XÁC NHẬN KÝ") or []
            if isinstance(d["data"].get("XÁC NHẬN"), dict):  # điện não
                signers = [{"Vai trò": "BS CHUYÊN KHOA", "Họ tên": d["data"]["XÁC NHẬN"].get("Bác sĩ chuyên khoa", {}).get("Họ tên")}]
            for s in signers:
                if "CHUYÊN KHOA" in (s.get("Vai trò") or "").upper() and s.get("Họ tên"):
                    out.setdefault("bac_si_chuyen_khoa_cdha", []).append({**parse_person(s["Họ tên"]), "phieu": d["file"]})
    out["bac_si_lam_benh_an"] = out["bac_si_dieu_tri"]
    return out


def map_document_context(docs, enc, dx):
    tt = one(docs, "ban-tom-tat-ho-so-benh-an") or {}
    bk = one(docs, "bang-ke-chi-phi-kcb") or {}
    dt = one(docs, "phieu-dieu-tri") or {}
    tq = tt.get("TÓM TẮT QUÁ TRÌNH ĐIỀU TRỊ", {})
    return {
        "ten_bieu_mau": "BỆNH ÁN NGOẠI KHOA" if "Ngoại" in (enc["vao_khoa"]["khoa"] or "") else "BỆNH ÁN NỘI KHOA",
        "ma_benh_an": first(clean(tt.get("THÔNG TIN BIỂU MẪU", {}).get("Mã bệnh án")), clean(dt.get("THÔNG TIN BIỂU MẪU", {}).get("Mã bệnh án"))),
        "so_luu_tru": clean(bk.get("THÔNG TIN BIỂU MẪU", {}).get("Sổ lưu trữ")),
        "so_kham_benh": enc["so_kham_benh"],
        "ma_y_te": enc.get("so_vao_vien"),
        "chan_doan": clean(tt.get("CHẨN ĐOÁN", {}).get("Chẩn đoán ra viện")),
        "ly_do_vao_vien": enc["ly_do_vao_vien"],
        "tom_tat_benh_ly": clean(tq.get("Tóm tắt quá trình bệnh lý và diễn biến lâm sàng")),
        "dau_hieu_lam_sang_chinh": clean(tq.get("Những dấu hiệu lâm sàng chính")),
        "ket_qua_can_lam_sang": clean(tq.get("Tóm tắt kết quả xét nghiệm/cận lâm sàng")),
        "phuong_phap_dieu_tri": clean(tq.get("Phương pháp điều trị")),
        "tinh_trang_ra_vien": clean(tq.get("TÌNH TRẠNG RA VIỆN")),
        "huong_dieu_tri_tiep_theo": clean(tq.get("Hướng điều trị và các chế độ tiếp theo")),
        "ngay_lam_benh_an": enc["vao_vien"],
        "ngay_ky_tom_tat": clean(tt.get("XÁC NHẬN", {}).get("Ngày giờ")),
    }


OUTCOME_MAP = [  # TÌNH TRẠNG RA VIỆN (chữ) -> vn-treatment-outcome-cs
    (r"khỏi", "1", "Khỏi"), (r"đỡ|giảm", "2", "Đỡ"), (r"không thay đổi|không đổi", "3", "Không thay đổi"),
    (r"nặng hơn", "4", "Nặng hơn"), (r"tử vong", "5", "Tử vong tại cơ sở KBCB"), (r"tiên lượng nặng", "6", "Tiên lượng nặng xin về"),
]
DISPOSITION_MAP = [  # hướng điều trị (chữ) -> vn-discharge-disposition-cs
    (r"xin (ra )?về|xin ra viện|cam kết xin về", "4", "Xin ra viện"), (r"trốn viện", "3", "Trốn viện"),
    (r"chuyển (tuyến|viện).*yêu cầu|theo yêu cầu", "5", "Chuyển tuyến/Chuyển cơ sở KBCB theo yêu cầu người bệnh"),
    (r"chuyển (tuyến|viện)", "2", "Chuyển tuyến/Chuyển cơ sở KBCB theo yêu cầu chuyên môn"), (r"ra viện", "1", "Ra viện"),
]


def map_discharge(docs, ctx):
    bk = one(docs, "bang-ke-chi-phi-kcb") or {}
    code_bk = clean(bk.get("I. Phần hành chính", {}).get("Tình trạng ra viện"))
    kq = ctx["tinh_trang_ra_vien"] or ""
    outcome = next(((c, d) for p, c, d in OUTCOME_MAP if re.search(p, kq, re.I)), (None, None))
    if code_bk and code_bk != outcome[0]:
        outcome = (code_bk, dict((c, d) for _, c, d in OUTCOME_MAP).get(code_bk))
    hd = ctx["huong_dieu_tri_tiep_theo"] or ""
    dispo = next(((c, d) for p, c, d in DISPOSITION_MAP if re.search(p, hd, re.I)), ("1", "Ra viện"))
    return {"ket_qua_dieu_tri": ctx["tinh_trang_ra_vien"], "ma_ket_qua_dieu_tri": outcome[0], "ten_ket_qua_dieu_tri": outcome[1],
            "huong_dieu_tri": ctx["huong_dieu_tri_tiep_theo"], "ma_hinh_thuc_ra_vien": dispo[0], "ten_hinh_thuc_ra_vien": dispo[1]}


def map_clinical_finding(docs):
    kb = one(docs, "phieu-kham-benh-vao-vien") or {}
    tt = one(docs, "ban-tom-tat-ho-so-benh-an") or {}
    kx = kb.get("IV. KHÁM XÉT", {})
    lines = [clean(x) for x in (kx.get("Các bộ phận") or []) if clean(x)]
    buckets = {"tuan_hoan": r"tim|mạch", "ho_hap": r"phổi|rales|thở", "tieu_hoa": r"bụng|gan|lách", "than_tiet_nieu_sinh_duc": r"thận|tiểu",
               "than_kinh": r"đồng tử|liệt|tkkt|hctđ|glasgow|g15", "co_xuong_khop": r"cột sống|khớp|chi"}
    out = {k: None for k in buckets}
    other = []
    for ln in lines:
        hit = next((k for k, p in buckets.items() if re.search(p, ln, re.I)), None)
        if hit:
            out[hit] = (out[hit] + " " if out[hit] else "") + ln
        else:
            other.append(ln)
    out["kham_toan_than"] = " ".join(clean(x) for x in (kx.get("Toàn thân") or []) if clean(x)) or None
    out["trieu_chung_co_nang"] = other or None
    out["kham_ngoai_khoa"] = clean(tt.get("TÓM TẮT QUÁ TRÌNH ĐIỀU TRỊ", {}).get("Những dấu hiệu lâm sàng chính"))
    return out


def map_histories(docs):
    kb = one(docs, "phieu-kham-benh-vao-vien") or {}
    tt = one(docs, "ban-tom-tat-ho-so-benh-an") or {}
    hb = kb.get("III. HỎI BỆNH", {})
    ts = hb.get("Tiền sử bệnh", {})
    return ({"qua_trinh_benh_ly": first(clean(hb.get("Quá trình bệnh lý")), clean(tt.get("TÓM TẮT QUÁ TRÌNH ĐIỀU TRỊ", {}).get("Tóm tắt quá trình bệnh lý và diễn biến lâm sàng")))},
            {"tien_su_ban_than": clean(ts.get("Bản thân")), "tien_su_benh_tom_tat": clean(tt.get("TÓM TẮT QUÁ TRÌNH ĐIỀU TRỊ", {}).get("Tiền sử bệnh"))},
            {"tien_su_gia_dinh": clean(ts.get("Gia đình"))})


# ---------------------------------------------------------------- sinh hiệu / observations
def vital_obs(key, raw_value, time_text, source, extra_time=None):
    """key in VITALS; raw_value '120/70 mmHg' | '80 lần/phút' | '37' ..."""
    if raw_value in (None, ""):
        return []
    loinc, disp_en, disp_vi, ucum_code, unit, profile = VITALS[key]
    s = nfc(str(raw_value)).strip()
    t_iso = vn_datetime(time_text) or extra_time
    base = {"type": "sign", "code": loinc, "display": disp_vi, "display_en": disp_en, "codesystem": SYS["loinc"], "version": VERSION,
            "assertion": [], "profile": profile, "source": source, "time": [time_text] if time_text else [], "time_iso": t_iso}
    if key == "huyet_ap":
        m = re.search(r"(\d{2,3})\s*/\s*(\d{2,3})", s)
        if not m:
            return []
        return [{**base, "text": f"huyet_ap: {s}", "ucum": {"key": "sinh_hieu", "unit": "mmHg", "code": "mm[Hg]", "system": SYS["ucum"],
                                                            "components": {"systolic": int(m.group(1)), "diastolic": int(m.group(2))}}, "position": pos(s)}]
    num = re.search(r"-?\d+(?:[\.,]\d+)?", s)
    if not num:
        return []
    val = float(num.group(0).replace(",", "."))
    val = int(val) if val.is_integer() else val
    return [{**base, "text": f"{key}: {s}", "ucum": {"key": "sinh_hieu", "unit": unit, "code": ucum_code, "system": SYS["ucum"], "value": val}, "position": pos(s)}]


VITAL_LINE_RE = re.compile(r"(Mạch|Nhiệt độ|Huyết áp|Nhịp thở|SpO2|Cân nặng|Chiều cao|BMI)\s*:\s*([0-9][0-9\./,]*\s*[^\s:]*)", re.I)


def map_observations(docs, enc):
    obs = []
    # 1. phiếu khám bệnh vào viện
    kb = one(docs, "phieu-kham-benh-vao-vien") or {}
    sh = kb.get("IV. KHÁM XÉT", {}).get("Sinh hiệu", {})
    t_kb = kb.get("I. HÀNH CHÍNH", {}).get("Đến khám bệnh lúc") or enc["vao_vien"]
    for label, key in VITAL_ALIASES.items():
        v = next((v for k, v in sh.items() if norm(k) == label), None)
        obs += vital_obs(key, v, t_kb, "phieu-kham-benh-vao-vien")
    # 2. phiếu điều trị (mỗi dòng diễn biến có dòng sinh hiệu)
    for d in docs.get("phieu-dieu-tri", []):
        for row in d["data"].get("Diễn biến điều trị", []):
            t = row.get("Ngày giờ")
            text = row.get("Diễn biến bệnh") or ""
            for m in VITAL_LINE_RE.finditer(text):
                key = VITAL_ALIASES.get(norm(m.group(1)))
                if key:
                    obs += vital_obs(key, m.group(2).strip(), t, f"phieu-dieu-tri#{d['seq']}")
    # 3. phiếu chăm sóc (mỗi cột thời điểm)
    for d in docs.get("phieu-cham-soc", []):
        for col in d["data"].get("Theo dõi chăm sóc", []):
            t = f"{col.get('Giờ') or ''} {col.get('Ngày') or ''}".strip()
            t_clean = re.sub(r"\s*([/:])\s*", r"\1", t)
            cs = col.get("Chỉ số sinh tồn, sinh trắc", {}) or {}
            for label, key in VITAL_ALIASES.items():
                v = next((v for k, v in cs.items() if norm(k) == label), None)
                obs += vital_obs(key, v, t_clean, f"phieu-cham-soc#{d['seq']}")
            # Glasgow, tri giác, da niêm mạc -> symptom/sign dạng text (không LOINC bắt buộc)
            tb = col.get("Toàn thân", {}) or {}
            if tb.get("Tri giác"):
                obs.append({"type": "sign", "text": f"tri_giac: {tb['Tri giác']}", "code": None, "display": "Tri giác", "codesystem": None, "version": VERSION,
                            "assertion": [], "ucum": {"key": "toan_than", "value_text": tb["Tri giác"]}, "position": [0, 0], "time": [t_clean], "time_iso": vn_datetime(t_clean), "source": f"phieu-cham-soc#{d['seq']}"})
    # 4. triệu chứng (symptom) từ khám bộ phận - giữ text
    for ln in (kb.get("IV. KHÁM XÉT", {}).get("Các bộ phận") or []):
        obs.append({"type": "symptom", "text": clean(ln), "code": None, "display": None, "codesystem": None, "version": VERSION, "assertion": [],
                    "position": [0, len(ln)], "time": [t_kb], "time_iso": vn_datetime(t_kb), "source": "phieu-kham-benh-vao-vien"})
    return obs


# ---------------------------------------------------------------- chẩn đoán
def split_dx(text):
    if not text:
        return []
    t = nfc(text).strip().rstrip(";")
    parts = re.split(r"\s*[;/]\s*|\s*\bTD\b\s*", t)
    return [p.strip(" .;") for p in parts if p and p.strip(" .;")]


def map_diagnoses(docs):
    kb = one(docs, "phieu-kham-benh-vao-vien") or {}
    tt = one(docs, "ban-tom-tat-ho-so-benh-an") or {}
    bk = one(docs, "bang-ke-chi-phi-kcb") or {}
    hc = one(docs, "trich-bien-ban-hoi-chan") or {}
    items = []
    def add(text, typ, source, code=None, rank=None, raw=None):
        m = ICD_RE.search(text)
        c = code or (m.group(1) if m else None)
        items.append({"text": text, "type": typ, "source": source, "code": c, "rank": rank, "raw": raw or text})
    ad_raw = first(clean(tt.get("CHẨN ĐOÁN", {}).get("Chẩn đoán vào viện")), clean(kb.get("IV. KHÁM XÉT", {}).get("Chẩn đoán vào viện")))
    for i, p in enumerate(split_dx(ad_raw), 1):
        add(p, "AD", "ban-tom-tat/phieu-kham", rank=str(i), raw=ad_raw)
    dd_raw = clean(tt.get("CHẨN ĐOÁN", {}).get("Chẩn đoán ra viện"))
    bkhc = bk.get("I. Phần hành chính", {})
    bk_main = (clean(bkhc.get("Chẩn đoán xác định")), clean(bkhc.get("Mã bệnh ICD-10")))
    bk_sec = (clean(bkhc.get("Bệnh kèm theo")), clean(bkhc.get("Mã bệnh kèm theo")))
    # Chẩn đoán ra viện: ưu tiên cách HIS đã mã hoá trên bảng kê (mỗi dòng ';' là một chẩn đoán, có thể chứa '/').
    dd_parts = []
    for part in re.split(r"\s*;\s*", dd_raw or ""):
        part = part.strip(" .")
        if not part:
            continue
        code = None
        if bk_main[0] and norm(part) == norm(bk_main[0]):
            code = bk_main[1]
        elif bk_sec[0] and norm(part) == norm(bk_sec[0]):
            code = bk_sec[1]
        if code:
            dd_parts.append((part, code))
        else:  # không khớp bảng kê -> tách tiếp theo '/' và để LLM mã hoá
            dd_parts += [(x, None) for x in split_dx(part)]
    for i, (p, code) in enumerate(dd_parts, 1):
        add(p, "DD", "ban-tom-tat+bang-ke" if code else "ban-tom-tat", code=code, rank=str(i), raw=dd_raw)
    if bk_main[0] and not any(norm(bk_main[0]) == norm(x["text"]) for x in items if x["type"] == "DD"):
        add(bk_main[0], "DD", "bang-ke", code=bk_main[1], rank="1")
    if bk_sec[0] and not any(norm(bk_sec[0]) == norm(x["text"]) for x in items if x["type"] == "DD"):
        add(bk_sec[0], "DD", "bang-ke", code=bk_sec[1], rank="2")
    # chẩn đoán hội chẩn (trung gian): type 'CM' (comorbidity? dùng 'AD' bổ sung) -> giữ như ghi chú
    kl = clean(hc.get("Kết luận (Sau khi đã khám lại và thảo luận)"))
    if kl:
        for i, p in enumerate(split_dx(kl), 1):
            if not any(norm(p) == norm(x["text"]) for x in items):
                add(p, "AD", "trich-bien-ban-hoi-chan", rank=str(len([x for x in items if x['type']=='AD']) + 1))
    return items


# ---------------------------------------------------------------- xét nghiệm
def map_labs(docs):
    results, reports = [], {}
    specs = (("ket-qua-xet-nghiem-huyet-hoc", "huyet_hoc", "01"), ("ket-qua-xet-nghiem-sinh-hoa-mau", "sinh_hoa_mau", "02"), ("phieu-xet-nghiem-nuoc-dich", "nuoc_tieu", "02"))
    for form, key, appendix in specs:
        for d in docs.get(form, []):
            data = d["data"]
            mau = data.get("THÔNG TIN MẪU", {}) or {}
            th = data.get("THÔNG TIN THỰC HIỆN", {}) or {}
            signers = data.get("XÁC NHẬN KÝ") or []
            tra = next((s for s in signers if "TRẢ" in (s.get("Vai trò") or "").upper()), {})
            t_kq = first(clean(tra.get("Thời gian")), clean(th.get("Thời gian thực hiện")), clean(mau.get("Thời gian nhận mẫu")))
            t_mau = first(clean(mau.get("Thời gian lấy mẫu")), clean(th.get("Thời gian lấy mẫu")), clean(mau.get("Thời gian nhận mẫu")), clean(th.get("Thời gian thực hiện")))
            reports[key] = {
                "file": d["file"], "ma_benh_pham": clean(data.get("THÔNG TIN BIỂU MẪU", {}).get("Mã bệnh phẩm")),
                "loai_mau": clean(mau.get("Loại mẫu")) or ("Nước tiểu" if key == "nuoc_tieu" else "Máu"),
                "tinh_trang_mau": clean(mau.get("Tình trạng mẫu")),
                "nguoi_lay_mau": first(clean(mau.get("Người lấy mẫu")), clean(th.get("Người lấy mẫu"))),
                "nguoi_nhan_mau": clean(mau.get("Người nhận mẫu")), "nguoi_thuc_hien": clean(th.get("Người thực hiện")),
                "thoi_gian_chi_dinh": clean(th.get("Thời gian chỉ định")), "thoi_gian_lay_mau": first(clean(mau.get("Thời gian lấy mẫu")), clean(th.get("Thời gian lấy mẫu"))),
                "thoi_gian_nhan_mau": clean(mau.get("Thời gian nhận mẫu")), "thoi_gian_thuc_hien": clean(th.get("Thời gian thực hiện")),
                "thoi_gian_tra_kq": clean(tra.get("Thời gian")), "bs_tra_kq": clean(tra.get("Họ tên")),
                "bs_dieu_tri": clean(next((s.get("Họ tên") for s in signers if "ĐIỀU TRỊ" in (s.get("Vai trò") or "").upper()), None)),
                "khoa_phong": clean(data.get("THÔNG TIN BỆNH NHÂN", {}).get("Khoa phòng")), "chan_doan": clean(data.get("THÔNG TIN BỆNH NHÂN", {}).get("Chẩn đoán")),
                "nhan_xet": clean(data.get("Nhận xét")), "thoi_gian_iso": vn_datetime(t_kq), "thoi_gian_mau_iso": vn_datetime(t_mau),
                "panels": [],
            }
            for grp in data.get("KẾT QUẢ XÉT NGHIỆM", []) or []:
                gname = clean(grp.get("Tên nhóm xét nghiệm")) or clean(grp.get("Tên xét nghiệm")) or ""
                panel = next((v for k, v in LAB_PANELS.items() if k in norm(gname)), None)
                reports[key]["panels"].append({"ten": gname, "loinc": panel[0] if panel else None, "display": panel[2] if panel else gname, "cls": panel[3] if panel else None})
                for row in grp.get("Chi tiết", []) or []:
                    name = clean(row.get("Tên xét nghiệm"))
                    if not name:
                        continue
                    val_raw = clean(row.get("KẾT QUẢ"))
                    unit_raw = first(clean(row.get("Đơn vị tính")), clean(row.get("Đơn vị")))
                    ref = first(clean(row.get("Giá trị BT")), clean(row.get("Giá trị bình thường")))
                    hit = lab_loinc(name, gname, unit_raw)
                    uc, ulabel = ucum(unit_raw)
                    num = to_number(val_raw)
                    rr = parse_ref_range(ref)
                    results.append({
                        "code": hit[0] if hit else None, "display": hit[2] if hit else None, "display_en": hit[1] if hit else None,
                        "codesystem": SYS["loinc"], "version": VERSION, "code_source": hit[3] if hit else None, "assertion": [],
                        "original_text": val_raw, "ten_xet_nghiem": name, "nhom": gname, "panel_loinc": panel[0] if panel else None,
                        "ucum": {"key": name, "value": num, "value_text": None if num is not None else val_raw, "unit": ulabel or unit_raw, "unit_raw": unit_raw, "code": uc, "system": SYS["ucum"] if uc else None},
                        "tham_chiếu": ref, "ref_low": rr[0] if rr else None, "ref_high": rr[1] if rr else None,
                        "đánh_dấu": clean(row.get("Đánh dấu")), "ghi_chu": clean(row.get("Ghi chú")), "quy_trinh": clean(row.get("Quy trình")),
                        "position": [0, len(val_raw or "")], "time": [t_kq] if t_kq else [], "time_iso": vn_datetime(t_kq), "report": key, "source": d["file"],
                    })
    return results, reports


# ---------------------------------------------------------------- CĐHA / TDCN
def map_diagnostic_reports(docs):
    reps = []
    for form, cat in (("phieu-chup-cong-huong-tu", "CDHA"), ("phieu-sieu-am-doppler", "CDHA"), ("phieu-dien-nao-do", "TDCN")):
        for d in docs.get(form, []):
            data = d["data"]
            bm, bn = data.get("THÔNG TIN BIỂU MẪU", {}), data.get("THÔNG TIN BỆNH NHÂN", {})
            kq = data.get("KẾT QUẢ") or data.get("KẾT QUẢ ĐIỆN NÃO") or {}
            name = first(clean(bn.get("Yêu cầu kiểm tra")), clean(bm.get("Tên kỹ thuật")), clean(kq.get("1. Kỹ thuật")), clean(bm.get("Tên phiếu")))
            if form == "phieu-dien-nao-do":
                name = "Ghi điện não đồ thông thường"
            signers = data.get("XÁC NHẬN KÝ") or []
            if isinstance(data.get("XÁC NHẬN"), dict):
                x = data["XÁC NHẬN"]
                signers = [{"Vai trò": "BÁC SĨ ĐIỀU TRỊ", "Thời gian": x.get("Thời gian"), "Họ tên": x.get("Bác sĩ điều trị", {}).get("Họ tên")},
                           {"Vai trò": "BS CHUYÊN KHOA", "Thời gian": x.get("Thời gian"), "Họ tên": x.get("Bác sĩ chuyên khoa", {}).get("Họ tên")}]
            ck = next((s for s in signers if "CHUYÊN KHOA" in (s.get("Vai trò") or "").upper()), {})
            dt = next((s for s in signers if "ĐIỀU TRỊ" in (s.get("Vai trò") or "").upper()), {})
            t = first(clean(ck.get("Thời gian")), clean(dt.get("Thời gian")))
            mo_ta = kq.get("Mô tả") or []
            if mo_ta and isinstance(mo_ta[0], dict):
                mo_ta = [f"{m.get('Vị trí')}: " + " ".join(m.get("Nhận xét") or []) for m in mo_ta]
            ket_luan = first(clean(kq.get("2. Kết luận")), clean(kq.get("Kết luận")))
            hit = imaging_loinc(name or "")
            reps.append({
                "code": hit[0] if hit else None, "display": hit[1] if hit else None, "cls": hit[2] if hit else None, "code_source": hit[3] if hit else None,
                "codesystem": SYS["loinc"], "version": VERSION, "assertion": [], "category": cat,
                "ucum": {"key": name}, "ky_thuat": clean(kq.get("1. Kỹ thuật")), "mo_ta": mo_ta, "original_text": ket_luan,
                "position": [0, len(ket_luan or "")], "time": [t] if t else [], "time_iso": vn_datetime(t),
                "bac_si_chuyen_khoa": clean(ck.get("Họ tên")), "bac_si_dieu_tri": clean(dt.get("Họ tên")),
                "chan_doan_chi_dinh": clean(bn.get("Chẩn đoán")), "khoa": clean(bn.get("Khoa")), "ma_so_phieu": clean(bm.get("Mã số")), "source": d["file"],
            })
    return reps


# ---------------------------------------------------------------- y lệnh: thuốc & chỉ định
MED_RE = re.compile(r"^-\s*(?P<name>.+?)\s*[\.]?\s*x\s*(?P<qty>\d+(?:[\.,]\d+)?)\s*(?P<unit>\S+)\s*$", re.M)


def map_orders(docs):
    meds, srs, diets = [], [], []
    for d in docs.get("phieu-dieu-tri", []):
        for row in d["data"].get("Diễn biến điều trị", []):
            t = row.get("Ngày giờ")
            bs = clean(row.get("BÁC SỸ ĐIỀU TRỊ", {}).get("Họ tên"))
            # HIS in các khối [Phiếu CĐHA:]/[Phiếu xét nghiệm:] lẫn trong 'Diễn biến bệnh', đơn thuốc trong 'Y lệnh'
            y = (row.get("Diễn biến bệnh") or "") + "\n" + (row.get("Y lệnh") or "")
            blocks = re.split(r"(?=\[[^\]]+\])", y)
            for b in blocks:
                m = re.match(r"\[([^\]]+)\]\s*(.*)", b.strip(), re.S)
                if not m:
                    continue
                label, body = m.group(1).strip(" :"), m.group(2).strip()
                if label.startswith("Đơn thuốc"):
                    dm = re.search(r"Ngày sử dụng\s*(\S+)", body)
                    ngay = dm.group(1) if dm else None
                    body2 = re.sub(r"Ngày sử dụng\s*\S+", "", body).strip()
                    # mỗi thuốc: '- Tên (thành phần). x 02 Viên\nUống, 8h34 16h'
                    chunks = re.split(r"\n(?=-\s)", body2)
                    for ch in chunks:
                        lines = [l.strip() for l in ch.strip().split("\n") if l.strip()]
                        if not lines:
                            continue
                        head = lines[0]
                        mm = re.match(r"-\s*(?P<name>.+?)\s*x\s*(?P<qty>\d+(?:[\.,]\d+)?)\s*(?P<unit>\S+)\s*$", head)
                        name = mm.group("name").strip(" .") if mm else head.lstrip("- ").strip()
                        cach_dung = " ".join(lines[1:]) if len(lines) > 1 else None
                        route = None
                        if cach_dung:
                            route = cach_dung.split(",")[0].strip()
                        comp = re.search(r"\((.+)\)", name)
                        meds.append({"original_text": name, "ten_thuoc": re.sub(r"\s*\(.+\)", "", name).strip(" ."), "thanh_phan": comp.group(1) if comp else None,
                                     "so_luong": to_number(mm.group("qty").replace(",", ".")) if mm else None, "don_vi": mm.group("unit") if mm else None,
                                     "cach_dung": cach_dung, "duong_dung": route, "dosage": f"{mm.group('qty')} {mm.group('unit')}\n{cach_dung}" if mm and cach_dung else cach_dung,
                                     "ngay_su_dung": ngay, "ngay_su_dung_iso": vn_datetime(ngay), "ngay_y_lenh": t, "ngay_y_lenh_iso": vn_datetime(t), "bac_si": bs,
                                     "code": None, "display": None, "codesystem": SYS["atc"], "version": VERSION, "assertion": [], "position": [0, len(name)], "source": f"phieu-dieu-tri#{d['seq']}"})
                elif label.startswith(("Phiếu CĐHA", "Phiếu xét nghiệm", "Phiếu TDCN")):
                    dm = re.match(r"(\S+)\s*(.*)", body, re.S)
                    ngay, rest = (dm.group(1), dm.group(2)) if dm else (None, body)
                    cat = "XN" if "xét nghiệm" in label.lower() else "CDHA"
                    rest = re.sub(r"\s*\n\s*", " ", rest)
                    names = [x.strip() for x in re.split(r",(?![^\(]*\))", rest) if x.strip()] if cat == "XN" else [rest.strip()]
                    for n in names:
                        srs.append({"ten_dich_vu": n, "loai": cat, "ngay": ngay, "ngay_iso": vn_datetime(ngay), "ngay_y_lenh": t, "ngay_y_lenh_iso": vn_datetime(t), "bac_si": bs, "source": f"phieu-dieu-tri#{d['seq']}"})
                elif label.startswith(("Chế độ ăn", "Chế độ chăm sóc")):
                    diets.append({"loai": label, "noi_dung": body, "ngay_y_lenh": t})
    return meds, srs, diets


# ---------------------------------------------------------------- bảng kê chi phí -> claim
def map_claim(docs, org):
    bk = one(docs, "bang-ke-chi-phi-kcb") or {}
    cp = bk.get("II. Phần chi phí", {}) or {}
    items = []
    for muc in cp.get("Mục", []) or []:
        tm = muc.get("Tổng mục", {}) or {}
        chi_tiet = []
        for r in muc.get("Chi tiết", []) or []:
            chi_tiet.append({"noi_dung": clean(r.get("Nội dung")), "don_vi_tinh": clean(r.get("Đơn vị tính")), "so_luong": to_number(r.get("Số lượng")),
                             "don_gia_bv": to_number(r.get("Đơn giá BV")), "don_gia_bh": to_number(r.get("Đơn giá BH")),
                             "ty_le_thanh_toan_dich_vu_pct": to_number(r.get("Tỷ lệ thanh toán theo dịch vụ")), "thanh_tien_bv": to_number(r.get("Thành tiền BV")),
                             "ty_le_thanh_toan_bhyt_pct": to_number(r.get("Tỷ lệ thanh toán BHYT")), "thanh_tien_bh": to_number(r.get("Thành tiền BH")),
                             "quy_bhyt": to_number(r.get("Quỹ BHYT")), "nguoi_benh_cung_chi_tra": to_number(r.get("Người bệnh cùng chi trả")),
                             "khac": to_number(r.get("Khác")), "nguoi_benh_tu_tra": to_number(r.get("Người bệnh tự trả"))})
        mcode = re.match(r"(\d+(?:\.\d+)?)", (muc.get("Mục con") or muc.get("Mục") or "").strip())
        items.append({"muc": clean(muc.get("Mục")), "muc_con": clean(muc.get("Mục con")), "ma_nhom_chi_phi": mcode.group(1).rstrip(".") if mcode else None,
                      "chi_tiet": chi_tiet,
                      "tong_muc": {k: to_number(tm.get(src)) for k, src in (("thanh_tien_bv", "Thành tiền BV"), ("thanh_tien_bh", "Thành tiền BH"), ("quy_bhyt", "Quỹ BHYT"), ("nguoi_benh_cung_chi_tra", "Người bệnh cùng chi trả"), ("khac", "Khác"), ("nguoi_benh_tu_tra", "Người bệnh tự trả"))}})
    tc = cp.get("Tổng cộng", {}) or {}
    money = lambda v: {"gia_tri": str(int(v)) if v is not None else None, "currency": "VND"}
    return {"co_so_kcb": org["benh_vien"], "item": [items], "chi_phi_tu_ngay": clean(cp.get("Chi phí tính từ ngày")), "chi_phi_den_ngay": clean(cp.get("Chi phí tính đến ngày")),
            "tong_chi_phi": money(to_number(tc.get("Tổng chi phí KBCB"))), "quy_bhyt_thanh_toan": money(to_number(tc.get("Quỹ BHYT thanh toán"))),
            "nguoi_benh_tra": {"cung_tra": money(to_number(tc.get("Người bệnh cùng chi trả"))), "khoan_khac": money(to_number(tc.get("Người bệnh tự trả")))},
            "nguon_khac": money(to_number(tc.get("Nguồn khác"))), "bang_chu": clean(tc.get("Tổng cộng bằng chữ")),
            "ngay_lap": clean(next((s.get("Thời gian") for s in bk.get("XÁC NHẬN KÝ", []) if s.get("Thời gian")), None))}


def map_document_reference(man, raw_url_prefix=None):
    """raw_url_prefix: tiền tố URL/đường dẫn tới thư mục 00_raw (mặc định output/<case>/00_raw như bước làm tay)."""
    prefix = raw_url_prefix or f"output/{man['case']}/00_raw"
    pdfs = []
    for d in man["documents"]:
        pdfs.append({"document_id": Path(d["file"]).stem, "title": Path(d["file"]).stem, "file_name": d["file"], "form_code": d["form"],
                     "skipped": d["form"].startswith("skip:"), "pages": d["pages"], "size_bytes": d["size_bytes"], "content_type": "application/pdf",
                     "hash_sha1_base64": d["sha1_b64"], "storage_url": f"{prefix}/{d['file']}", "document_type": Path(d["file"]).stem})
    return {"pdf": pdfs, "docx": []}


# ---------------------------------------------------------------- mã hoá bằng LLM cho phần còn thiếu
CODING_SYSTEM = """Bạn là chuyên gia mã hoá y tế Việt Nam (ICD-10 bản Việt Nam theo QĐ 4469/QĐ-BYT, LOINC, ATC/WHO).
Trả về DUY NHẤT JSON: {"items":[{"id":..., "code":..., "display_vi":..., "display_en":..., "confidence": 0-1}]}.
- kind=icd10: mã ICD-10 phù hợp nhất với chẩn đoán tiếng Việt (ưu tiên mã 4 ký tự khi có; chẩn đoán triệu chứng dùng chương R).
- kind=loinc_lab: mã LOINC cho chỉ số xét nghiệm (xem đơn vị để chọn Mass/Moles; test strip nước tiểu dùng mã 'by Test strip').
- kind=loinc_imaging: mã LOINC cho kỹ thuật CĐHA/thăm dò chức năng.
- kind=atc: mã ATC (WHO) cho thuốc; thuốc phối hợp vitamin B1+B6+B12 -> A11DB.
- Không chắc thì vẫn trả mã gần nhất nhưng confidence thấp."""


def llm_coding(items, llm, max_tokens=8000):
    if not items:
        return {}
    res = llm.complete_json(CODING_SYSTEM, "Cần mã hoá:\n" + json.dumps(items, ensure_ascii=False, indent=1), max_tokens=max_tokens)
    return {str(x.get("id")): x for x in res.get("items", [])}


def map_consultation(docs):
    """Trích biên bản hội chẩn -> dữ liệu cho ClinicalImpression (không để bước 3 phải đọc lại phiếu)."""
    hc = one(docs, "trich-bien-ban-hoi-chan")
    if not hc:
        return None
    bn = hc.get("THÔNG TIN BỆNH NHÂN", {}) or {}
    return {"thoi_gian": clean(bn.get("Hội chẩn lúc")), "thoi_gian_iso": vn_datetime(bn.get("Hội chẩn lúc")),
            "chu_toa": clean(bn.get("Chủ tọa")), "thu_ky": clean(bn.get("Thư ký")),
            "thanh_vien": [x.strip() for x in re.split(r"[;\n]", bn.get("Thành viên tham gia") or "") if x.strip()],
            "tom_tat": [clean(x) for x in (hc.get("Tóm tắt quá trình diễn biến bệnh, quá trình điều trị và chăm sóc người bệnh") or []) if clean(x)],
            "ket_luan": clean(hc.get("Kết luận (Sau khi đã khám lại và thảo luận)")), "huong_dieu_tri": clean(hc.get("Hướng điều trị tiếp"))}


def map_allergies(docs):
    """Phiếu chăm sóc: ô 'Tiền sử dị ứng' -> [{'chua_ghi_nhan': bool, 'noi_dung': str|None}]."""
    out = []
    for d in docs.get("phieu-cham-soc", []):
        da = d["data"].get("Tiền sử dị ứng") or {}
        if isinstance(da, dict) and (da.get("Chưa ghi nhận") is not None or da.get("Nội dung")):
            out.append({"chua_ghi_nhan": bool(da.get("Chưa ghi nhận")) and not da.get("Nội dung"), "noi_dung": clean(da.get("Nội dung")), "source": f"phieu-cham-soc#{d['seq']}"})
            break
    return out


def normalize(man, docs, llm, *, max_tokens_coding=8000, raw_url_prefix=None, generated_by="fhir_pipeline.normalize"):
    """JSON tài liệu -> clinical dict (chưa ghi file)."""
    patient = map_patient(docs)
    enc = map_encounter(docs)
    org = map_organization(docs)
    cov = map_coverage(docs)
    loc = map_location(docs, enc)
    prac = map_practitioner(docs)
    dx = map_diagnoses(docs)
    ctx = map_document_context(docs, enc, dx)
    dis = map_discharge(docs, ctx)
    cf = map_clinical_finding(docs)
    hpi, mh, fh = map_histories(docs)
    obs = map_observations(docs, enc)
    labs, lab_reports = map_labs(docs)
    drs = map_diagnostic_reports(docs)
    meds, srs, diets = map_orders(docs)
    claim = map_claim(docs, org)
    docref = map_document_reference(man, raw_url_prefix)

    # --- mã hoá bằng LLM cho phần chưa tra được
    todo = []
    for i, d in enumerate(dx):
        if not d["code"]:
            todo.append({"id": f"dx{i}", "kind": "icd10", "text": d["text"]})
    for i, l in enumerate(labs):
        if not l["code"]:
            todo.append({"id": f"lab{i}", "kind": "loinc_lab", "text": l["ten_xet_nghiem"], "group": l["nhom"], "unit": l["ucum"]["unit_raw"], "value": l["original_text"]})
    for i, r in enumerate(drs):
        if not r["code"]:
            todo.append({"id": f"dr{i}", "kind": "loinc_imaging", "text": r["ucum"]["key"]})
    for i, m in enumerate(meds):
        todo.append({"id": f"med{i}", "kind": "atc", "text": m["original_text"]})
    coded = llm_coding(todo, llm, max_tokens_coding)
    for i, d in enumerate(dx):
        c = coded.get(f"dx{i}")
        if c and c.get("code"):
            d["code"], d["code_source"] = c["code"], "llm"
        else:
            d["code_source"] = "document" if d["code"] else None
        info = icd10_lookup(d["code"])
        d["display"] = info["vi"] if info else (c or {}).get("display_vi")
        d["display_en"] = info["en"] if info else (c or {}).get("display_en")
        d["icd10_code"] = d["code"]
        d["codesystem"] = SYS["icd10vn"]
        d["codesystem_secondary"] = SYS["icd10who"]
        d["version"] = VERSION
        d["assertion"] = []
        d["position"] = pos(d["text"], d["raw"])
        d["in_dictionary"] = bool(info)
    for i, l in enumerate(labs):
        c = coded.get(f"lab{i}")
        if c and c.get("code"):
            if not loinc_check_digit(c["code"]):  # LLM bịa mã (sai chữ số kiểm tra) -> không nhận, để trống
                l["code_source"] = f"llm-rejected({c['code']})"
                continue
            l["code"], l["display"], l["display_en"], l["code_source"] = c["code"], c.get("display_vi"), c.get("display_en"), f"llm({c.get('confidence')})"
    for i, r in enumerate(drs):
        c = coded.get(f"dr{i}")
        if c and c.get("code"):
            if not loinc_check_digit(c["code"]):
                r["code_source"] = f"llm-rejected({c['code']})"
                continue
            r["code"], r["display"], r["code_source"] = c["code"], c.get("display_en") or c.get("display_vi"), f"llm({c.get('confidence')})"
    for i, m in enumerate(meds):
        c = coded.get(f"med{i}")
        if c and c.get("code"):
            m["code"], m["display"], m["code_source"] = c["code"], c.get("display_en") or c.get("display_vi"), f"llm({c.get('confidence')})"

    clinical = {
        "meta": {"case": man["case"], "generated_by": generated_by, "vncore_version": VERSION, "source_documents": [d["data_file"] for d in man["documents"] if d.get("data_file")]},
        "patient": patient, "related_person": map_related_person(docs), "encounter": enc, "coverage": cov, "location": loc, "organization": org,
        "practitioner": prac, "document_context": ctx, "discharge": dis, "clinical_finding": cf, "history_of_present_illness": hpi,
        "medical_history": mh, "family_histories": fh, "lab_reports": lab_reports, "document_reference": docref, "claim": claim,
        "observations": obs, "diagnoses": dx, "lab_results": labs, "diagnostic_reports": drs, "medication_requests": meds,
        "service_requests": srs, "care_orders": diets,
        "consultation": map_consultation(docs), "allergies": map_allergies(docs),
    }
    return clinical


def summary(clinical) -> dict:
    return {"counts": {k: len(v) for k, v in clinical.items() if isinstance(v, list)},
            "diagnoses": [(d["type"], d["text"], d["code"], d["code_source"]) for d in clinical["diagnoses"]],
            "labs_uncoded": [l["ten_xet_nghiem"] for l in clinical["lab_results"] if not l["code"]],
            "medications": [(m["ten_thuoc"], m["code"]) for m in clinical["medication_requests"]]}
