# Workflow portable `fhir_pipeline` — chỉ cần thay core model là chạy

Gói `fhir_pipeline/` là bản chuẩn hoá của 4 bước làm tay trong `work/` (xem `docs/01_hanh_trinh_tung_buoc.md`).
Toàn bộ nghiệp vụ (phân loại, KIE theo template, chuẩn hoá, mã hoá, dựng FHIR VN Core, kiểm tra) **không biết** model nào đang chạy:
model nằm sau một giao diện duy nhất `JsonLLM.complete_json(system, user, images=None, max_tokens=...) -> dict`.

```
fhir_pipeline/
  llm/base.py          JsonLLM: cache theo hash yêu cầu, parse JSON "lỏng", nhờ model sửa JSON hỏng, thống kê token, ghi fixture
  llm/openai_compat.py vLLM / OpenAI / Azure / Ollama / LM Studio... (response_format json_object + extra_body)
  llm/anthropic.py     Claude Messages API (prefill "{" để ép JSON, ảnh base64) — chưa chạy thử vì lab không có key
  llm/mock.py          phát lại fixture (test, chạy offline)
  config.py            PipelineConfig = llm + facility + paths + rules (YAML/JSON, ${ENV})
  pdf.py               text PDF + giải mã font không ToUnicode + chỉ số "text đáng ngờ"
  extract.py           bước 1: zip -> 01_documents (+ reconcile khoá, PATIENT_MISMATCH, TEXT_SUSPECT)
  normalize.py         bước 2: JSON tài liệu -> clinical.json (+ mã hoá LLM có xác minh)
  terminology.py       LOINC/UCUM/ICD-10/CLS/ATC/sinh hiệu/ngày giờ VN (configure(vendor_dir))
  fhir_builder.py      bước 3: FhirBuilder(clinical, facility, vendor).build() -> resources + 2 Bundle
  validate.py          bước 4: validate_dir(fhir_dir, out_dir, vendor) theo snapshot VN Core
  pipeline.py          run_case(zip, cfg, steps=...) điều phối + run_summary.json
  cli.py               python -m fhir_pipeline run|resume|validate|llm-check
config/pipeline.yaml   cấu hình mẫu (GLM-5.3-Flash trên vLLM + BVĐK Phú Thọ)
tests/                 9 test: provider (stub HTTP) + đầu-cuối ca mẫu bằng mock (fixture tests/fixtures/llm)
```

## Chạy

```bash
export LLM_BASE_URL=http://<vllm-host>:<port>   # OpenAI-compatible
export LLM_API_KEY=...
python -m fhir_pipeline --config config/pipeline.yaml llm-check                       # gọi thử model
python -m fhir_pipeline --config config/pipeline.yaml run data/sample/HSBA-BN001736827.zip
python -m fhir_pipeline --config config/pipeline.yaml resume BN001736827 --steps fhir,validate   # chạy lại từ output
python -m fhir_pipeline --config config/pipeline.yaml validate output/BN001736827/03_fhir
python -m unittest discover -s tests -v                                                # offline, ~15 s
```

Kết quả nằm ở `output/<case>/`: `00_raw/`, `01_documents/`, `manifest.json`, `02_clinical/clinical.json`,
`03_fhir/{resources/, bundle-document.json, bundle-transaction.json}`, `04_validation/report.{json,md}`, `run_summary.json`
(thời gian từng bước, số lần gọi/token của LLM, lỗi kiểm tra).

## Thay model

Chỉ sửa khối `llm` trong YAML hoặc dùng `--set`:

| Mục tiêu                | Cấu hình                                                                                                                                     |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| vLLM khác (Qwen3, GLM…) | `provider: openai_compat`, `model: <served-name>`, `extra_body.chat_template_kwargs.enable_thinking: false` nếu model có thinking      |
| OpenAI                    | `provider: openai_compat`, `base_url: https://api.openai.com`, `api_key_env: OPENAI_API_KEY`, `model: gpt-4o-mini`, `extra_body: {}` |
| Claude                    | `provider: anthropic`, `model: claude-sonnet-5-5`, `json_mode: prefill`, `api_key_env: ANTHROPIC_API_KEY`                              |
| Chạy offline / test      | `provider: mock`, `cache_dir: tests/fixtures/llm`                                                                                          |

```bash
python -m fhir_pipeline -c config/pipeline.yaml --set llm.provider=anthropic --set llm.model=claude-sonnet-5-5 --set llm.json_mode=prefill run <zip>
```

Thêm provider mới = một lớp con `JsonLLM` cài `_call(messages, max_tokens, temperature, json_mode) -> (text, usage)` và
đăng ký trong `llm/__init__.py:make_llm`. Prompt, cache, parse, sửa lỗi, thống kê đã có sẵn ở lớp nền.

Yêu cầu tối thiểu với model: trả JSON đúng khung template ~3–6k token (phiếu XN nhiều dòng), đọc tiếng Việt tốt.
Với model nhỏ, **bắt buộc** có chế độ ép JSON (`response_format` hoặc prefill); tắt thinking khi trích xuất.

## Thay cơ sở KCB

Sửa khối `facility` (tên đầy đủ, Sở Y tế, mã tỉnh/xã theo `vn-province-cs`/`vn-ward-cs`, mã CSKCB 5 ký tự, URL hệ thống
định danh MRN/nhân viên/y lệnh). Luật phân loại tên file ở `rules` (hoặc truyền `classifier=` vào `run_case` để dùng
ClassifyRAG); template TT32 ở `paths.templates` — phiếu không có template sẽ được ghi `skip:no-template:<form>`.

## Cache và fixture

- Mỗi yêu cầu LLM được băm (model + system + user + ảnh + max_tokens + temperature + json_mode) → `cache_dir/<hash>.json`
  chứa cả request và response. Chạy lại không tốn token; đổi prompt/model → hash mới.
- `legacy_cache_dir` tái dùng cache của bước làm tay (`work/common.py`).
- `record_dir` ghi fixture để provider `mock` phát lại (đó là cách tạo `tests/fixtures/llm`, 17 yêu cầu, 336 KB).

## Giới hạn hiện tại / việc tiếp theo

1. Mới kiểm chứng trên **một ca** (nội trú ngoại thần kinh). Cần thêm ca với các phiếu khác (phẫu thuật, nội khoa, truyền dịch, nhiều đơn thuốc) để mở rộng `normalize.py` (hiện chỉ có mapper cho 13 loại phiếu) và các profile còn thiếu (Procedure, NutritionOrder, CarePlan, Immunization…).
2. PDF scan/ảnh chưa có đường OCR; `TEXT_SUSPECT` mới chỉ cảnh báo. Có thể nối ảnh trang (`pdf.pdf_page_png_b64`) vào `complete_json(images=...)` cho model vision.
3. Kiểm tra VN Core là bộ offline tự viết (0 lỗi trên ca mẫu); thuật ngữ ngoài (LOINC/SNOMED/UCUM/ATC) chưa kiểm. Khi có Docker/Java, chạy thêm HL7 validator với `-ig vendor/vncore/package`.
4. Anthropic provider chưa được chạy thật (chỉ test payload bằng stub).
5. Chốt chặn chất lượng mới có: so khoá template, tên người bệnh, text đáng ngờ. Nên thêm: so mã BN/ngày sinh, tỷ lệ null, kiểm tra tổng tiền bảng kê = tổng các mục.
