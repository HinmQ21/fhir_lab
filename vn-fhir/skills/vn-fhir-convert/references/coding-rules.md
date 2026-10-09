# Quy tắc mã hoá (terminology) — "không bịa mã"

Nguyên tắc nền: **mã chỉ được ghi khi lấy từ (a) hồ sơ in sẵn, (b) kết quả tra cứu `term_lookup.py`, hoặc (c) bảng
curated trong `vnlib`**. Mỗi coding mang `source` ∈ {document, lookup, curated, conceptmap, llm, manual} và `verified`.
`source: llm` chỉ là *đề xuất* — `quality_check.py` báo lỗi nếu chưa `verified: true` do người duyệt đặt. Không tìm được
mã ⇒ `codings: []` + `text` nguyên văn + 1 dòng trong `unmapped[]`. Builder vẫn tạo resource hợp lệ (CodeableConcept.text).

## Hệ mã dùng trong VN Core 0.10.0 (khoá `vnlib.SYS` → URL)

| khoá | URL | dùng cho | tra cứu |
|---|---|---|---|
| `icd10vn` | `…/CodeSystem/vn-icd10-cs` | Condition.code slice icd10vn (bắt buộc với vn-core-condition-diagnosis), Claim.diagnosis | `term_lookup.py icd10 "<tên>"`, `icd10-code <mã>` |
| `icd10who` | `http://hl7.org/fhir/sid/icd-10` | slice icd10who (tuỳ chọn, cùng mã, tên EN) | tự sinh từ từ điển (`source: conceptmap`) |
| `loinc` | `http://loinc.org` | Observation.code, DiagnosticReport.code, ServiceRequest.code, DocumentReference.type, Composition.section.code | `lab`, `imaging`, `vital`, `doc-type`, `section`, `loinc <mã> --online` |
| `cls` | `…/CodeSystem/vn-cls-cs` (QĐ 1227/QĐ-BYT) | slice vnCls của Observation/DR/SR xét nghiệm & CĐHA; Claim.item/Invoice line code | `cls "<tên>" --unit --appendix` |
| `ucum` | `http://unitsofmeasure.org` | mọi Quantity (bắt buộc với vn-core-observation-lab) | `ucum "<đơn vị>"` |
| `snomed` | `http://snomed.info/sct` | đường dùng thuốc (26643006 Oral…), NKA 716186003, body site | bảng curated nhỏ trong vnlib; không tra online |
| `atc` | `http://www.whocc.no/atc` | Medication.code (phân loại, không thay số đăng ký) | curated |
| `v2-0487` | HL7 specimen type | Specimen.type (BLD, SER, UR, PLAS, CSF…) | curated |
| vn-*-cs | `…/CodeSystem/<id>` | mọi extension/field VN Core (xem bảng dưới) | `cs <id> "<text>"`, `cs-code <id> <mã>`, `vs <vs-id> --contains <mã>` |

## Bảng mã VN Core hay dùng (đã xác minh trong vendor 0.10.0)

| Trường clinical v2 | CodeSystem | Ví dụ mã | Ghi chú |
|---|---|---|---|
| `encounter.type.code` | vn-encounter-type-cs | 01 ngoại trú, 03 nội trú | |
| `encounter.referral_mode` | vn-referral-mode-cs (required VS) | 1 Đến khám, 2 Chuyển tuyến… | ô "Hình thức đến" |
| `encounter.treatment_outcome` | vn-treatment-outcome-cs | 1 Khỏi, 2 Đỡ, 3 Không thay đổi, 4 Nặng hơn, 5 Tử vong | tóm tắt HSBA "Kết quả điều trị" |
| `encounter.discharge_disposition` | vn-discharge-disposition-cs (VS 1–5) | 1 Ra viện, 2 Chuyển viện, 3 Trốn viện, 4 Xin ra viện, 5 Tử vong | |
| `encounter.payment_method` / `cost_statement.payment_method` | vn-payment-method-cs | 1 FFS | |
| `encounter.insurance_visit_type` | vn-insurance-visit-type-cs | — | **legacy** trước 01/07/2024: không xuất; dùng Claim.subType |
| `cost_statement.claim_subtype` | vn-patient-visit-type-cs | 1.15 tự đến nội trú cấp cơ bản, 3.2… | suy luận → ghi review_notes |
| `cost_statement.lines[].category` | vn-cost-category-cs (required) | 1 Khám, 2.2 Giường nội trú, 3 XN, 4 CĐHA, 5 TDCN, 6.x VTYT, 8 Thuốc, 10 Vận chuyển | cột "Nhóm" trên bảng kê |
| `lines[].scope` | vn-insurance-scope-cs | 1 trong phạm vi, 2 ngoài phạm vi, 3 ngoài DM nhưng BHYT trả | tỷ lệ BHYT 0 + tự trả 100% ⇒ 2 |
| `lines[].payment_source` | vn-payment-source-cs | 1 BHYT | |
| `lines[].department` | vn-department-cs | K20 Ngoại thần kinh, K36 Huyết học, K47 Xét nghiệm | là **mã**, không phải tham chiếu |
| `organizations[].department_code` | vn-department-cs | như trên | kèm `type` orgKind dept |
| `organizations[].org_type` / `org_level` / `facility_care_level` | vn-organization-type-cs / vn-healthcare-level-cs / vn-facility-care-level-cs | BV, TYT / TINH / CO_BAN, CHUYEN_SAU | suy từ tên ⇒ review_notes |
| `practitioners[].qualifications[]` | vn-qualification-cs (required) | BS, BS-CKI, BS-CKII, ThS, TS, PGS, GS, YS, DD, HS, KTV, DS, DSTH, CN | từ tiền tố in ("BSCKII", "ThS.Bs", "ĐD") qua `person` |
| `roles[].specialty[]` | vn-specialty-cs | TKNGOAI, TK, CDHA, TDCN, XN, HUYET | suy từ khoa ⇒ review_notes |
| `roles[].title_code` | vn-practitioner-title-cs | V.08.01.01… | chỉ khi in trên hồ sơ |
| `patient.ethnicity.code` | vn-ethnicity-cs (required) | 01 Kinh | mã HIS trùng mã BYT, vẫn kiểm |
| `patient.address.province_code` / `ward_code` | vn-province-cs / vn-ward-cs (ĐVHC 2025) | 25 Phú Thọ / 08527 Xã Bản Nguyên | mã HIS cũ (12/08) **không** dùng |
| `coverage.card_type.code` | vn-bhyt-card-type-cs | GD, HT, DN… = 2 ký tự đầu thẻ | |
| `identifiers[].type` | vn-identifier-type-cs | CCCD, CMND, BHYT, MA_LK, MA_LUOT_KCB, BHXH, GPHN, CCHN, CSKCB, MRN, HC, GKS, MST, VNEID | type khác (MA_BENH_AN, SO_VAO_VIEN, STAFF, LOCAL) → `type.text` + system cục bộ |
| `medication_requests[].dosage.route.vn_code` | vn-medication-route-cs | 1.01 Uống | + SNOMED 26643006 |
| `cost_statement.attestations[].role` | vn-cost-statement-attestation-role-cs | preparer, billing-confirmation, patient-confirmation | 3 vai ⇒ Composition final |
| Provenance.reason | vn-consent-purpose-cs | TREAT, HPAYMT, EMR | builder tự đặt |

## LOINC — quy tắc chọn

1. **Đơn vị quyết định mã**: cùng tên xét nghiệm nhưng mmol/L (Moles/volume) ≠ mg/dL (Mass/volume). `lab "<tên>" --unit <đơn vị>` xếp hạng theo `UNIT_KIND`.
2. **Bệnh phẩm quyết định mã**: "Glucose" máu 14749-6 ≠ "Glucose" nước tiểu test strip 25428-4 → truyền `--panel nuoc_tieu` hoặc tên có "niệu".
3. **# vs %**: số tuyệt đối (G/L) và tỷ lệ (%) là hai mã (Neut# 751-8 / Neut% 770-8). LLM hay nhầm — bảng curated đã tách.
4. **Check digit** (mod-10 LOINC) loại ngay mã sai (32623-3 ✗ → 32623-1 ✓). Mã không có trong `assets/loinc_verified.json` ⇒ `loinc <mã> --online` rồi thêm vào bảng (giữ `status`).
5. **Deprecated** (21000-5 RDW-SD, 18755-9 MR report, 18760-9 US report) chỉ dùng khi không có mã thay thế; DocumentReference.type dùng 18748-4 cho mọi phiếu CĐHA.
6. **Panel**: chỉ gán mã panel khi bộ xét nghiệm khớp đúng (CBC 57021-8, UA strip 50556-0). Bộ sinh hoá tự chọn ⇒ DR.code text-only + từng Observation có mã.
7. **Sinh hiệu**: mã + đơn vị cố định theo profile (xem vncore-profiles.md). `unit` phải là fixedString ('/min', '°C', 'mmHg', 'kg', 'cm', '%') — không dịch.
8. **Tài liệu / mục**: `doc-type <form>`, `section <key>`; section Composition phải khớp slice (10154-3, 11348-0, 29545-1, 29548-5, 30954-2, 10160-0, 47519-4, 18842-5).

## CLS (QĐ 1227/QĐ-BYT) — quy tắc

- Danh mục có **3 khối CBC trùng lặp** (1000147–1000169, 1000170–1000197, 1000198–1000211): dùng khối đầu; chỉ sang khối 2 khi khái niệm không có ở khối 1 (NRBC 1000183).
- Đơn vị trong CLS là đơn vị **quy ước** của danh mục; khớp ±0.05 với `--unit` để chọn đúng dòng (Glucose 1010103 là mg/dL dù phiếu in mmol/L — vẫn dùng vì chỉ có 1 dòng).
- Không có CLS cho: khám bệnh, ngày giường, Doppler xuyên sọ, điện não đồ, thuốc, vật tư ⇒ Claim.item.productOrService text-only (hoặc ATC cho thuốc).
- ConceptMap `vn-cm-cls-to-loinc` cho gợi ý LOINC từ CLS (trường `loinc[]` trong kết quả `cls`) — dùng để **đối chiếu**, không thay bảng curated nếu đơn vị khác.

## ICD-10 VN — quy tắc

- Mã in trên bảng kê / tóm tắt HSBA là **nguồn chính** (`source: document`), kể cả khi tên bệnh gộp nhiều triệu chứng ("Đau đầu/ Rối loạn tiền đình" → H81.8 như HIS in).
- Chẩn đoán vào viện thường không in mã → `icd10 "<tên>"`; ghi `candidates[]` (top 3) và lý do chọn vào `note`. Tiền tố "TD" (theo dõi) ⇒ `verification_status: provisional`; "CRNN" = chưa rõ nguyên nhân (giữ trong text).
- Luôn kèm slice `icd10who` cùng mã (tên EN) — builder/`icd()` sinh tự động.
- Không tạo Condition cho chuỗi HIS tự chèn (tên ICD của mã đã có).

## UCUM — bảng nhanh (`vnlib.UCUM`)

G/L→`10*9/L` (unit "10^9/L"); T/L→`10*12/L`; g/dL; g/L; %; fL; pg; mmol/L; µmol/L→`umol/L`; U/L; mg/dL; mg/L; /µL→`/uL`; mmHg→`mm[Hg]`; °C→`Cel`; lần/phút→`/min`; kg; cm; kg/m2; viên→`{tbl}`; điểm→`{score}`; tỷ trọng→`{Spec_grav}`; pH→`[pH]`. Thiếu ⇒ bổ sung vào vnlib (kèm nguồn) rồi mới dùng.
