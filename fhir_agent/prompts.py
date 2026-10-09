"""Prompt cho từng nhiệm vụ. Nội dung rút từ skill vn-fhir-convert (SKILL.md, clinical-schema.md, coding-rules.md,
document-types.md, pitfalls.md). Hình mẫu khối dùng GIÁ TRỊ GIỮ CHỖ (<…>), không chép dữ liệu ca thật.
"""
from __future__ import annotations

import json

SYSTEM = """Bạn là chuyên viên chuyển đổi hồ sơ bệnh án Việt Nam (TT 32/2023/TT-BYT) sang "clinical JSON v2" — lớp trung gian để \
script dựng FHIR R4 theo VN Core IG 0.10.0. Đọc phiếu như một bác sĩ / kế toán viện phí. KHÔNG BAO GIỜ BỊA.

Quy tắc bắt buộc:
1. Chỉ ghi điều có trên phiếu. Giữ NGUYÊN VĂN tên xét nghiệm, chẩn đoán, kết luận, đơn vị in (`text`, `unit_raw`). Mọi suy luận không in \
trên giấy (chuyên khoa, cấp KCB, giờ thực hiện suy ra…) phải ghi vào `review_notes`.
2. Mã hoá: KHÔNG tự ghi mã LOINC, CLS, UCUM, SNOMED, ATC — để `"codings": []`, script sẽ tra cứu theo text + đơn vị. Chỉ ghi mã ICD-10 \
khi mã được IN trên phiếu: {"system":"icd10vn","code":"M50.2","source":"document"}. Trường mã danh mục VN Core (ethnicity.code, \
referral_mode, category dòng bảng kê, department_code…) chỉ chọn mã trong DANH MỤC được cung cấp; không chắc ⇒ bỏ trường, giữ text.
3. Mọi resource có `sources: ["doc-NN"]` = các phiếu chứa thông tin đó. review_notes / conflicts / unmapped: mỗi mục MỘT câu ngắn, KHÔNG chép lại nguyên dòng phiếu hay bảng (câu trả lời dài vô ích sẽ bị cắt).
4. Thời gian ISO: "YYYY-MM-DDTHH:MM:SS+07:00"; chỉ có ngày ⇒ "YYYY-MM-DD"; không có ⇒ bỏ trường và ghi `note`. "08h34 02/02/2026" ⇒ \
"2026-02-02T08:34:00+07:00".
5. id: chữ thường không dấu, khớp ^[a-z0-9][a-z0-9.-]{0,63}$, ổn định, có nghĩa: pt-<mã BN>, enc-<mã lượt>, doc-NN, \
cond-<ad|dd|cm><n>-<slug>, obs-vs-<NN>-<MMDD>-<HHMM>-<khoá>, obs-lab-<panel>-<NN>-<slug>, obs-exam-<NN>-<MMDD>-<HHMM>-<slug>, \
sr-<NN>-<slug>, dr-<slug>, spc-<loại>-<mã mẫu>, pr-<slug họ tên>, dept-<slug>, org-<slug>, loc-<slug>, med-/mr-/ma-<slug>, cp-<slug>, ci-<slug>.
6. Tham chiếu giữa resource = chuỗi id (không phải "Type/id"), CHỈ trỏ tới id có trong REGISTRY hoặc id bạn tạo trong cùng câu trả lời.
7. Mâu thuẫn giữa các phiếu ⇒ `conflicts[]` {field, values:[{value, source}], resolution, rationale} — không chọn im lặng. Thông tin không \
biểu diễn được ⇒ `unmapped[]` {item, reason, action}.
8. Chuỗi dạng [[P1]], [[ID3]], [[D1]], [[A2]] và id chứa "pii-p1" là dữ liệu đã được che (họ tên, mã định danh, ngày sinh, địa chỉ). Chép \
NGUYÊN chuỗi vào trường tương ứng, không đoán, không sửa, không tách.
9. Trả về DUY NHẤT một JSON object với đúng các khoá được yêu cầu (khoá không có dữ liệu ⇒ [] hoặc null).
"""

P = "<…>"
CODE = {"text": "<nguyên văn in trên phiếu>", "codings": []}

SHAPES: dict[str, object] = {
    "patient": {"id": "pt-<mã BN chữ thường>", "name": {"text": "<Họ tên viết hoa chữ đầu>"}, "gender": "male|female|unknown",
                "birth_date": "YYYY-MM-DD", "identifiers": [{"type": "CCCD|BHYT|MRN|MA_BENH_AN|SO_VAO_VIEN|…", "value": P, "text": "<nhãn in, khi type ngoài danh mục>"}],
                "ethnicity": {"code": "<mã vn-ethnicity-cs>", "text": "<in>"}, "occupation": {"text": "<in>"}, "nationality": {"code": "VN", "text": "Việt Nam"},
                "address": {"text": "<nguyên văn>", "line": ["<số nhà/thôn/khu>"], "ward": "<Xã/Phường …>", "state": "<Tỉnh/Thành phố …>", "country": "VN"},
                "managing_organization": "<org id cơ sở>", "sources": ["doc-NN"]},
    "coverage": {"id": "cov-bhyt-<số thẻ chữ thường>", "bhyt_number": "<2 chữ + 13 số>", "card_type": {"code": "<2 ký tự đầu thẻ>"},
                 "period": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}, "benefit_level": 80, "primary_care_facility": "<org id nơi ĐKKCB ban đầu>",
                 "payor": "Bảo hiểm xã hội Việt Nam", "status": "active", "sources": ["doc-NN"]},
    "encounter": {"id": "enc-<mã lượt>", "identifiers": [{"type": "MA_LUOT_KCB", "value": P}, {"type": "SO_VAO_VIEN", "text": "Số vào viện", "value": P}],
                  "status": "finished", "class": "IMP|AMB|EMER", "type": {"code": "<vn-encounter-type-cs>"},
                  "period": {"start": "<giờ vào viện ISO>", "end": "<giờ ra viện ISO>"}, "reason_text": "<lý do vào viện nguyên văn>",
                  "service_provider": "<org id cơ sở>", "department": "<dept id khoa điều trị>",
                  "locations": [{"location": "<loc id>", "period": {"start": P, "end": P}, "status": "completed"}],
                  "participants": [{"individual": "<pr id>", "type": "ADM|ATND|CON|DIS", "period": {"start": P}}],
                  "referral_mode": "<vn-referral-mode-cs>", "treatment_outcome": "<vn-treatment-outcome-cs>", "discharge_disposition": "<vn-discharge-disposition-cs>",
                  "discharge_disposition_text": "<in>", "payment_method": "1", "length_days": 1, "admit_source_text": "<in>", "sources": ["doc-NN"]},
    "organizations": [{"id": "org-<slug>|dept-<slug>", "kind": "facility|department|payer", "name": "<tên đầy đủ>", "aliases": ["<cách viết khác trên phiếu>"],
                       "identifiers": [{"type": "CSKCB", "value": "<mã CSKCB 5 số nếu in>"}], "department_code": "<mã vn-department-cs, chỉ cho khoa>",
                       "part_of": "<org id cơ sở, cho khoa>", "org_type": "<vn-organization-type-cs>", "address": {"text": P}, "telecom": [{"system": "phone", "value": P, "use": "work"}],
                       "sources": ["doc-NN"]}],
    "locations": [{"id": "loc-<slug>", "name": "<tên in>", "kind": "clinic|ward|room|bed", "local_code": "<mã in, vd P.304, H036>",
                   "managing_organization": "<org/dept id>", "part_of": "<loc id cha>", "sources": ["doc-NN"]}],
    "practitioners": [{"id": "pr-<slug họ tên>", "name_raw": "<nguyên văn kèm học hàm, vd ThS.Bs Nguyễn Văn A>", "name": {"text": "<Họ tên>"},
                       "qualifications": ["<vn-qualification-cs: BS|BS-CKI|BS-CKII|ThS|TS|DD|KTV|…>"],
                       "roles": [{"organization": "<dept/org id>", "title_text": "<chức danh / vai trò in, vd Bác sĩ điều trị>", "location": "<loc id hoặc null>"}],
                       "sources": ["doc-NN"]}],
    "conditions": [{"id": "cond-ad1-<slug>", "role": "admission|discharge|comorbidity|problem", "rank": 1, "text": "<chẩn đoán nguyên văn>",
                    "code": {"text": "<chẩn đoán nguyên văn>", "codings": [{"system": "icd10vn", "code": "<chỉ khi mã IN trên phiếu>", "source": "document"}]},
                    "clinical_status": "active", "verification_status": "provisional|confirmed", "recorded_date": "<ISO>", "recorder": "<pr id>",
                    "asserter": "<pr id>", "note": "<TD = theo dõi ⇒ provisional; CRNN = chưa rõ nguyên nhân>", "sources": ["doc-NN"]}],
    "encounter_diagnoses": [{"condition": "<cond id>", "use": "AD|DD|CM|CC", "rank": 1}],
    "observations": [{"id": "obs-…", "kind": "vital-sign|lab|exam|survey", "code": CODE, "status": "final", "effective": "<ISO>", "issued": "<ISO giờ trả KQ>",
                      "performer": ["<pr id>"], "value": {"quantity": {"value": 0.0, "unit_raw": "<đơn vị in>"}},
                      "interpretation": ["H|L|N|A"], "reference_range": {"text": "<nguyên văn>", "low": 0.0, "high": 0.0},
                      "specimen": "<spc id>", "based_on": ["<sr id>"], "method_text": "<in>", "device_text": "<in>", "note": P, "sources": ["doc-NN"]}],
    "_value_variants": [{"quantity": {"value": 18.43, "unit_raw": "G/L"}}, {"string": "Âm tính"}, {"absent": "not-performed"}, {"integer": 15}],
    "_blood_pressure": {"kind": "vital-sign", "code": {"text": "Huyết áp", "codings": []},
                        "components": [{"code": {"text": "Huyết áp tâm thu", "codings": []}, "value": {"quantity": {"value": 120, "unit_raw": "mmHg"}}},
                                       {"code": {"text": "Huyết áp tâm trương", "codings": []}, "value": {"quantity": {"value": 80, "unit_raw": "mmHg"}}}]},
    "specimens": [{"id": "spc-<bld|ser|ur>-<mã mẫu>", "type": {"code": "BLD|SER|UR|PLAS|CSF", "text": "<loại mẫu in>"},
                   "identifiers": [{"type": "LOCAL", "text": "Mã bệnh phẩm", "value": "<mã mẫu>"}], "status": "available",
                   "received": "<ISO giờ nhận mẫu>", "requests": ["<sr id>"], "note": P, "sources": ["doc-NN"]}],
    "service_requests": [{"id": "sr-<NN>-<slug>", "category": "XN|CDHA|TDCN", "code": CODE, "status": "completed", "intent": "order",
                          "authored_on": "<ISO giờ chỉ định>", "occurrence": "YYYY-MM-DD", "requester": "<pr id>", "reason_reference": ["<cond id>"],
                          "note": P, "sources": ["doc-NN"]}],
    "diagnostic_reports": [{"id": "dr-<slug>", "kind": "lab|imaging|functional", "code": CODE, "based_on": ["<sr id>"], "status": "final",
                            "effective": "<ISO giờ thực hiện/nhận mẫu>", "issued": "<ISO giờ trả KQ/ký>", "performer": ["<dept/pr id>"],
                            "results_interpreter": ["<pr id>"], "specimen": ["<spc id, không dùng cho imaging>"], "results": ["<obs id>"],
                            "technique_text": "<mục Kỹ thuật>", "findings": ["<từng dòng mô tả>"], "conclusion": "<Kết luận nguyên văn>",
                            "presented_form": "doc-NN", "note": P, "sources": ["doc-NN"]}],
    "medications": [{"id": "med-<slug>", "code": CODE, "form": {"text": "<dạng bào chế>"},
                     "ingredients": [{"item": {"text": "<hoạt chất>"}, "strength": {"value": 100, "unit_raw": "mg"}, "per": "viên"}], "sources": ["doc-NN"]}],
    "medication_requests": [{"id": "mr-<slug>-<YYYYMMDD>", "medication": "<med id>", "status": "completed", "intent": "order", "category": "inpatient",
                             "authored_on": "<ISO>", "requester": "<pr id>", "reason_reference": ["<cond id>"],
                             "dosage": {"text": "<y lệnh nguyên văn>", "route": {"text": "Uống", "vn_code": "<vn-medication-route-cs>"},
                                        "timing": {"frequency": 2, "period": 1, "period_unit": "d", "time_of_day": ["08:00:00"]},
                                        "dose": {"value": 1, "unit_raw": "viên"}},
                             "dispense": {"value": 2, "unit_raw": "viên"}, "sources": ["doc-NN"]}],
    "medication_administrations": [{"id": "ma-<slug>-<YYYYMMDD>-<HHMM>", "medication": "<med id>", "request": "<mr id>", "status": "completed",
                                    "category": "inpatient", "effective": "<ISO>", "performer": ["<pr id>"],
                                    "dosage": {"text": P, "route": {"text": "Uống"}, "dose": {"value": 1, "unit_raw": "viên"}},
                                    "note": "<bằng chứng thực hiện; suy luận ⇒ ghi rõ>", "sources": ["doc-NN"]}],
    "allergies": [{"id": "ai-nka", "nka": True, "text": "<nguyên văn, vd Chưa ghi nhận>", "verification_status": "unconfirmed",
                   "recorded_date": "<ISO>", "recorder": "<pr id>", "sources": ["doc-NN"]}],
    "family_history": [{"id": "fmh-<slug>", "status": "completed", "relationship": {"text": "<quan hệ hoặc Gia đình (chung)>", "codings": []},
                        "date": "<ISO>", "conditions": [{"text": P}], "note": "<nguyên văn tiền sử gia đình>", "sources": ["doc-NN"]}],
    "clinical_impressions": [{"id": "ci-<slug>", "status": "completed", "code_text": "<tên biên bản>", "date": "<ISO>", "assessor": "<pr id chủ toạ>",
                              "problems": ["<cond id>"], "description": "<tóm tắt bệnh án + khám>", "summary": "<kết luận + hướng điều trị>",
                              "findings": [{"reference": "<cond id>", "basis": P}], "notes": [P], "sources": ["doc-NN"]}],
    "care_plans": [{"id": "cp-<slug>", "title": P, "status": "completed", "intent": "order|plan", "category": [{"text": P}],
                    "period": {"start": "<ISO>", "end": "<ISO>"}, "created": "<ISO>", "author": "<pr id>", "addresses": ["<cond id>"],
                    "description": "<chẩn đoán điều dưỡng / mục tiêu>",
                    "activities": [{"kind": "ServiceRequest|NutritionOrder|MedicationRequest", "code": {"text": P}, "description": "<y lệnh nguyên văn>",
                                    "status": "completed", "scheduled_text": P}], "notes": [P], "sources": ["doc-NN"]}],
    "cost_statement": {"ma_lk": "<mã liên kết / mã lượt>", "so_bang_ke": "<số in>", "form_name": "<tên mẫu in>",
                       "period": {"start": "<ISO>", "end": "<ISO>"}, "prepared_at": "<ISO giờ in bảng kê>", "claim_type": "institutional",
                       "claim_subtype": "<vn-patient-visit-type-cs (suy luận ⇒ review_notes)>", "payment_method": "1", "insurer": "Bảo hiểm xã hội Việt Nam",
                       "diagnoses": [{"condition": "<cond id>", "type": "principal|comorbidity", "type_text": "<nhãn in>"}],
                       "segments": [{"sequence": 1, "coverage": "<cov id>", "period": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}, "benefit_level": 80, "title": P}],
                       "lines": [{"sequence": 1, "segment": 1, "category": "<vn-cost-category-cs theo cột Nhóm>", "text": "<tên dịch vụ/thuốc in>", "code": None,
                                  "unit": "<ĐVT>", "quantity": 1, "hospital_unit_price": 0, "insurance_unit_price": 0, "service_payment_ratio": 100,
                                  "hospital_amount": 0, "insurance_payment_ratio": 100, "insurance_amount": 0,
                                  "allocation": {"insurance_fund": 0, "patient_copay": 0, "other_source": 0, "patient_self_pay": 0},
                                  "department": "<mã khoa Kxx>", "serviced": "YYYY-MM-DD", "scope": "1|2|3", "benefit_level": 80, "payment_source": "1"}],
                       "totals": {"hospital_total": 0, "insurance_total": 0, "insurance": 0, "patient_copay": 0, "other_fund": 0, "patient_self_pay": 0,
                                  "non_covered": 0, "medication": 0, "supplies": 0},
                       "total_in_words": "<nguyên văn>", "attestations": [{"role": "preparer", "party": "<org/pr id>", "time": "<ISO>"}],
                       "composition_status": "preliminary|final", "invoice_status": "issued", "sources": ["doc-NN"]},
    "compositions": {"discharge_summary": {"id": "comp-<slug>", "identifier": {"value": "<mã bệnh án>", "text": "Mã bệnh án"}, "status": "final",
                                           "type": {"code": "18842-5", "display": "Discharge summary"}, "type_text": "<tên mẫu in>", "title": P,
                                           "date": "<ISO>", "authors": ["<pr id>"], "attesters": [{"mode": "professional", "time": "<ISO>", "party": "<pr id>"}],
                                           "custodian": "<org id cơ sở>", "confidentiality": "R",
                                           "sections": [{"key": "chief_complaint|hpi|history|physical_exam|diagnosis|hospital_course|lab_results|medications|discharge_summary|plan_of_care",
                                                         "title": "<tiêu đề mục in>", "text_lines": ["<nguyên văn>"], "entries": ["<id resource liên quan trong REGISTRY>"]}],
                                           "sources": ["doc-NN"]}},
    "documents_patch": [{"id": "doc-NN", "title": "<tiêu đề phiếu>", "date": "<ISO ngày/giờ lập phiếu>", "extracted": True, "authors": ["<pr id>"],
                         "note": "<vd trang phụ lục không chứa dữ liệu người bệnh ⇒ extracted=false>"}],
    "conflicts": [{"field": "<trường>", "values": [{"value": P, "source": "doc-NN"}], "resolution": P, "rationale": P}],
    "unmapped": [{"item": P, "reason": P, "action": P}],
    "review_notes": ["<mọi suy luận không có trên giấy>"],
}


def shape(keys: list[str]) -> str:
    out = {}
    for k in keys:
        if k in SHAPES:
            out[k] = SHAPES[k]
    extra = []
    if "observations" in keys:
        extra.append("Biến thể `value` (đúng 1 khoá): " + json.dumps(SHAPES["_value_variants"], ensure_ascii=False))
        extra.append("Huyết áp = 1 observation với `components` (không có `value`): " + json.dumps(SHAPES["_blood_pressure"], ensure_ascii=False))
    return json.dumps(out, ensure_ascii=False, indent=1) + ("\n" + "\n".join(extra) if extra else "")


COMMON_TAIL = ["documents_patch", "conflicts", "unmapped", "review_notes"]

TASKS = {
    "admin": {
        "title": "Bộ khung hành chính (đọc TOÀN BỘ hồ sơ)",
        "owns": ["patient", "coverage", "encounter", "organizations", "locations", "practitioners"],
        "instructions": """Chốt bộ khung từ chứng từ kết luận (Bản tóm tắt HSBA, Bảng kê chi phí), rồi phiếu khám vào viện:
- patient: họ tên, giới, ngày sinh (chỉ ngày sinh, không lưu tuổi), mọi định danh in (CCCD, BHYT, mã BN → type MRN, mã bệnh án, số vào viện…), dân tộc, nghề, địa chỉ.
- coverage: thẻ BHYT (số thẻ, hạn, mức hưởng %, nơi ĐKKCB ban đầu → organizations kind facility kèm mã CSKCB).
- encounter.identifiers: MA_LUOT_KCB = giá trị "Số khám bệnh" / "Mã lượt KCB" in trên Bảng kê chi phí (không phải "Sổ lưu trữ");
  "Sổ lưu trữ" ⇒ type "LOCAL", text "Sổ lưu trữ"; "Số vào viện" ⇒ type "SO_VAO_VIEN".
- encounter: period vào/ra viện, khoa điều trị, buồng/giường (locations), người tham gia (ADM bác sĩ khám vào viện,
  ATND bác sĩ điều trị, CON hội chẩn, DIS bác sĩ ra viện), hình thức đến, kết quả điều trị, hình thức ra viện, số ngày điều trị.
  KHÔNG ghi encounter.diagnoses (nhiệm vụ khác làm).
- organizations: cơ sở KCB (kind facility, org_type), MỌI khoa/phòng xuất hiện (kind department, department_code nếu khớp danh mục), nơi ĐKKCB.
- locations: phòng khám, buồng, giường.
- practitioners: MỌI nhân viên y tế có tên trên bất kỳ phiếu nào (người ký, người chỉ định, KTV, điều dưỡng, bác sĩ đọc kết quả),
  name_raw giữ nguyên học hàm/chức danh in; qualifications theo tiền tố in (ThS.Bs ⇒ ["ThS","BS"], BSCKII ⇒ ["BS-CKII"], ĐD ⇒ ["DD"]).
  Một người xuất hiện nhiều phiếu ⇒ MỘT phần tử, sources gộp đủ các phiếu.
- documents_patch: tiêu đề + ngày lập + người ký (authors) cho từng phiếu; trang phụ lục / bản scan không có chữ ⇒ extracted=false + note.""",
        "enums": ["vn-ethnicity-cs", "vn-encounter-type-cs", "vn-referral-mode-cs", "vn-treatment-outcome-cs", "vn-discharge-disposition-cs",
                  "vn-qualification-cs", "vn-organization-type-cs", "vn-department-cs", "vn-identifier-type-cs"],
        "pitfalls": ["checkbox", "tuổi", "mã his", "namespace", "chuyên khoa"],
        "max_tokens": 24000,
    },
    "clinical": {
        "title": "Chẩn đoán, tiền sử, dị ứng, hội chẩn (đọc TOÀN BỘ hồ sơ)",
        "owns": ["conditions", "encounter_diagnoses", "allergies", "family_history", "clinical_impressions"],
        "instructions": """- conditions: MỘT phần tử cho mỗi chẩn đoán phân biệt (vào viện / ra viện / bệnh kèm theo / kết luận hội chẩn), gộp sources
  các phiếu ghi cùng chẩn đoán. role admission (chẩn đoán vào viện, khoa khám bệnh, hội chẩn), discharge (ra viện, bệnh chính),
  comorbidity (bệnh kèm theo). Mã ICD-10 in sẵn (bảng kê, tóm tắt HSBA) ⇒ codings source document. "TD …" ⇒ provisional.
  recorded_date = giờ phiếu ghi chẩn đoán lần đầu; recorder = người ký phiếu đó.
  Dòng chẩn đoán HIS tự chèn tên ICD của mã đã có (vd "…; Các thoát vị đĩa đệm cột sống cổ khác") ⇒ KHÔNG tạo Condition thừa.
- encounter_diagnoses: danh sách chẩn đoán của lượt điều trị (AD vào viện, DD ra viện, CM bệnh kèm theo) kèm rank.
- allergies: "Chưa ghi nhận"/"Bình thường" ⇒ nka=true. family_history: tiền sử gia đình. clinical_impressions: mỗi biên bản hội chẩn.""",
        "enums": [],
        "pitfalls": ["icd", "his tự chèn"],
        "max_tokens": 16000,
    },
    "orders": {
        "title": "Y lệnh: chỉ định cận lâm sàng, thuốc, chế độ chăm sóc",
        "owns": ["service_requests", "medications", "medication_requests", "care_plans"],
        "instructions": """- service_requests: MỖI xét nghiệm / CĐHA / TDCN được chỉ định (mục "Phiếu XN / CĐHA" trên tờ điều trị, hướng điều trị hội chẩn):
  category XN|CDHA|TDCN, code.text nguyên văn, authored_on = giờ y lệnh, requester = bác sĩ chỉ định, reason_reference = cond id.
  Mỗi xét nghiệm sinh hoá riêng lẻ là 1 service_request riêng. id dạng sr-<NN thứ tự>-<slug>.
- medications + medication_requests: thuốc trong y lệnh (liều, đường dùng, giờ, số lượng). route.vn_code chọn trong danh mục.
- care_plans: y lệnh chế độ ăn, chế độ chăm sóc (cấp I/II/III), theo dõi — intent order. (Kế hoạch chăm sóc điều dưỡng làm ở nhiệm vụ khác.)""",
        "enums": ["vn-medication-route-cs", "vn-service-request-category-cs"],
        "pitfalls": ["medicationadministration", "y lệnh"],
        "max_tokens": 16000,
    },
    "results": {
        "title": "Phiếu kết quả cận lâm sàng",
        "owns": ["specimens", "diagnostic_reports", "observations"],
        "instructions": """Trích phiếu kết quả được giao (xét nghiệm / CĐHA / TDCN):
- diagnostic_reports: 1 báo cáo / phiếu (kind lab|imaging|functional), based_on = sr id tương ứng trong REGISTRY (khớp theo tên dịch vụ),
  effective = giờ thực hiện / nhận mẫu, issued = giờ trả kết quả / ký; imaging: technique_text, findings[] (từng dòng mô tả), conclusion nguyên văn.
  Nếu dòng Kết luận lỗi font (ký tự rác) ⇒ lấy kết luận nguyên văn từ TÀI LIỆU THAM KHẢO (tờ điều trị / tóm tắt), thêm doc đó vào sources và ghi note.
- specimens (chỉ xét nghiệm): loại mẫu (BLD máu toàn phần, SER huyết thanh, UR nước tiểu), mã mẫu, giờ nhận mẫu, requests = sr id.
- observations kind "lab": MỖI dòng kết quả là 1 observation: code.text = tên xét nghiệm in (giữ cả ký hiệu [G/L], [%] nếu có),
  value.quantity {value, unit_raw = đơn vị in}; chuỗi ⇒ value.string; ô trống ⇒ value.absent "not-performed" (không ghi 0);
  interpretation theo cờ H/L in cạnh giá trị (không tự suy); reference_range.text nguyên văn + low/high; specimen; based_on; performer;
  effective = giờ nhận mẫu / thực hiện; issued = giờ trả KQ. id: obs-lab-<huyet-hoc|sinh-hoa|nuoc-tieu>-<NN>-<slug>.
  Báo cáo CĐHA/TDCN không có observation riêng (chỉ conclusion/findings).
- diagnostic_reports.results = danh sách id các observation vừa tạo.""",
        "enums": [],
        "pitfalls": ["ký hiệu máy", "ô kết quả trống", "lỗi font", "giờ in", "# vs %", "deprecated"],
        "max_tokens": 24000,
    },
    "observe": {
        "title": "Sinh hiệu, khám, thang điểm, thực hiện thuốc, chăm sóc điều dưỡng",
        "owns": ["observations", "medication_administrations", "care_plans"],
        "instructions": """Trích phiếu được giao (phiếu khám / tờ điều trị / phiếu chăm sóc / hội chẩn / đánh giá / sàng lọc):
- observations kind "vital-sign": MỖI chỉ số ở MỖI mốc giờ là 1 observation: Mạch, Nhiệt độ, Huyết áp (1 obs có components tâm thu/tâm trương),
  Nhịp thở, SpO2, Cân nặng, Chiều cao, BMI. code.text = tên chỉ số ("Mạch", "Nhiệt độ", "Huyết áp", "Nhịp thở", "SpO2", "Cân nặng", "Chiều cao", "BMI"),
  value.quantity.unit_raw = đơn vị in. effective = giờ của cột / giờ khám (phiếu khám: "Đến khám bệnh lúc…", KHÔNG phải giờ ký cuối phiếu); performer = người ghi. id: obs-vs-<NN phiếu>-<MMDD>-<HHMM>-<mach|nhiet-do|huyet-ap|nhip-tho|spo2|can-nang|chieu-cao|bmi>.
- observations kind "exam": dấu hiệu khám có giá trị (Glasgow, tri giác, tình trạng da, đau…), value.string nguyên văn hoặc quantity (điểm).
- observations kind "survey": điểm thang đánh giá (nguy cơ té ngã, sàng lọc dinh dưỡng NRS…), value.quantity unit_raw "điểm", note = xếp loại in.
  Bản phiếu để trống điểm ⇒ không tạo observation, chỉ documents_patch note.
- medication_administrations: chỉ khi phiếu có bằng chứng thực hiện thuốc (cột thực hiện thuốc, ký điều dưỡng); medication/request = id trong REGISTRY.
- care_plans (chỉ phiếu chăm sóc): kế hoạch chăm sóc điều dưỡng (chẩn đoán điều dưỡng, mục tiêu, can thiệp), intent plan.
- Trang phụ lục bảng mã (không chứa dữ liệu người bệnh) ⇒ trả mảng rỗng + documents_patch extracted=false.""",
        "enums": [],
        "pitfalls": ["đơn vị sinh hiệu", "checkbox", "phụ lục", "bản trống", "medicationadministration"],
        "max_tokens": 24000,
    },
    "cost": {
        "title": "Bảng kê chi phí KCB (Mẫu 01/KBCB, QĐ 6556)",
        "owns": ["cost_statement"],
        "instructions": """- cost_statement: thông tin chung (mã liên kết, số bảng kê, thời gian điều trị, giờ in), segments (giai đoạn thẻ + mức hưởng),
  diagnoses (cond id trong REGISTRY: chẩn đoán xác định = principal, bệnh kèm theo = comorbidity), totals, attestations (ô ký có in tên;
  không in tên ⇒ chỉ preparer = cơ sở, composition_status preliminary).
- lines: MỖI dòng dịch vụ/thuốc/vật tư là 1 phần tử, chép số CHÍNH XÁC từng đồng: quantity, hospital_unit_price, insurance_unit_price,
  service_payment_ratio, hospital_amount, insurance_payment_ratio, insurance_amount, allocation (quỹ BHYT, cùng chi trả, nguồn khác, tự trả).
  category theo nhóm in (vn-cost-category-cs). Dòng tỷ lệ BHYT 0 và tự trả 100% ⇒ scope "2", insurance_amount 0. department = mã khoa Kxx.
  Số in có dấu chấm phân cách nghìn ("50.600") ⇒ số nguyên 50600.""",
        "enums": ["vn-cost-category-cs", "vn-insurance-scope-cs", "vn-payment-source-cs", "vn-patient-visit-type-cs", "vn-department-cs",
                  "vn-cost-statement-attestation-role-cs"],
        "pitfalls": ["bảng kê", "ldl", "hai dòng bảng kê", "công khai"],
        "max_tokens": 32000,
    },
    "summary": {
        "title": "Bản tóm tắt hồ sơ bệnh án → Composition",
        "owns": ["compositions"],
        "instructions": """- compositions.discharge_summary: từ Bản tóm tắt HSBA. sections theo các mục in (lý do vào viện, quá trình bệnh lý, tiền sử,
  khám, chẩn đoán, diễn biến, kết quả CLS, thuốc, tình trạng ra viện, hướng điều trị), text_lines nguyên văn, entries = id liên quan có trong REGISTRY
  (chẩn đoán, kết quả CLS, dị ứng, tiền sử…). Không tạo resource mới.""",
        "enums": [],
        "pitfalls": [],
        "max_tokens": 16000,
    },
}

CHOOSE_SYSTEM = """Bạn là chuyên viên mã hoá thuật ngữ y khoa. Với mỗi mục, chọn ĐÚNG MỘT ứng viên (theo chỉ số k) khớp nghĩa lâm sàng với \
văn bản, hoặc null nếu không ứng viên nào đúng nghĩa. Không bao giờ đề xuất mã ngoài danh sách. Trả về JSON {"choices":[{"i":0,"k":0,"reason":"…"}]}."""

REPAIR_SYSTEM = SYSTEM + """
Bạn đang SỬA một resource đã trích xuất mà bộ kiểm tra chất lượng báo lỗi. Sửa theo đúng phiếu gốc; giữ nguyên `id`. Nếu lỗi do thiếu dữ liệu \
trên phiếu thì bỏ trường đó (và ghi note), không bịa. Nếu resource không thể hợp lệ vì không có trên phiếu ⇒ trả {"drop": true, "reason": "…"}.
Trả về {"resource": {...}} hoặc {"drop": true, "reason": "..."}."""
