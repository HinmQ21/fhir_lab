# 06 — Harness agent `fhir_agent`: HSBA → clinical v2 → FHIR VN Core, cắm được Gemini / OpenAI-compatible

Ngày: 08/10/2026. Nguồn chuẩn: skill `vn-fhir/skills/vn-fhir-convert` (schema clinical v2, 3 cổng chất lượng, "không bịa mã").
Harness biến luồng skill (vốn do Claude Code điều khiển) thành một **workflow xác định** gọi LLM có giới hạn,
chạy được với bất kỳ model nào qua API: GLM nội bộ, Gemini, OpenAI hoặc endpoint OpenAI-compatible (vLLM, Ollama, OpenRouter…).

## 1. Thiết kế

```
zip/PDF hoặc thư mục .txt
  │ 1 ingest      inventory.py của skill (phân loại form, trích chữ, phát hiện scan cần OCR) + giải mã font không ToUnicode (pdftext.py)
  │               → documents[] dựng XÁC ĐỊNH
  │ 2 extract     4 đợt nhiệm vụ LLM, mỗi nhiệm vụ = 1 lời gọi JSON, chạy song song trong đợt
  │                 W1 admin    (đọc toàn bộ)  patient, coverage, encounter, organizations, locations, practitioners
  │                 W2 clinical (toàn bộ)      conditions, encounter_diagnoses, allergies, family_history, clinical_impressions
  │                    orders   (tờ điều trị, hội chẩn) service_requests, medications, medication_requests, care_plans
  │                 W3 results:<doc>  specimens, diagnostic_reports, observations xét nghiệm/CĐHA/TDCN
  │                    observe:<doc>  sinh hiệu, khám, thang điểm, medication_administrations, care_plans điều dưỡng
  │                    cost:<doc>     cost_statement (bảng kê)
  │                 W4 summary:<doc>  compositions (bản tóm tắt / giấy ra viện)
  │               đợt sau nhận REGISTRY id của đợt trước → tham chiếu nhất quán, không trùng lặp giữa phiếu
  │ 3 assemble    gộp thực thể (trùng id/tên → 1, gộp sources, ánh xạ lại tham chiếu), chuẩn hoá ngày giờ/số/enum,
  │               LOẠI mọi mã do LLM tự ghi (chỉ giữ ICD-10 in trên phiếu đã kiểm), gỡ tham chiếu treo
  │ 4 coding      mã CHỈ từ tra cứu: VITALS (LOINC + UCUM), lab_loinc_candidates + CLS, LAB_PANELS / imaging_candidates,
  │               route SNOMED, mã ĐVHC; ICD-10 / LOINC ngoài bảng curated: LLM CHỌN chỉ số trong danh sách tra cứu;
  │               họ tên nhân viên phải có nguyên văn trên phiếu
  │ 5 cổng 1      quality_check.Q  → lỗi gom theo resource → LLM sửa đúng resource đó (kèm lỗi + văn bản phiếu nguồn)
  │               → chuẩn hoá + mã hoá lại → chỉ nhận khi số lỗi GIẢM (resource xấu đi bị hoàn tác)
  │ 6 cổng 2      build_fhir.Builder → validate_fhir (profile VN Core 0.10.0, invariant vn-*) → sửa như cổng 1
  │ 7 cổng 3      review.md (nguồn, quyết định mã, conflicts, unmapped, review_notes, kết quả cổng, PII) + scorecard nếu có GT
  ▼
runs/agent/<case>/<run>/ clinical.json · fhir/ (resources, bundle document/transaction/cost) · review.md · run.json · trace.jsonl
```

Nguyên tắc giữ chất lượng của skill:

| Nguyên tắc skill | Cách harness bảo đảm |
|---|---|
| Không bịa mã | Prompt cấm ghi mã; assemble xoá mọi coding không có cờ `_harness`; mã chỉ do `coding.py` thêm từ vnlib; LLM chỉ trả **chỉ số** trong danh sách ứng viên ICD |
| Mỗi resource có nguồn | `sources` mặc định = phiếu của nhiệm vụ; lọc theo id tài liệu thật |
| Không trùng lặp giữa phiếu | Nhiệm vụ theo thực thể (admin/clinical đọc toàn bộ) + REGISTRY + khử trùng theo tên chuẩn hoá |
| 3 cổng | Gọi đúng `quality_check`, `build_fhir`, `validate_fhir` của skill; vòng sửa có giới hạn (`quality.repair_rounds`) |
| Người duyệt cuối | `meta.status = extracted`, review.md liệt kê mọi suy luận và mã |

## 2. Cắm provider

`config/agent.yaml` → khối `providers`; khoá đọc từ `.env` qua `${BIẾN}` (hỗ trợ `${BIẾN:-mặc định}`), không in ra log.

| kind | Giao thức | Ghi chú |
|---|---|---|
| `openai_compat` | POST `/chat/completions` | `base_url` là gốc, `…/v1` hoặc URL đầy đủ; `json_mode: true` gửi `response_format: json_object`; `extra_body` gửi thêm tham số riêng (vd `chat_template_kwargs`) |
| `gemini` | `models/<model>:generateContent` | `responseMimeType: application/json`, `systemInstruction`, bỏ phần thought; `generation_config` gộp thêm (thinkingConfig…) |
| `anthropic` | POST `/v1/messages` (Claude, vd profile `haiku` = claude-haiku-5-5, khoá `CLAUDE_API_KEY`; profile `sonnet` = claude-sonnet-5-5) | không gửi `temperature` (model Claude mới báo deprecated); `stop_reason: max_tokens` coi như bị cắt; privacy `pseudonymize` |
| `mock` | đọc `fixtures/<task>.json` | kiểm thử offline |

Chung: thử lại theo backoff với 408/409/425/429/5xx/529 và timeout; `fallback_models` khi model chính quá tải;
parse JSON "lỏng" (bỏ markdown, lời dẫn, dấu phẩy thừa; chọn khối JSON lớn nhất) + 1 lời gọi sửa JSON;
kiểm hình dạng phản hồi theo nhiệm vụ (thiếu khoá / bị cắt → thử lại với nhắc nhở và max_tokens lớn hơn);
cache theo sha256(prompt, model, tham số) trong `runs/agent/_cache`; `trace.jsonl` ghi mỗi lời gọi (token, độ trễ, model) nhưng KHÔNG ghi nội dung.

Thêm provider mới: thêm một khối vào `providers` (OpenAI-compatible không cần viết code). Giao thức khác: kế thừa `LLMClient._call`.

## 3. Quyền riêng tư (PDPL 2023, NĐ 13/2023)

| privacy | Hành vi |
|---|---|
| `none` | endpoint nội bộ (GLM): gửi nguyên văn |
| `pseudonymize` | cloud: `PrivacyGuard` che họ tên (người bệnh, người nhà, nhân viên), CCCD, BHYT, SĐT, mã BN/bệnh án, ngày sinh, địa chỉ chi tiết bằng token nhất quán `[[P1]]`, `[[ID1]]`…, khôi phục sau khi nhận; bắt buộc cờ `--allow-cloud` |
| `block` | cấm gửi hồ sơ |

Che theo mẫu là giảm rủi ro, không phải ẩn danh tuyệt đối (tên không theo mẫu có thể sót). Hồ sơ thật chỉ chạy với GLM nội bộ;
Gemini được kiểm thử trên ca tổng hợp `tests/fixtures/synthetic-case/` (hư cấu hoàn toàn).

## 4. Chạy

```bash
python -m fhir_agent providers                         # liệt kê profile, có/thiếu khoá
python -m fhir_agent ping --provider gemini            # kiểm tra kết nối + JSON (câu hỏi tổng hợp)
python -m fhir_agent run data/sample/HSBA-BN001736827.zip --provider glm --gt gt/BN001736827/fhir
python -m fhir_agent run tests/fixtures/synthetic-case --provider gemini --allow-cloud --case SYNTH001
python tests/test_fhir_agent.py                        # kiểm thử offline (không gọi API)
```

Chạy lại các bước sau trích xuất (sau khi sửa code) mà không tốn LLM: `--replay runs/agent/<case>/<run>` (thêm `--replay-live` để vẫn gọi model cho choose/repair).

## 5. Kết quả (08–09/10/2026)

Ca thật BN001736827 (23 phiếu), so với ground truth `gt/BN001736827/fhir` bằng `compare.py`:

| Lần chạy | F1 | P | R | Cổng 1 (lỗi) | Cổng 2 (lỗi) | Resource |
|---|---|---|---|---|---|---|
| Pipeline cũ `fhir_pipeline` (mốc) | 0.68 | | | | | 167 |
| r2: GLM, harness bản đầu | 0.746 | 0.709 | 0.787 | 7 → 3 sau sửa | 3 → 1 sau sửa | 290 |
| r3-replay-final: trích xuất GLM của r3 + mọi bước xác định mới, không gọi LLM | **0.790** | 0.744 | 0.842 | **0** | 1 | 291 |
| haiku-r2b: Claude Haiku 5.5 (cloud, che PII; ca test, người dùng cho phép), trích xuất mới + bản sửa 09/10 | 0.779 | 0.734 | 0.831 | 11 → 0 sau sửa | 1 | 291 |
| sonnet-r2b: Claude Sonnet 5.5 (cloud, che PII), trích xuất mới + bản sửa 09/10 | 0.768 | 0.714 | 0.831 | **0** ngay lần đầu | **0** | 303 |

Theo loại (r3-replay-final): Patient 1/1, Encounter 1/1, ServiceRequest **18/18**, Practitioner **14/14**, DocumentReference 23/23,
Coverage/Claim/Invoice/MedicationRequest 1/1, DiagnosticReport 6/8, Observation 85/111, Condition 2/3 (bước LLM chọn ICD không chạy khi replay).
Lỗi cổng 2 còn lại: 1 dòng xét nghiệm nước tiểu cần bước `choose:loinc` (cần LLM).

Chi phí một ca (r2, GLM): 41 lời gọi, ~303k token vào, ~69k token ra, 311 s (trích xuất 230 s với 6 luồng).
Haiku 5.5: 40 lời gọi, ~486k token vào, ~225k token ra, 492 s (4 luồng). Sonnet 5.5: 24 lời gọi, ~344k vào, ~138k ra, 395 s.
Sửa sau khi chạy Claude: regex che tên dừng trước nhãn vai trò ("Hà Xuân Tài Thư ký:" không còn thành tên "Hà Xuân Tài Thư"); bỏ Practitioner
chỉ gồm chức danh ("Bác Sĩ Chuyên Khoa"); dòng chi phí thiếu `serviced` lấy thời gian đợt điều trị; drop_dangling gỡ cả documents.authors/authenticator;
prompt observe: giờ sinh hiệu phiếu khám = "Đến khám bệnh lúc", không phải giờ ký (chưa có trong lần chạy r2). Haiku chọn ICD đúng 3/3 Condition, Practitioner 14/14 nhưng thêm 2 tên sai
("Bác Sĩ Chuyên Khoa", "Hà Xuân Tài Thư"); Builder từng lỗi khi dân tộc chỉ có text → assemble tra mã vn-ethnicity-cs theo tên.
Ca tổng hợp với Gemini (`gemini-3.5-flash`, che PII): 11 lời gọi, ~62k token vào, ~24k token ra; replay offline: cổng 1 = 0 lỗi, 120 resource,
4 lỗi cổng 2 đều là mã xét nghiệm chờ `choose:loinc`.

Những cải tiến có tác động lớn nhất (đều là bước xác định, áp dụng được cho mọi hồ sơ):

| Vấn đề phát hiện khi chạy | Sửa |
|---|---|
| GLM viết suy luận vào `content` trước JSON → hết token, parser bắt nhầm `{…}` nhỏ | gửi `response_format: json_object` (ít hơn 12 lần token); parser chọn khối JSON lớn nhất; kiểm khoá theo nhiệm vụ + thử lại có nhắc |
| Model chép nguyên dòng bảng kê vào ghi chú → bị cắt ở max_tokens | luật "ghi chú một câu ngắn"; không cache phản hồi bị cắt |
| 5 phiếu CĐHA/TDCN in bằng font không có ToUnicode → text là chuỗi GID, LLM đoán sai họ tên ("Hà Khuêc Trung") | `pdftext.py` giải mã GID → Unicode theo cmap Times New Roman đúng kiểu chữ (thường/đậm/nghiêng) |
| Họ tên sai chính tả / dính học hàm ("BS&CKI …") | chuẩn hoá học hàm; kiểm họ tên phải có NGUYÊN VĂN trên phiếu, sai thì thay bằng tên gần nhất trên phiếu (≥ 0.8) + review_notes |
| ServiceRequest đơn chất không có mã | LOINC của chỉ định = LOINC của kết quả cùng hồ sơ ∩ ứng viên curated; CLS chọn theo ConceptMap |
| Xét nghiệm ngoài bảng curated (HCT L/L, NEU%, HbA1c) | ứng viên = curated ∪ LOINC ánh xạ từ dòng CLS → LLM chỉ chọn chỉ số (`choose:loinc`) |
| "Glucose mao mạch" bị nhận là mạch (sinh hiệu) | so alias theo biên từ + loại trừ glucose/mao mạch |
| Trường bắt buộc của build_fhir bị bỏ sót, null tường minh, section trùng, MA không tham chiếu Medication, Organization thiếu identifier | `ensure_required` + bỏ null + gộp section + tạo Medication + định danh nội bộ tạm (cùng quy ước GT) |
| 429 hết quota Gemini bị thử lại 139 lần | `QuotaExceeded` chuyển ngay model dự phòng; mỗi model chỉ thử 2 lần trước khi chuyển |

Quyền riêng tư: kiểm offline trên ca thật — 14/14 nhân viên y tế, họ tên người bệnh, CCCD, BHYT, mã BN đều bị che (0 rò rỉ);
không gửi hồ sơ thật tới cloud. Test `test_privacy_roundtrip` kiểm trên ca tổng hợp.

Kiểm thử: `python tests/test_fhir_agent.py` — 8 test, gồm đầu-cuối offline (replay phản hồi Gemini của ca tổng hợp) và giải mã font.

## 6. Việc còn mở

- Endpoint GLM từ 09/10 rớt kết nối với prompt > ~2k token (`RemoteProtocolError` sau ~20 s; prompt nhỏ vẫn chạy) — lỗi phía máy chủ;
  khi ổn định, chạy lại đầy đủ để có số đo với text đã giải mã font + bước `choose:*`.
- Key Gemini hiện hết quota mọi model flash (HTTP 429) — cần nâng hạn mức để chạy lại ca tổng hợp có vòng sửa.
- Observation khác GT chủ yếu ở cách đặt tên quan sát khám tự do ("khám tim" vs "tim") và sinh hiệu lặp ở phiếu chăm sóc — quy ước của GT,
  chưa tối ưu theo để tránh khớp quá mức một ca.
