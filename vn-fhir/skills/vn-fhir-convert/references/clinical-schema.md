# Clinical JSON v2 — lớp trung gian (schema: `assets/clinical.schema.json`)

Mục đích: tách **ngữ nghĩa** (Claude đọc phiếu, quyết định, tra mã) khỏi **cấu trúc FHIR** (`build_fhir.py` sinh ra
xác định, lặp lại được). Một tệp `clinical.json` đủ để dựng toàn bộ bundle; sửa gì cũng sửa ở đây rồi dựng lại.
Ví dụ đầy đủ đã ẩn danh: `assets/example/clinical.example.json` (248 resource khi dựng).

## Quy ước chung

- `id`: chữ thường, không dấu, `^[a-z0-9][a-z0-9.-]{0,63}$`, **ổn định** và có nghĩa: `pt-<mrn>`, `enc-<mã lượt>`, `doc-NN`,
  `cond-<role><n>-<slug>`, `obs-vs-<doc>-<MMDD>-<HHMM>-<key>`, `obs-lab-<panel>-<NN>-<slug>`, `sr-NN-<slug>`, `dr-<slug>`,
  `spc-<type>-<mã mẫu>`, `pr-<slug tên>`, `dept-<slug>`, `org-<slug>`, `loc-<slug>`, `mr-/ma-/med-<slug>`, `cp-<slug>`, `ci-<slug>`.
- `sources: ["doc-01", ...]` **bắt buộc** trên mọi resource lâm sàng (→ Provenance). Không có nguồn = không tạo resource.
- Mã hoá: `{"text": "<nguyên văn>", "codings": [{"system": "loinc", "code": "…", "display": "…", "source": "curated|lookup|document|conceptmap|llm|manual", "verified": true, "confidence": 1.0}]}`.
  `codings: []` hợp lệ. `system` là khoá `vnlib.SYS` hoặc URL đầy đủ.
- Thời gian: `YYYY-MM-DDTHH:MM:SS+07:00` hoặc `YYYY-MM-DD`; không có giờ ⇒ ngày; không có ngày ⇒ bỏ trường + note.
- Giá trị đo: `{"quantity": {"value": 18.43, "unit_raw": "G/L", "ucum": "10*9/L", "unit": "10^9/L"}}` | `{"string": "Negative"}` | `{"codeable": {...}}` | `{"absent": "not-performed"}` | `{"integer"|"boolean"|"datetime": …}`. Đúng 1 khoá.
- Tham chiếu giữa resource = chuỗi id (không phải `Type/id`); builder tra registry để gắn `resourceType`.
- `note` (string) trên bất kỳ resource → `Annotation`; `conflicts[] / unmapped[] / review_notes[]` ở gốc → `review.md` + Provenance config.

## Khối gốc

| Khối | Bắt buộc | Nội dung chính |
|---|---|---|
| `meta` | ✓ | `schema_version "2.0"`, `vncore_version`, `case_id`, `created`, `created_by`, `status` (draft/extracted/reviewed/ground-truth), `facility{organization, name, base_url, mrn_system, staff_system, order_system}`, `source_archive`, `deidentified` (do deidentify.py thêm) |
| `documents[]` | ✓ | `id, seq, file, form, title, type{code,display}, date, pages, size_bytes, sha1_b64, content_type, extracted, authors[] | author_org, note` |
| `organizations[]` | ✓ | `kind facility|department|payer`, `name, aliases, identifiers[], department_code, local_code, part_of, org_type, org_level, facility_care_level, address, telecom[]` |
| `locations[]` | | `kind clinic|ward|bed|room…`, `name, local_code, managing_organization, part_of` |
| `practitioners[]` | ✓ | `name{text,family,given}, name_raw, qualifications[mã], identifiers[], roles[{id, organization, title_code|title_text, specialty[], location, period}]` |
| `patient` | ✓ | `name, gender, birth_date, identifiers[{type,value,system?,text?,assigner?}], ethnicity{code,text}, occupation{code?,text}, nationality{code}, address{text,line[],ward,city,district,state,province_code,ward_code,country}, contacts[], managing_organization` |
| `coverage` | | `bhyt_number, card_type{code}, period, benefit_level (%), primary_care_facility (org id), payor (org id hoặc tên), status` |
| `encounter` | ✓ | `identifiers[], status, class IMP|AMB|EMER, type{code}, period, reason_text, service_provider, department, locations[{location,period,status}], participants[{individual,type ADM|ATND|CON…,period}], referral_mode, treatment_outcome, discharge_disposition(+_text), payment_method, length_days, admit_source_text, diagnoses[{condition,use AD|DD|CM|CC,rank}], account` |
| `conditions[]` | ✓ | `role admission|discharge|comorbidity|…, rank, text, code, clinical_status, verification_status, onset, recorded_date, recorder, asserter, note, candidates[]` |
| `observations[]` | ✓ | `kind vital-sign|lab|exam|survey|imaging, code, status, effective, issued, performer[], value | components[{code,value}], interpretation[H|L|N|A…], reference_range{low,high,unit,text}, specimen, based_on[], method_text, device_text, body_site, note` |
| `specimens[]` | | `type{code (v2-0487), text}, identifiers[], status, received, collected, collector, requests[], note` |
| `service_requests[]` | | `category XN|CDHA|TDCN|…, code, status, intent, priority, authored_on, occurrence, requester, performer, reason_reference[], specimen[], requisition, note` |
| `diagnostic_reports[]` | | `kind lab|imaging|functional|pathology, code, based_on[], status, effective, issued, performer[] (org/practitioner), results_interpreter[], specimen[] (không cho imaging), results[obs id], technique_text, findings[], conclusion, conclusion_codes[], presented_form (doc id), note` |
| `medications[]` | | `code (text + ATC), form{text}, ingredients[{item{text}, strength{value,unit_raw}, per}], note` |
| `medication_requests[]` | | `medication (id) | medication_code, status, intent, category inpatient|outpatient, authored_on, requester, payment_source, reason_reference[], dosage{text, route{text,snomed,snomed_display,vn_code}, timing{frequency,period,period_unit,time_of_day[],bounds}, dose{value,unit_raw,ucum,unit}}, dispense, use_period, note` |
| `medication_administrations[]` | | `medication, request, status, category, effective | effective_period, performer[], dosage, note` |
| `allergies[]` | | `nka true | code, text, clinical_status, verification_status, type, category[], criticality, recorded_date, recorder, reactions[], note` |
| `family_history[]` | | `status, relationship{text,codings}, sex, date, conditions[], note` |
| `clinical_impressions[]` | | `status, code_text, date, assessor, problems[], description, summary, findings[{reference|code, basis}], prognosis_text, notes[]` |
| `care_plans[]` | | `title, status, intent, category[{text}], period, created, author, contributors[], addresses[], description, activities[{kind, code{text}, description, status, scheduled_text, performer[], reason}], notes[]` |
| `cost_statement` | (nếu có bảng kê) | `ma_lk, so_bang_ke, form_name, period, prepared_at, prepared_by, claim_type, claim_subtype, payment_method, insurer, diagnoses[{condition,type,type_text}], segments[{sequence,coverage,period,benefit_level,title}], lines[], totals{hospital_total, insurance_total, insurance, patient_copay, other_fund, patient_self_pay, non_covered, medication, supplies, insurance_ffs}, total_in_words, attestations[{role,party,time}], composition_status, invoice_status, account_status, account_name, account_description` |
| `cost_statement.lines[]` | | `sequence, segment, category (vn-cost-category-cs), text, code{text,codings}, unit, quantity, hospital_unit_price, insurance_unit_price, service_payment_ratio, hospital_amount, insurance_payment_ratio, insurance_amount, allocation{insurance_fund, patient_copay, other_source, patient_self_pay}, department (Kxx), serviced (date | {start,end}), scope, benefit_level, payment_source` |
| `compositions{key}` | | `discharge_summary` (và key khác): `id, identifier{value,text,assigner}, status, type{code,display}, type_text, title, date, authors[], attesters[{mode,time,party}], custodian, confidentiality, sections[{key (vnlib.SECTIONS) | code, title, text_lines[], entries[]}]` |
| `conflicts[]` | | `{field, values[{value, source}], resolution, rationale}` |
| `unmapped[]` | | `{item, reason, action}` |
| `review_notes[]` | | chuỗi — mọi suy luận không có trên giấy |

## Ánh xạ sang FHIR (tóm tắt — chi tiết trong `build_fhir.py`)

documents→DocumentReference (+Provenance/doc) · organizations→Organization (+department profile) · locations→Location ·
practitioners→Practitioner + PractitionerRole/role · patient→Patient · coverage→Coverage · encounter→Encounter ·
conditions→Condition(-diagnosis nếu có icd10vn) · observations→Observation (profile theo `kind`+LOINC) · specimens→Specimen ·
service_requests→ServiceRequest · diagnostic_reports→DiagnosticReport(-lab/-imaging) · medications→Medication ·
medication_requests→MedicationRequest · medication_administrations→MedicationAdministration · allergies→AllergyIntolerance ·
family_history→FamilyMemberHistory · clinical_impressions→ClinicalImpression · care_plans→CarePlan ·
cost_statement→Account + Claim + Invoice + Composition(bảng kê) · compositions→Composition ·
Bundles: `bundle-document.json` (Composition tóm tắt + bao đóng), `bundle-cost-statement.json`, `bundle-transaction.json` (PUT tất cả).

## Mở rộng schema

Thêm trường mới: (1) đặt tên snake_case, ghi vào `clinical.schema.json`; (2) builder đọc với `.get()` và bỏ qua khi thiếu;
(3) cập nhật bảng này; (4) thêm 1 dòng vào `assets/example`. Không đổi nghĩa trường cũ — tăng `schema_version` minor.
