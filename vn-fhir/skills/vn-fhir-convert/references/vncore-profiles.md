# VN Core IG 0.10.0 — ràng buộc profile mà builder/validator thực thi

Nguồn: `vendor/vncore/package/StructureDefinition-*.json` (differential) + invariant `vn-*`. Bảng này tóm tắt những
ràng buộc **ảnh hưởng tới dữ liệu đầu vào** (clinical v2); builder tự lo cấu trúc (slice, fixed value, meta.profile).
Base: `http://fhir.hl7.org.vn/core` → `/StructureDefinition/<id>`, `/CodeSystem/<id>`, `/ValueSet/<id>`, `/sid/<x>`.
Tra nhanh: `python -c "import json;d=json.load(open('vendor/vncore/package/StructureDefinition-<id>.json'));print([ (e['id'],e.get('min'),e.get('max')) for e in d['differential']['element']])"`.

## Hành chính

| Profile | Ràng buộc chính (1..1 = bắt buộc) | Clinical v2 phải có |
|---|---|---|
| vn-core-patient | identifier slice theo `type` (CCCD sid/cccd, BHYT sid/bhyt, BHXH, GKS, HC, MRN hệ thống riêng của cơ sở + assigner); inv **identifier-minimum**: ≥1 CCCD/HC/GKS **hoặc** MRN + ext force-majeure; ext ethnicity (CC, required vn-ethnicity-vs), occupation (extensible), patient-citizenship (sub-ext code ISO 3166), address vn-core-address với ext province/ward (valueCoding vn-province-cs / vn-ward-cs) | `identifiers[CCCD]` hoặc MRN; `ethnicity.code`; `address.province_code` (+ `ward_code`) |
| vn-core-encounter | identifier slice MA_LUOT_KCB (type + sid/ma-luot-kcb); subject 1..1; serviceProvider 1..1; ext referralMode (required vn-referral-mode-vs), treatmentOutcome, paymentMethod; insuranceVisitType legacy; hospitalization.dischargeDisposition extensible | `identifiers[MA_LUOT_KCB]`, `service_provider`, `referral_mode` |
| vn-core-organization | identifier 1..* (slice CSKCB = type CSKCB + sid/ma-cskcb; Local = sid/organization-local-id **+ assigner**); name 1..1; ext org-type/org-level/facility-care-level/rank-status | cơ sở: ≥1 identifier (CSKCB nếu biết, nếu không → `{"type":"INTERNAL","system":"<his>/sid/organization",...}`) |
| vn-core-organization-department | identifier slice localCode (sid/organization-local-id, type.text "Mã khoa cục bộ"); type **2..***: orgKind = organization-type `dept` + deptClass = vn-department-cs; name 1..1; partOf 1..1 ≠ self | `kind: department`, `department_code` (Kxx), `part_of`; `local_code` chỉ khi in |
| vn-core-location | status 1..1; name 1..1 | `name`, `kind` (clinic/ward/bed → physicalType) |
| vn-core-practitioner | identifier 1..* (CCCD / GPHN / CCHN — GPHN/CCHN cần ext practice-license-status); name 1..1; qualification slice RegulatedTitle (vn-regulated-practitioner-title-cs) / Education (vn-qualification-cs, **required**, coding 1..1) | ≥1 identifier (mã NV nội bộ tạm nếu không in); `qualifications[]` mã vn-qualification-cs |
| vn-core-practitioner-role | practitioner 1..1; code extensible vn-practitioner-title-vs; specialty extensible vn-specialty-vs | `roles[].organization`; `title_text`; `specialty[]` |
| vn-core-coverage | identifier BHYT fixed sid/bhyt; type coding fixed insurance-coverage-type `medical`; ext bhytCardType, primaryCareFacility (Reference Organization) | `bhyt_number`, `card_type.code`, `primary_care_facility` (org id), `period`, `benefit_level` |
| vn-core-document-reference | type 1..1 (LOINC); subject 1..1; inv document-number issuer-scoped | mọi `documents[]`: `type.code`/`form`, `date`, `sha1_b64`, `size_bytes` |
| vn-core-provenance | mọi phần tử MS, không constraint | builder sinh 1 Provenance / tài liệu từ `sources[]` |

## Lâm sàng

| Profile | Ràng buộc | Clinical v2 |
|---|---|---|
| vn-core-condition-diagnosis | category pattern encounter-diagnosis; code.coding slice **icd10vn 1..1** (vn-icd10-cs, required vn-icd10-vs); icd10who/snomedct/yhct tuỳ chọn; encounter 1..1; **recordedDate 1..1** | `code.codings[icd10vn]`, `recorded_date`; không có ICD ⇒ builder dùng vn-core-condition (code 1..1 text) |
| vn-core-observation-lab | category Laboratory; code.coding slice loinc / vnCls; subject 1..1; effective 1..1; valueQuantity.system fixed UCUM; inv lab-final-result-complete (final ⇒ value hoặc DAR), lab-observation-ucum, lab-observation-code-system (≥1 LOINC/CLS) | `kind: lab`, `effective`, `value.quantity.ucum` hoặc `value.string`/`absent`, ≥1 coding loinc/cls |
| vn-core-observation-vital-signs (+ heart-rate, body-temperature, blood-pressure, respiratory-rate, spo2, body-weight, body-height) | code.coding loinc required vn-vital-signs-codes-vs; **value.unit fixedString** ('/min', '°C', 'mmHg', '%', 'kg', 'cm') + code fixed UCUM; BP: component systolic 8480-6 / diastolic 8462-4 với mmHg; inv vital-value-or-dar, bp-component | `kind: vital-sign` + `vnlib.VITALS` (unit label = fixedString). BMI → vn-core-observation-vital-signs (39156-5, kg/m2) |
| Observation khác (exam, survey) | **không có profile VN Core** → Observation base; category exam/survey; code có thể text-only | `kind: exam|survey`, `code.text` |
| vn-core-diagnostic-report (+ -lab, -imaging) | category 1..* pattern (lab: LAB + cost 3; imaging: RAD + cost 4, specimen 0..0); code extensible vn-cls-vs / vn-lab-observation-code-vs / vn-imaging-report-code-vs | `kind: lab|imaging|functional`; imaging **không** có specimen |
| vn-core-service-request | code 1..1; category extensible vn-service-request-category-vs; ext referralMode | `code`, `category` (XN/CDHA/TDCN/…), `authored_on`, `requester` |
| vn-core-specimen | status/type/subject 1..1 | `type.code` (v2-0487), `status` |
| vn-core-medication-request | authoredOn 1..1; dosageInstruction 1..1; route extensible vn-medication-route-vs; ext paymentSource | `authored_on`, `dosage.text` (+ route vn_code/snomed), `payment_source` |
| vn-core-medication-administration | medication CC hoặc Reference; inv: completed + inpatient ⇒ medicationReference + context | `medication` (id), `effective`, `status: completed`, `category: inpatient` |
| vn-core-medication | code 1..1; identifier slice drugRegistrationNumber / gtin | `code.text` (+ ATC) |
| vn-core-allergy-intolerance | code 1..1 extensible vn-allergy-code-vs | `nka: true` (SNOMED 716186003) hoặc `code` |
| vn-core-care-plan | activity.detail.description MS; inv active ⇒ detail/reference | `activities[].description` |
| vn-core-clinical-impression / family-member-history / condition | theo base | `status`, `date` / `status`, `relationship` |
| vn-core-composition | identifier 1..1; subject/custodian 1..1; attester.party 1..1; section slice theo LOINC (10154-3 chief complaint, 11348-0 history, 29545-1 exam, 29548-5 dx → Condition, 30954-2 lab → Observation-lab/DiagnosticReport, 10160-0 meds → MedicationRequest/Medication, 47519-4 procedures, 18842-5 discharge) | `compositions.<key>`: `identifier`, `attesters[].party`, `sections[].key` (khoá `vnlib.SECTIONS`) + `entries` đúng loại |
| vn-core-bundle (document) | entry[0] = Composition; fullUrl duy nhất; mọi reference giải quyết được trong bundle | builder tự gom bao đóng + Provenance (target cắt theo bundle) |

## Bảng kê chi phí (cost statement) — 4 resource + bundle

| Profile | Ràng buộc | Clinical v2 `cost_statement` |
|---|---|---|
| vn-core-account | identifier MA_LK 1..1; type pattern PBILLACCT; subject/servicePeriod/owner 1..1; coverage.priority 1..1 | `ma_lk`, `period`, `account_status` |
| vn-core-claim | identifier MA_LK 1..1; subType extensible vn-patient-visit-type-vs; **diagnosis 1..***; item.extension department / insuranceCostInfo / linePaymentAllocation / paymentSource; item.category vn-cost-category-vs; inv line-payment-allocation-vnd / nonnegative, other-fund-breakdown, insurance-cost-ratio-and-lexical; ext claim-cost-summary (totalMedication… Money) | `diagnoses[]` (≥1, condition id), `claim_subtype`, `lines[]`, `totals` |
| vn-core-cost-statement-invoice | ext segment 1..* (sequence, coverage?, period start+end, benefitLevel); identifier MA_LK fixed sid/ma-lk; subject/date/issuer/account 1..1; lineItem 1..* với ext line (segmentSequence, category required vn-cost-category-vs, serviced date|Period, unit, hospitalUnitPrice…), sequence 1..1, priceComponent base 1..1 (amount VND); totalNet VND; inv segment-sequence-unique, line-segment-exists, line-unit-price-required, money-vnd, line-quantity-amount-required, **total-sum**, **line-allocation-sum**, issued-allocation-complete, uninsured-no-benefit, segment-benefit-range | `segments[]`, `lines[].sequence/segment/category/quantity/hospital_unit_price/hospital_amount/allocation{4 khoản}` (tổng = thành tiền), `serviced`, `totals.hospital_total` = Σ dòng |
| vn-core-cost-statement-composition | identifier 1..1; subject/custodian 1..1; attester 1..* với ext cost-statement-attestation-role (role required, attestedAt); section 1..* entry → Invoice; inv attestation-role-unique, **three-signatures** (final ⇒ preparer + billing-confirmation + patient-confirmation), time-consistent | `attestations[]` (role, party?, time); `composition_status` (preliminary nếu thiếu chữ ký) |
| vn-core-cost-statement-document-bundle | identifier.system fixed sid/ma-lk; type document; timestamp; entry sliced: composition 1..1, invoice 1..1, account 0..1, provenance 0..*; inv document-ma-lk, same-subject, signature-complete | builder gom; `ma_lk` phải trùng giữa Account/Claim/Invoice/Bundle |

## Extension (giá trị)

- `vn-ext-ethnicity`, `-occupation`, `-org-type`, `-org-level`, `-facility-care-level`, `-bhyt-card-type`, `-referral-mode`, `-treatment-outcome`, `-payment-method`, `-insurance-visit-type`, `-claim-item-department`, `-payment-source`: **valueCodeableConcept**.
- `vn-ext-primary-care-facility`: valueReference(Organization). `vn-ext-province` / `vn-ext-ward`: valueCoding.
- `vn-ext-insurance-cost-info` sub: scope (CC), paymentRatio (decimal), paymentRatioSourceLexical (string), benefitLevel, paymentCeiling (Money), servicePaymentRatio, insuranceUnitPrice (Money VND), insuranceLineAmount (Money VND).
- `vn-ext-line-payment-allocation` sub: insuranceFund / patientCopay / otherSource / patientSelfPay — **cả 4 bắt buộc**, Money VND, không âm.
- `vn-ext-cost-statement-segment` sub: sequence (positiveInt), coverage (Reference), period (start+end), benefitLevel. `vn-ext-cost-statement-line` sub: segmentSequence, category (CC), serviced (valueDate | valuePeriod), unit, hospitalUnitPrice, packageComponent, relatedServiceSequence.
- `vn-ext-claim-cost-summary` sub: totalMedication, totalSupplies, totalInsurance, totalPatientCopay, totalPatientSelfPay, totalOtherFund, totalNonCovered, totalInsuranceFeeForService (Money).
- `vn-ext-cost-statement-attestation-role` sub: role (code), attestedAt (dateTime). `vn-ext-practice-license-status` sub: status (Coding).

## Những gì validator offline KHÔNG kiểm (cần validator chính thức khi có Java)

Binding với ValueSet không có expansion offline (SNOMED, LOINC toàn bộ, ATC), FHIRPath phức tạp ngoài danh sách `INVARIANTS`,
XHTML narrative chi tiết, slicing theo discriminator `profile` lồng sâu. `report.json.stats` ghi số binding đã/không kiểm.
