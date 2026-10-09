# 04 — Plugin `vn-fhir` (skill cho Claude Code) và bộ ground truth BN001736827

Ngày 08/10/2026. Bước tiếp theo sau pipeline portable (03): thay vì một pipeline LLM "đoán" mã, xây một **skill** để Claude
Code chuyển hồ sơ bất kỳ theo đúng quy trình, với script xác định cho phần cấu trúc và vòng lặp kiểm tra bắt buộc; đồng thời
tạo **ground truth** duyệt tay cho ca mẫu để đo mọi phiên bản tự động.

## Vị trí

| Thứ | Đường dẫn |
|---|---|
| Plugin | `vn-fhir/` (README có cây thư mục + cách chạy tay) |
| Skill | `vn-fhir/skills/vn-fhir-convert/SKILL.md` + `references/` + `scripts/` + `assets/` |
| Ground truth | `gt/BN001736827/` — `make_gt.py` → `clinical.gt.json` → `fhir/` (248 resource) + `review.md` + `quality_report.md` |
| Ví dụ ẩn danh | `vn-fhir/skills/vn-fhir-convert/assets/example/clinical.example.json` |

## Quy trình 7 bước (tóm tắt — chi tiết trong SKILL.md)

inventory (kiểm kê PDF, OCR?, phân loại, danh tính) → đọc toàn bộ phiếu, chốt bộ khung → viết `clinical.json` (schema v2) →
mã hoá bằng `term_lookup.py` (ICD-10 VN, LOINC curated nhạy đơn vị, CLS QĐ 1227, UCUM, vn-*-cs) → cổng 1 `quality_check.py` →
`build_fhir.py` + cổng 2 `validate_fhir.py` (hook tự chạy) → `review.md` (+ `compare.py` với GT).

## Kết quả trên ca mẫu

| Chỉ số | Giá trị |
|---|---|
| Resource / bundle | 248 / 3 (document 152 entry, cost-statement 43, transaction 248) |
| Validator VN Core | 0 lỗi, 3 cảnh báo (LOINC 21000-5 deprecated), 38 profile, 330 invariant |
| quality_check --strict | 0 lỗi, 2 cảnh báo có chủ đích (xem review.md §5) |
| compare GT vs GT | F1 1.0 |
| compare pipeline cũ (mock) vs GT | F1 0.68 — thiếu Provenance/Invoice/Account/CarePlan/MedAdmin, Practitioner không khớp |
| Mã sửa so với bản v1 | 20+ LOINC (# vs %, molar vs mass, check digit, deprecated), 1 ICD (M50.1 → M50.2), kết luận siêu âm bịa → nguyên văn |

## Nguyên tắc đã mã hoá vào skill

1. Không bịa mã: coding phải có `source` ∈ document/lookup/curated; `llm` bị chặn tới khi người duyệt xác nhận.
2. Giữ nguyên văn (`text`, `unit_raw`) cạnh mã; không có mã ⇒ `codings: []` + `unmapped`.
3. Mọi resource có `sources` ⇒ Provenance theo tài liệu gốc.
4. Mâu thuẫn ghi vào `conflicts[]` với nguồn; ưu tiên bảng kê / tóm tắt HSBA.
5. Sửa `clinical.json`, không sửa tay FHIR; hook validator chặn tệp FHIR lỗi.
6. PII: `gt/`, `output/` chứa dữ liệu thật — chỉ nội bộ; `deidentify.py` trước khi chia sẻ (PDPL 2023).

## Việc còn lại

- Tạo 20–30 ca tổng hợp + bundle đáp án để chạy `evals/evals.json` ở quy mô; đo F1 theo loại resource.
- OCR cho phiếu scan (inventory đã đánh dấu `needs_ocr`).
- Validator HL7 chính thức (Java) khi có môi trường; so với validator offline.
- Mở rộng bảng curated (CT, X-quang, nội soi, phẫu thuật, giấy ra viện/chuyển tuyến) kèm nguồn kiểm chứng.
- Chạy lại `fhir_pipeline` trên ca mẫu để cập nhật `output/BN001736827` (hiện vẫn là bản v1 với mã cũ).
