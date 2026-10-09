# Vòng lặp chất lượng (bắt buộc) — "chưa qua 3 cổng thì chưa xong"

```
inventory ─► clinical.json ─► quality_check (cổng 1) ─► build_fhir ─► validate_fhir (cổng 2) ─► review.md (cổng 3)
                 ▲                 │ lỗi                                   │ lỗi
                 └─────────────────┴───────────────────────────────────────┘   (sửa clinical.json, KHÔNG sửa tay FHIR)
```

## Cổng 1 — `quality_check.py clinical.json`  (chất lượng ngữ nghĩa)

| Kiểm tra | Mức | Sửa ở đâu |
|---|---|---|
| JSON Schema (`assets/clinical.schema.json`) | error | thiếu trường bắt buộc / sai kiểu / id sai định dạng |
| Tham chiếu id không tồn tại, id trùng | error | sửa id hoặc tạo resource thiếu |
| Mã không có trong CodeSystem / ICD-10 / CLS; LOINC sai check digit hoặc NLM báo không tồn tại | error | `term_lookup.py` chọn lại; không có ⇒ `codings: []` + `unmapped[]` |
| coding `source: llm` chưa `verified: true` | error | người duyệt xác nhận (đổi `source` → manual/lookup) hoặc bỏ |
| LOINC deprecated / chưa có trong bảng đã kiểm | warning / info | `term_lookup.py loinc <mã> --online` → thêm vào `assets/loinc_verified.json` |
| Sinh hiệu: LOINC ngoài VITALS, đơn vị ≠ fixedString, giá trị ngoài sinh lý | error / warning | dùng `vnlib.VITALS`; kiểm lại phiếu |
| Lab: thiếu UCUM, range đảo, cờ H/L không khớp giá trị | warning | **không tự sửa cờ** — giữ như phiếu, ghi `note`; chỉ sửa khi đọc nhầm |
| Dòng thời gian ngoài đợt điều trị, issued < effective, sinh sau vào viện, thẻ không phủ | warning / error | kiểm giờ in; nếu đúng như phiếu ⇒ `conflicts[]`/note |
| Bảng kê: thành tiền ≠ SL×giá, phân bổ ≠ thành tiền, tổng ≠ Σ dòng, quỹ ≠ BH×tỷ lệ×mức hưởng, final thiếu 3 chữ ký | error / warning | đọc lại từng đồng; bảng kê là chứng từ — số phải khớp |
| Thiếu `sources`, nguồn không tồn tại, tài liệu không được trích dẫn | error / info | mọi resource phải có nguồn |
| PII chưa ẩn danh | info | `deidentify.py` trước khi chia sẻ |

Thoát 1 khi có error. Chạy `--strict` để coi warning là lỗi (khuyến nghị cho ground truth).

## Cổng 2 — `validate_fhir.py <fhir_dir>`  (tuân thủ VN Core)

- Kiểm **mọi** resource theo `meta.profile`: cardinality, fixed/pattern, slice, binding required/extensible với VS có expansion
  offline, ~40 invariant `vn-*` (identifier-minimum, three-signatures, total-sum, line-allocation-sum, lab-observation-ucum…),
  định dạng date/dateTime/time, LOINC check digit + bảng đã kiểm, UCUM, narrative, tham chiếu (trong thư mục / trong bundle),
  bundle document (Composition đầu tiên, fullUrl duy nhất, entry của section tồn tại).
- Hook PostToolUse (`hooks/hooks.json` → `hook_validate.py`) tự chạy cổng này mỗi khi ghi tệp `*.json` có `resourceType`:
  có lỗi ⇒ exit 2, danh sách lỗi hiện trong ngữ cảnh ⇒ phải sửa ngay. Tắt tạm: `VN_FHIR_HOOK=off`.
- Lỗi ở cổng 2 hầu như luôn là lỗi dữ liệu đầu vào (thiếu `recorded_date`, sai `unit`, thiếu identifier) ⇒ sửa `clinical.json`
  rồi dựng lại. Chỉ sửa `build_fhir.py` khi lỗi lặp trên nhiều ca (và thêm test).
- `report.json.stats`: `binding-external-or-uncomputable` là số binding không kiểm được offline — ghi vào review.md.

## Cổng 3 — `review.md` (người đọc được)

Bắt buộc có các mục: (1) nguồn từng khối dữ liệu (bảng resource ↔ doc-NN); (2) quyết định mã hoá có lý do
(`conditions[].candidates`, CLS khối nào, LOINC theo đơn vị); (3) `conflicts` (giá trị, nguồn, cách giải quyết);
(4) `unmapped` + hành động; (5) `review_notes` — mọi suy luận không có trên giấy (chuyên khoa, cấp KCB, subType, giờ thực hiện);
(6) kết quả 2 cổng trước (số lỗi/cảnh báo, binding không kiểm); (7) cảnh báo PII.
Template: `gt/BN001736827/review.md` trong repo mẫu.

## Đo lường — `compare.py <candidate_fhir> <gt_fhir>`

Precision/recall theo khoá ngữ nghĩa từng loại resource (không theo id) + diff trường trên resource khớp → `scorecard.md`.
Dùng khi: so pipeline tự động với GT, hồi quy sau khi sửa builder (GT vs GT phải F1 = 1.0), chấm bài test skill (`evals/`).
Ngưỡng gợi ý cho một ca "đạt": F1 ≥ 0.95 trên Observation/Condition/ServiceRequest/DiagnosticReport, Claim/Invoice khớp từng dòng.

## Khi nào dừng

Dừng khi: cổng 1 = 0 error, cổng 2 = 0 error, mọi warning còn lại được giải thích trong review.md, `conflicts`/`unmapped` đã
liệt kê đủ, và (nếu có GT) scorecard đạt ngưỡng. Không "làm sạch" bằng cách xoá resource hay bỏ mã để hết lỗi — nếu một
resource không thể hợp lệ vì thiếu dữ liệu, ghi `unmapped` và bỏ resource đó **có chủ đích, có ghi chú**.
