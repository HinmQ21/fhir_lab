# Bảng ánh xạ: phiếu TT32 → clinical.json → FHIR VN Core 0.10.0

Ký hiệu: `VN` = `http://fhir.hl7.org.vn/core`; `TH` = `http://terminology.hl7.org/CodeSystem`.
Cột "Nguồn" ghi phiếu được ưu tiên (thứ tự ưu tiên chung: bảng kê chi phí > bản tóm tắt HSBA > phiếu khám vào viện > phiếu khác).

## 1. Hành chính và bối cảnh

| Nguồn (phiếu / trường) | clinical.json | FHIR (profile) | Element / mã |
|---|---|---|---|
| Phiếu khám, tóm tắt: Họ tên, ngày sinh, giới | `patient.ho_ten/ngay_thang_nam_sinh/gioi_tinh` | Patient (`vn-core-patient`) | `name.text/family/given`, `birthDate` (ISO), `gender` Nữ→female |
| Tóm tắt: Số căn cước | `patient.so_cccd` | Patient | `identifier` type `vn-identifier-type-cs#CCCD` + `v2-0203#NI`, system `VN/sid/cccd` (12 số, `vn-cccd-format`) |
| Bảng kê: Mã thẻ BHYT | `coverage.so_the_bhyt` | Patient + Coverage | `identifier` type `BHYT`, system `VN/sid/bhyt` (`vn-bhyt-format`) |
| Mọi phiếu: Mã BN | `patient.ma_benh_nhan` | Patient | `identifier` type `MRN` + `v2-0203#MR`, system cơ sở `…/sid/mrn`, **assigner** = BV (`vn-patient-mrn-issuer-scoped`) |
| Tóm tắt/tờ điều trị: Mã bệnh án | `document_context.ma_benh_an` | Patient, Composition | `identifier` (system cơ sở) / `Composition.identifier` `VN/sid/document-number` + assigner |
| Phiếu khám: Dân tộc, nghề nghiệp, ngoại kiều | `patient.dan_toc/nghe_nghiep/ma_ngoai_kieu` | Patient | `vn-ext-ethnicity` (`vn-ethnicity-cs` 01), `vn-ext-occupation` (text), `patient-citizenship` ISO-3166 VN |
| Phiếu khám: Địa chỉ (Khu 7, Xã Bản Nguyên, Tỉnh Phú Thọ) | `patient.dia_chi.*` | Patient | `address.line/city/state/text`, ext `vn-ext-province` (`vn-province-cs` 25), `vn-ext-ward` (`vn-ward-cs` 08527) |
| Phiếu khám: Người nhà | `related_person` | RelatedPerson (base R4, VN Core chưa có profile) | `name.text`, `telecom`, `relationship.text` |
| Đầu phiếu: Sở Y tế / Bệnh viện | `organization.so_y_te/benh_vien` | Organization ×2 (`vn-core-organization`) | BV: `identifier` `VN/sid/organization-local-id` + assigner.reference (Sở), `vn-ext-org-type` BV, `vn-ext-org-level` TINH, `partOf` Sở; `type` organization-type `prov` |
| Phiếu khám: Khoa, mã khoa (K20) | `location.khoa/ma_khoa` | Organization (`vn-core-organization-department`) | `type` dept + `vn-department-cs#K20`, `partOf` BV (`vn-org-dept-partof-required`) |
| Phiếu khám: Buồng, giường (H036) | `location.phong/giuong` | Location ×2 (`vn-core-location`) | `physicalType` wa / bd, `identifier` `VN/sid/location-local-id` + assigner.reference, `managingOrganization` khoa |
| Chữ ký: BSCKI Cao Đắc Hiệp, ThS.Bs Vương Quang Uyển… | `practitioner.*` (parse_person: name, titles) | Practitioner (`vn-core-practitioner`) + PractitionerRole | `name.prefix/family/given`, `qualification.code` `vn-qualification-cs` (BS-CKI, ThS, BS, KTV…), KHÔNG dùng `vn-regulated-practitioner-title-cs` (đòi CCHN); Role: `organization` = khoa |
| Bảng kê: Mức hưởng, giá trị từ/đến, nơi ĐK KCB (25262) | `coverage.*` | Coverage (`vn-core-coverage`) | `type` insurance-coverage-type `medical` (fixed), `period`, `costToBeneficiary` 80 %, `vn-ext-bhyt-card-type` (`vn-bhyt-card-type-cs` GD), `vn-ext-primary-care-facility` (Reference.identifier `VN/sid/ma-cskcb`), `payor.display` BHXH VN |

## 2. Lượt khám chữa bệnh

| Nguồn | clinical.json | FHIR | Element / mã |
|---|---|---|---|
| Phiếu khám: Số khám bệnh 4001838 | `encounter.so_kham_benh` | Encounter (`vn-core-encounter`) | `identifier` type `MA_LUOT_KCB`, system `VN/sid/ma-luot-kcb` + assigner (`vn-encounter-visit-code-issuer-scoped`) |
| Phiếu khám: Số vào viện 08679/26 | `encounter.so_vao_vien` | Encounter | `identifier` hệ thống cơ sở |
| Loại KCB nội trú | `encounter.loai_kcb` 03 | Encounter | `class` v3-ActCode `IMP`; `type` `vn-encounter-type-cs#03` |
| Bảng kê: Đến khám / Kết thúc điều trị | `encounter.vao_vien_iso/ra_vien_iso` | Encounter | `period.start/end` (+07:00) |
| Bảng kê: Điều trị từ (vào khoa) | `encounter.vao_khoa.thoi_gian_iso` | Encounter | `location[].period` (khoa, giường) |
| Bảng kê: Tổng số ngày điều trị | `encounter.tong_so_ngay_dieu_tri` | Encounter | `length` 1 d (UCUM) |
| Phiếu khám: Lý do vào viện | `encounter.ly_do_vao_vien` | Encounter / Composition | `reasonCode.text`; section 10154-3 |
| Bảng kê: Trái tuyến ☑ | `encounter.tuyen` 3 | Encounter | `vn-ext-insurance-visit-type` (`vn-insurance-visit-type-cs` 3) |
| Bảng kê: Cấp cứu ☐ | `encounter.cap_cuu` | Encounter | `vn-ext-referral-mode` (`vn-referral-mode-cs` 1 Đến khám) |
| Bảng kê: Tình trạng ra viện "2" / tóm tắt "Đỡ, giảm" | `discharge.ma_ket_qua_dieu_tri` | Encounter | `vn-ext-treatment-outcome` (`vn-treatment-outcome-cs` 2) |
| Tờ điều trị: "Gia đình xin về" | `discharge.ma_hinh_thuc_ra_vien` 4 | Encounter | `hospitalization.dischargeDisposition` (`vn-discharge-disposition-cs` 4) |
| Chữ ký các phiếu | `practitioner.bac_si_dieu_tri/kham_benh/truong_khoa` | Encounter | `participant` v3-ParticipationType ATND / ADM / CON |

## 3. Chẩn đoán

| Nguồn | clinical.json | FHIR | Element / mã |
|---|---|---|---|
| Tóm tắt: Chẩn đoán vào viện (tách `/`, "TD" = theo dõi) | `diagnoses[type=AD]` | Condition (`vn-core-condition-diagnosis`) | `category` encounter-diagnosis (pattern), `code.coding` [`VN/CodeSystem/vn-icd10-cs` (bắt buộc), `http://hl7.org/fhir/sid/icd-10`], `verificationStatus` provisional/confirmed, `recordedDate`, `encounter`, `recorder` |
| Bảng kê: Chẩn đoán xác định + Mã ICD-10 (H81.8), Bệnh kèm theo (M50.2) | `diagnoses[type=DD]` (`code_source=document`) | Condition | như trên; mã HIS thắng mã LLM |
| Cả hai | — | Encounter.diagnosis | `use` diagnosis-role `AD`/`DD`, `rank` |
| Trích biên bản hội chẩn | `document 15` | ClinicalImpression (`vn-core-clinical-impression`) | `assessor` chủ toạ, `summary` diễn biến + kết luận, `finding.itemReference` → Condition, `supportingInfo` thành viên |
| Phiếu khám: Tiền sử gia đình "BT" | `family_histories` | FamilyMemberHistory (`vn-core-family-member-history`) | `relationship` v3-RoleCode FAMMEMB, `note` |

## 4. Sinh hiệu và khám

| Nguồn | clinical.json | FHIR | Element / mã |
|---|---|---|---|
| Phiếu khám "Mạch 85 lần/ph", tờ điều trị, bảng chăm sóc | `observations[type=sign]` | Observation (`vn-core-observation-heart-rate` + `-vital-signs`) | LOINC 8867-4, `valueQuantity` `/min` |
| Nhiệt độ 37 ºC | — | `…-body-temperature` | 8310-5, unit `°C` code `Cel` |
| Huyết áp 120/70 mmHg | `ucum.components` | `…-blood-pressure` | 85354-9; component 8480-6 / 8462-4, `mm[Hg]` (`vn-bp-component-value-or-dar`) |
| Nhịp thở, cân nặng, chiều cao, SpO2 | — | `…-respiratory-rate / body-weight / body-height / spo2` | 9279-1 `/min`, 29463-7 `kg`, 8302-2 `cm`, 2708-6 `%` |
| Khám toàn thân / bộ phận, triệu chứng | `clinical_finding.*` | Composition section 29545-1 (narrative) | — |

## 5. Xét nghiệm

| Nguồn | clinical.json | FHIR | Element / mã |
|---|---|---|---|
| Tờ điều trị `[Phiếu xét nghiệm:]` | `service_requests[loai=XN]` | ServiceRequest (`vn-core-service-request`) | `category` `vn-service-request-category-cs#XN`, `code` CLS/LOINC, `requester`, `reasonCode` ICD-10, `authoredOn` |
| Phiếu XN: Mã bệnh phẩm E003653317, loại mẫu, giờ nhận | `lab_reports.<key>` | Specimen (`vn-core-specimen`) | `identifier` `VN/sid/specimen-local-id` + assigner, `type` v2-0487 BLD/SER/UR, `receivedTime`, `collection.collectedDateTime` |
| Mỗi dòng chỉ số (tên, kết quả, ĐVT, BT, H/L, quy trình) | `lab_results[]` | Observation (`vn-core-observation-lab`) | `category` laboratory (pattern), `code.coding` LOINC (hoặc `vn-cls-cs`), `valueQuantity` UCUM (`vn-lab-observation-ucum`; pH→`[pH]`, tỷ trọng→`1`) / `valueString` / `dataAbsentReason`, `referenceRange.low/high/text`, `interpretation` v3 H/L, `method` (quy trình), `specimen`, `performer` KTV, `effectiveDateTime`, `issued` |
| Cả phiếu | `lab_reports.<key>` + `panels` | DiagnosticReport (`vn-core-diagnostic-report-lab`) | `category` v2-0074 LAB + `vn-cost-category-cs#3` (pattern), `code` LOINC panel (57021-8 CBC, 24323-8 sinh hoá, 50556-0 nước tiểu) + CLS, `result[]`, `specimen`, `resultsInterpreter`, `presentedForm` = PDF, `basedOn` ServiceRequest |

## 6. CĐHA / TDCN

| Nguồn | clinical.json | FHIR | Element / mã |
|---|---|---|---|
| Tờ điều trị `[Phiếu CĐHA:]` | `service_requests[loai=CDHA]` | ServiceRequest | `category` CDHA (TDCN cho điện não), `code` LOINC + `vn-cls-cs` |
| Phiếu MRI CSC / sọ não, siêu âm Doppler, điện não (kỹ thuật, mô tả, kết luận, BS chuyên khoa) | `diagnostic_reports[]` | DiagnosticReport (`vn-core-diagnostic-report-imaging`; điện não dùng `vn-core-diagnostic-report`) | `category` RAD + `vn-cost-category-cs#4` (TDCN: OTH + 5), `code` LOINC 24935-9/24590-2/43765-7/39289-4/11523-8 + CLS, `conclusion` = kết luận, `text` = kỹ thuật + mô tả, `resultsInterpreter`, `presentedForm` PDF |

## 7. Thuốc và chăm sóc

| Nguồn | clinical.json | FHIR | Element / mã |
|---|---|---|---|
| Tờ điều trị `[Đơn thuốc:] - Vitamin 3B extra (…) x 02 Viên / Uống, 8h34 16h` | `medication_requests[]` | MedicationRequest (`vn-core-medication-request`) | `medicationCodeableConcept` ATC A11DB + text, `category` inpatient, `authoredOn`, `requester`, `reasonReference` DD, `dosageInstruction` (`timing.repeat` 2/ngày `timeOfDay` 08:34:00/16:00:00, `route` SNOMED 26643006 + `vn-medication-route-cs#1.01`, `doseQuantity` 1 `{tbl}`), `dispenseRequest.quantity` |
| `[Chế độ ăn]`, `[Chế độ chăm sóc]` | `care_orders[]` | (chưa sinh resource; dự kiến NutritionOrder / CarePlan) | — |
| Phiếu chăm sóc: Tiền sử dị ứng "chưa ghi nhận" | — | AllergyIntolerance NKA (SNOMED 716186003) khi có ô này | — |

## 8. Tài liệu, chi phí, gói

| Nguồn | clinical.json | FHIR | Element / mã |
|---|---|---|---|
| Mỗi PDF không bị bỏ (16) | `document_reference.pdf[]` | DocumentReference (`vn-core-document-reference`) | `type` LOINC (34117-2 phiếu khám, 11502-2 XN, 18755-9 MR, 18760-9 siêu âm, 11523-8 điện não, 34746-8 chăm sóc, 11488-4 hội chẩn, 11506-3 tờ điều trị, 18842-5 tóm tắt, 51851-4 hành chính), `content.attachment` contentType/url/size/hash(SHA-1 b64)/title, `context.encounter/period`, `custodian` |
| Bảng kê chi phí (mục, chi tiết, quỹ BHYT/cùng chi trả/tự trả, tổng) | `claim.item[0][]`, `claim.tong_*` | Claim (`vn-core-claim`) | `identifier` `MA_LK` `VN/sid/ma-lk` + assigner, `type` institutional, `use` claim, `diagnosis.diagnosisCodeableConcept` ICD-10 VN (bắt buộc CodeableConcept), `insurance` 1 focal → Coverage, `item.category` `vn-cost-category-cs` (1, 2.2, 3, 4, 5, 8…), `item.detail` quỹ BHYT / cùng chi trả / tự trả, `total` VND, ext `vn-ext-claim-item-department` K20 |
| Bản tóm tắt HSBA + toàn bộ | `document_context`, mọi phần | Composition (`vn-core-composition`) | `type` 18842-5, `status` final + `attester` (người ký tóm tắt), `identifier` số bệnh án + assigner, `custodian` BV, `section[]` LOINC 10154-3, 11348-0, 29545-1, 29548-5, 30954-2, 18748-4, 8648-8, 10160-0, 47519-4, 18842-5, 51851-4 với `entry` trỏ tới các resource trên |
| — | — | Bundle (`vn-core-bundle`) | `document`: Composition đầu tiên, `fullUrl` = `<base_url>/<Type>/<id>`; `transaction`: thêm `request` PUT `<Type>/<id>` |

## 9. Những gì KHÔNG lấy được từ phiếu (phải cấu hình hoặc tra ngoài)

| Mục | Vì sao thiếu | Cách xử lý hiện tại |
|---|---|---|
| Mã CSKCB 5 ký tự của BVĐK tỉnh Phú Thọ | Không in trên 23 phiếu | `FACILITY["ma_cskcb"]` (None → chỉ có mã nội bộ) |
| Giấy phép hoạt động (profile `vn-core-kcb-organization`) | Không in | Dùng `vn-core-organization` |
| Số CCHN bác sĩ | Không in | Không dùng chức danh được quản lý; chỉ học vị `vn-qualification-cs` |
| Mã nghề nghiệp theo `vn-occupation-cs` | HIS in "00" | `vn-ext-occupation` dạng text |
| Mã tỉnh/xã | HIS in mã cũ (12/08) | Tra `vn-province-cs`/`vn-ward-cs` theo tên |
| Giờ lấy mẫu nước tiểu | Phiếu không in | Lấy giờ phiếu cùng đợt + `note` |
| Mã LOINC cho một số chỉ số địa phương | Không có trong bảng curated | LLM gợi ý + đối chiếu; còn `?` thì chỉ `code.text` (vẫn hợp lệ nếu có CLS) |
