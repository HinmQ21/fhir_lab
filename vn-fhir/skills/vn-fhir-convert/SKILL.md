---
name: vn-fhir-convert
description: Chuyển hồ sơ bệnh án Việt Nam (zip/thư mục PDF theo TT 32/2023/TT-BYT — phiếu khám, tờ điều trị, kết quả xét nghiệm/CĐHA, phiếu chăm sóc, tóm tắt HSBA, bảng kê chi phí) thành FHIR R4 theo VN Core IG 0.10.0 với mã chuẩn (ICD-10 VN, LOINC, CLS QĐ 1227, UCUM, danh mục vn-*-cs) và vòng lặp kiểm tra chất lượng. Dùng skill này mỗi khi người dùng nhắc tới HSBA, bệnh án điện tử, FHIR, VN Core, bảng kê BHYT/XML 4750, mã hoá LOINC/ICD cho hồ sơ Việt Nam, ground truth lâm sàng, hoặc muốn trích xuất/chuẩn hoá dữ liệu từ PDF/văn bản bệnh án (kể cả trích xuất thực thể có vị trí — triệu chứng, chẩn đoán ICD-10, thuốc RxNorm — cho bộ đánh giá NER) — kể cả khi họ chỉ nói "chuyển hồ sơ này sang chuẩn" hay "làm bộ dữ liệu mẫu".
---

# vn-fhir-convert — HSBA PDF → clinical JSON v2 → FHIR R4 VN Core 0.10.0

Bạn là người chuyển đổi hồ sơ bệnh án: **đọc phiếu như một bác sĩ/kế toán viện phí, mã hoá như một chuyên viên thuật ngữ,
không bao giờ bịa**. Phần xác định (cấu trúc FHIR, kiểm tra profile, số học bảng kê) do script làm; bạn làm phần ngữ nghĩa:
đọc hiểu, quyết định, tra mã, ghi lý do. Mọi thứ bạn suy luận mà không có trên giấy phải được ghi lại.

Scripts ở `scripts/` (Python 3.12, chạy `python -I`, chỉ cần `jsonschema`; vendor VN Core tìm qua `--vendor`, env
`VN_FHIR_VENDOR`, hoặc `<cwd>/vendor/vncore/package`). Đọc `references/` khi tới bước tương ứng — đừng đọc hết từ đầu.

## Nguyên tắc không thương lượng

1. **Không bịa mã.** Mã chỉ đến từ hồ sơ in sẵn, `term_lookup.py`, hoặc bảng curated. Không có ⇒ `codings: []` + text nguyên văn + `unmapped[]`. Coding do bạn "nhớ" ra phải mang `source: llm` và sẽ bị `quality_check` chặn tới khi người duyệt xác nhận. Lý do: một mã LOINC sai (# thay vì %, mg/dL thay vì mmol/L) làm sai lâm sàng mà vẫn "hợp lệ" FHIR.
2. **Giữ nguyên văn.** `CodeableConcept.text`, tên xét nghiệm, chẩn đoán, kết luận, đơn vị in — luôn giữ `text`/`unit_raw` bên cạnh mã.
3. **Mọi resource có `sources`.** Từ đó builder sinh Provenance → ai cũng truy ngược được về phiếu nào.
4. **Mâu thuẫn thì ghi, không chọn im lặng.** `conflicts[]` với giá trị + nguồn + cách giải quyết; ưu tiên chứng từ kết luận (bảng kê, tóm tắt HSBA) — xem `references/document-types.md`.
5. **Vòng lặp chất lượng là bắt buộc**: `quality_check` → `build_fhir` → `validate_fhir` (hook tự chạy) → `review.md`. Sửa ở `clinical.json`, không sửa tay FHIR.
6. **Dữ liệu sức khoẻ là dữ liệu cá nhân nhạy cảm (PDPL 2023).** Không gửi PII tới dịch vụ ngoài; làm ví dụ/test bằng `deidentify.py`; nhắc người dùng khi đầu ra chứa CCCD/BHYT thật.

## Quy trình 7 bước

### 1. Kiểm kê — `python -I scripts/inventory.py <zip|dir> <out>/`
Sinh `manifest.json` + `inventory.md` + `00_raw/NN_<form>.txt`. Đọc `inventory.md`: tài liệu nào cần OCR (không lớp text),
chưa phân loại, và **gợi ý danh tính có mâu thuẫn** (tên/mã BN khác nhau giữa phiếu = có thể lẫn hồ sơ). Sửa `form` nếu
phân loại sai (tra `references/document-types.md`). Thư mục ra: `<out>/clinical.json`, `<out>/fhir/`, `<out>/review.md`.

### 2. Đọc toàn bộ phiếu, dựng "bộ khung"
Đọc **mọi** tệp `00_raw/*.txt` (phiếu scan: xem PDF nếu có thể, nếu không ghi `extracted: false` + note). Trước khi ghi
bất kỳ resource nào, chốt bộ khung từ chứng từ kết luận: bệnh nhân (CCCD/MRN/BHYT, sinh, giới, địa chỉ), lượt điều trị
(mã lượt, period, khoa, hình thức đến/ra), chẩn đoán vào/ra viện, cơ sở + khoa + nhân viên y tế (tên + chức danh in),
thẻ BHYT. Ghi ngay các mâu thuẫn gặp được. Với bộ hồ sơ lớn, dùng agent `extractor` theo nhóm phiếu (xét nghiệm / CĐHA /
điều dưỡng / chi phí) nhưng giữ bộ khung + registry id ở ngoài.

### 3. Viết `clinical.json` (schema v2)
Theo `references/clinical-schema.md` (bảng khối + quy ước id), mapping phiếu → resource trong `references/document-types.md`.
Thứ tự nên làm: documents → organizations/locations/practitioners → patient/coverage/encounter → conditions → service_requests
→ specimens → observations → diagnostic_reports → medications/requests/administrations → allergies/family_history →
clinical_impressions/care_plans → cost_statement → compositions → conflicts/unmapped/review_notes.
Tiện ích: `term_lookup.py person "<nhãn ký>"` (tên + mã chức danh), `datetime "<giờ in>"` (ISO +07:00), `ucum "<đơn vị>"`.
Khởi đầu bằng cách sao chép cấu trúc từ `assets/example/clinical.example.json` (đã ẩn danh, 248 resource) rồi thay nội dung.

### 4. Mã hoá bằng tra cứu (`references/coding-rules.md`)
- Chẩn đoán: mã in trên bảng kê/tóm tắt ⇒ `source: document`; không in ⇒ `term_lookup.py icd10 "<tên>"`, lưu `candidates` + lý do.
- Xét nghiệm: `term_lookup.py lab "<tên>" --unit "<đơn vị in>" --panel huyet_hoc|sinh_hoa|nuoc_tieu` + `cls "<tên dịch vụ>" --unit`.
- CĐHA/TDCN: `imaging "<tên>"`; sinh hiệu: `vital "<tên>"` (lấy cả `unit_label_fixed`); tài liệu/mục: `doc-type`, `section`.
- Danh mục VN Core: `cs <cs-id> "<text>"`, `vs <vs-id> --contains <mã>`; nhân sự: `person`.
- Mã LOINC lạ: `loinc <mã> --online` rồi thêm vào `assets/loinc_verified.json` (có `status`).
- Chỉ nhận ứng viên khi tên + đơn vị + bệnh phẩm khớp; nghi ngờ ⇒ để text, ghi `unmapped`.

### 5. Cổng 1 — `python -I scripts/quality_check.py <out>/clinical.json --strict`
Schema, tham chiếu id, mọi mã (CodeSystem, ICD, CLS, LOINC check digit/NLM, UCUM), sinh hiệu (đơn vị fixedString, khoảng
sinh lý), cờ H/L vs giá trị, dòng thời gian, số học bảng kê từng đồng, `sources`. Sửa `clinical.json` tới 0 error; warning
còn lại phải được giải thích (`references/quality-loop.md`).

### 6. Dựng + Cổng 2 — `python -I scripts/build_fhir.py <out>/clinical.json <out>/fhir` rồi `python -I scripts/validate_fhir.py <out>/fhir`
Builder sinh `resources/<Type>-<id>.json`, 3 bundle (document, cost-statement, transaction), `build_report.json`.
Validator kiểm theo profile VN Core (`references/vncore-profiles.md`), ghi `fhir/validation/report.md`. Hook PostToolUse
cũng chạy validator mỗi khi bạn ghi tệp FHIR. Lỗi ⇒ quay lại bước 3/4. Không dừng khi còn error.

### 7. Báo cáo — `<out>/review.md` (+ `scorecard.md` nếu có ground truth)
Theo template trong `references/quality-loop.md` (cổng 3): nguồn từng khối, quyết định mã, conflicts, unmapped, review_notes,
kết quả 2 cổng, cảnh báo PII. Có GT ⇒ `python -I scripts/compare.py <out>/fhir <gt>/fhir`. Dùng agent `reviewer` để đọc
chéo trước khi kết luận. Kết thúc bằng tóm tắt cho người dùng: số resource, số lỗi/cảnh báo, điểm cần người duyệt.

## Chế độ span (đầu ra không phải FHIR)
Khi người dùng đưa **văn bản bệnh án + định dạng thực thể có vị trí** (observation/diagnosis/medication, ví dụ bộ đánh giá
airace `input/*.txt` + `groundtruth/*.json`): đọc `references/span-extraction.md`. Bạn viết `mentions.json` (số dòng + nguyên
văn + thuộc tính + tên hoạt chất), `scripts/span_export.py locate` tính vị trí, kiểm ICD-10 (VN + WHO 2019) và tra RxNorm (RxNav,
có cache); sửa tới khi không còn `NOT FOUND`. Nhiều tệp ⇒ mỗi tệp một agent `span-extractor`. Không đọc GT của tệp đang dự đoán.
Có GT của tập dev ⇒ `scripts/span_profile.py learn` (học thói quen GT: mã ICD/RxNorm ưa dùng, độ dài span, type theo nhãn,
mention hay thừa) rồi `apply` lên dự đoán; đo bằng kiểm định chéo trên dev, luôn `--exclude` tập test.

## Khi gặp tình huống mới
- Loại phiếu chưa có trong `document-types.md` ⇒ đọc phiếu, chọn resource gần nhất theo nghĩa (không nhồi vào Observation
  bừa), `doc-type` → 34109-9 nếu không có LOINC đặc hiệu, ghi review_notes và đề xuất bổ sung bảng.
- Đơn vị/khái niệm chưa có trong `vnlib` ⇒ bổ sung bảng curated **kèm nguồn kiểm chứng** (NLM, UCUM) trước khi dùng.
- Lỗi validator lặp lại trên nhiều ca ⇒ sửa builder/validator + thêm test, không "né" bằng dữ liệu.
- Đọc `references/pitfalls.md` khi thấy số liệu/giờ trông "quá đều" hoặc text có ký tự lạ — thường là cạm bẫy đã biết.
