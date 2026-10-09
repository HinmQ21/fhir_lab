# fhir_lab — HSBA (zip PDF) → JSON tài liệu → clinical.json → FHIR VN Core

Chuyển hồ sơ bệnh án PDF sang FHIR R4 theo **VN Core IG 0.10.0**, với model AI thay được bằng cấu hình.

- Giai đoạn 1 (xong): làm tay từng bước trên ca mẫu → `work/step1..4`, nhật ký `docs/01_hanh_trinh_tung_buoc.md`, bảng ánh xạ `docs/02_anh_xa_vncore.md`.
- Giai đoạn 2 (xong): chuẩn hoá thành gói `fhir_pipeline/` (CLI + config YAML + provider LLM cắm được + test offline) → `docs/03_workflow_portable.md`.

```
data/sample/HSBA-BN001736827.zip     ca mẫu (23 PDF)            data/sample/clinical.json   mẫu định dạng trung gian
config/pipeline.yaml                  cấu hình (llm / facility / paths / rules)
config/templates/*.template.json      template TT32 cho từng loại phiếu
vendor/vncore/, vendor/icd10/         package hl7.fhir.vn.core#0.10.0 + từ điển ICD-10 VN
fhir_pipeline/                        gói portable (nguồn chuẩn để phát triển tiếp)
work/                                 các bước làm tay (giữ làm tư liệu; fhir_pipeline được sinh từ đây)
tests/                                unittest: provider LLM (stub) + đầu-cuối ca mẫu bằng mock
output/<case>/                        00_raw, 01_documents, 02_clinical, 03_fhir, 04_validation, run_summary.json
```

## Chạy nhanh

```bash
pip install httpx pymupdf fonttools pyyaml            # Python 3.12
export LLM_BASE_URL=http://<vllm>:<port>  LLM_API_KEY=...
python -m fhir_pipeline -c config/pipeline.yaml run data/sample/HSBA-BN001736827.zip
python -m unittest discover -s tests -v              # không cần mạng
```

Đổi model: sửa khối `llm` trong `config/pipeline.yaml` hoặc `--set llm.model=...` (OpenAI-compatible, Anthropic, mock).
Đổi cơ sở KCB: khối `facility`.

## Kết quả trên ca mẫu

16 phiếu trích xuất → 167 resource FHIR (Composition, Patient, Encounter, 4 Condition, 92 Observation, 8 DiagnosticReport,
18 ServiceRequest, 3 Specimen, MedicationRequest, Coverage, Claim, 3 Organization, 2 Location, 10 Practitioner, 3 PractitionerRole,
16 DocumentReference, ClinicalImpression, FamilyMemberHistory) → Bundle document + transaction; 0 lỗi theo bộ kiểm tra offline
(254 invariant VN Core, 93 binding); 17 lần gọi LLM cho cả ca.

## Harness agent `fhir_agent/` (08/10/2026)

Luồng skill vn-fhir-convert (clinical v2, 3 cổng, không bịa mã) đóng gói thành workflow xác định, cắm model qua API:
GLM nội bộ, Gemini, OpenAI hoặc endpoint OpenAI-compatible bất kỳ (`config/agent.yaml`, khoá trong `.env`).
Chi tiết, kết quả và quy tắc quyền riêng tư: `docs/06_fhir_agent_harness.md`.

```bash
python -m fhir_agent providers
python -m fhir_agent run data/sample/HSBA-BN001736827.zip --provider glm --gt gt/BN001736827/fhir
python -m fhir_agent run tests/fixtures/synthetic-case --provider gemini --allow-cloud   # cloud: chỉ dữ liệu tổng hợp hoặc đã che PII
```

## Plugin & ground truth (08/10/2026)

- `docs/04_vn_fhir_plugin_ground_truth.md` — plugin Claude Code `vn-fhir/` (skill vn-fhir-convert, scripts, hook, agents) và ground truth `gt/BN001736827/` (248 resource, 0 lỗi).
- `docs/05_span_mode_airace_pilot.md` — chế độ span (trích xuất thực thể có vị trí) + airace_eval_data: 0.808 trên 101 tệp (0.800 trên 9 tệp filter); thêm profile học từ GT dev: 0.852 trên filter.
