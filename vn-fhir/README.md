# vn-fhir — Claude Code plugin: HSBA (TT 32/2023) → FHIR R4 VN Core 0.10.0

Chuyển một bộ hồ sơ bệnh án PDF Việt Nam thành bundle FHIR theo VN Core IG 0.10.0, với mã chuẩn (ICD-10 VN, LOINC,
CLS QĐ 1227, UCUM, danh mục `vn-*-cs`) và vòng lặp kiểm tra chất lượng bắt buộc. Thiết kế theo nguyên tắc: **script làm
phần xác định, Claude làm phần ngữ nghĩa, không ai bịa mã, mọi resource truy vết được về phiếu gốc.**

```
vn-fhir/
├── .claude-plugin/plugin.json      manifest
├── .mcp.json                       MCP "vn-terminology" (tuỳ chọn, cần pip install mcp)
├── hooks/hooks.json                PostToolUse: validate mỗi khi ghi tệp FHIR
├── agents/extractor.md             trích xuất theo nhóm phiếu
├── agents/reviewer.md              đọc chéo, săn lỗi ngữ nghĩa
├── agents/span-extractor.md        chế độ span: 1 văn bản -> mentions -> span_export
└── skills/vn-fhir-convert/
    ├── SKILL.md                    quy trình 7 bước (ngắn)
    ├── references/                 document-types · clinical-schema · coding-rules · vncore-profiles · quality-loop · pitfalls · span-extraction
    ├── assets/                     clinical.schema.json · loinc_verified.json · example/clinical.example.json (đã ẩn danh)
    └── scripts/
        vnlib.py          thư viện chung: vendor, danh mục, bảng curated (LOINC/UCUM/VITALS/DOC_TYPES/SECTIONS), parse ngày/tên
        inventory.py      zip/dir PDF -> manifest.json + 00_raw/*.txt + inventory.md (OCR?, phân loại, gợi ý danh tính)
        term_lookup.py    CLI tra cứu: icd10 · cls · lab · imaging · vital · cs · vs · ucum · loinc · doc-type · section · person · datetime
        term_server.py    cùng engine, dạng MCP server
        quality_check.py  cổng 1: schema, tham chiếu, mã, sinh hiệu, lab, thời gian, bảng kê, truy vết -> quality_report.md
        build_fhir.py     clinical.json -> resources/*.json + bundle-document / bundle-cost-statement / bundle-transaction
        validate_fhir.py  cổng 2: profile VN Core + ~40 invariant vn-* + LOINC/UCUM/date + bundle -> validation/report.md
        hook_validate.py  bọc validate_fhir cho hook
        compare.py        chấm candidate vs ground truth theo khoá ngữ nghĩa -> scorecard.md
        deidentify.py     ẩn danh nhất quán (HMAC) + dịch ngày, cho clinical.json và thư mục FHIR
        span_export.py    chế độ span: mentions (dòng + nguyên văn) -> vị trí ký tự, ICD-10 VN/WHO, RxNorm (RxNav cache); chấm kiểu airace
        span_profile.py   chế độ span: học thói quen GT tập dev (mã ICD/RxNorm, độ dài span, type, rank, mention thừa) rồi áp lên dự đoán
```

## Cài đặt

```bash
pip install jsonschema            # bắt buộc;  pip install mcp  # nếu muốn MCP server
claude plugin add ./vn-fhir       # hoặc --plugin-dir ./vn-fhir khi chạy claude
export VN_FHIR_VENDOR=/path/to/vendor   # chứa vncore/package, vncore/terminology, vncore/expansions, icd10/icd10_vn.json
```
Vendor mẫu nằm ở `fhir_lab/vendor/` (VN Core 0.10.0 + ICD-10 VN). Không có vendor thì các bước tra mã và validate không chạy.

## Chạy tay (không qua Claude)

```bash
S=vn-fhir/skills/vn-fhir-convert/scripts
python -I $S/inventory.py  data/HSBA-xxx.zip  out/xxx
# ... viết out/xxx/clinical.json theo references/clinical-schema.md (hoặc để Claude làm) ...
python -I $S/quality_check.py out/xxx/clinical.json --strict
python -I $S/build_fhir.py    out/xxx/clinical.json out/xxx/fhir
python -I $S/validate_fhir.py out/xxx/fhir
python -I $S/compare.py       out/xxx/fhir gt/xxx/fhir        # nếu có ground truth
python -I $S/deidentify.py    out/xxx/clinical.json out/xxx-deid --fhir-dir out/xxx/fhir
```

## Ground truth mẫu

`fhir_lab/gt/BN001736827/` — 23 phiếu, 248 resource, 0 lỗi validator, `review.md` ghi mọi quyết định. Sinh lại bằng
`python gt/BN001736827/make_gt.py` (các quyết định duyệt tay nằm trong script). Bản ẩn danh của cùng ca:
`assets/example/clinical.example.json`. **Thư mục `gt/` chứa dữ liệu cá nhân thật — không đưa ra ngoài.**

## Kiểm thử skill

`skills/vn-fhir-convert/evals/evals.json` liệt kê prompt + tiêu chí; chấm bằng `compare.py` với GT và `validate_fhir.py`
(0 lỗi). Thêm ca mới: chạy skill trên hồ sơ mới → duyệt tay bằng agent `reviewer` → lưu `gt/<case>/` (hoặc bản ẩn danh).

## Giới hạn

- Validator offline không kiểm binding tới SNOMED/LOINC/ATC toàn bộ và FHIRPath phức tạp; khi có Java dùng thêm HL7 validator
  với package VN Core.
- Phiếu scan cần OCR trước (inventory đánh dấu `needs_ocr`); plugin không tích hợp OCR.
- Bảng curated LOINC/CLS/UCUM phủ xét nghiệm & CĐHA thường gặp ở hồ sơ nội trú; mở rộng trong `vnlib.py` kèm nguồn kiểm chứng.

## Chế độ span (bộ đánh giá NER, vd airace)

```bash
S=vn-fhir/skills/vn-fhir-convert/scripts
python -I $S/span_export.py view   input/HSBA-BN-0009.txt                       # văn bản kèm số dòng
python -I $S/span_export.py locate input/HSBA-BN-0009.txt m.json pred/HSBA-BN-0009.json --report r.json
python -I $S/span_export.py score  groundtruth/HSBA-BN-0009.json pred/HSBA-BN-0009.json -v 20
python -I $S/span_profile.py learn --gt groundtruth --input input --pred dev_pred --exclude test_ids.txt -o profile.json
python -I $S/span_profile.py apply profile.json pred pred_prof --input input   # --parts icd,rx,trim,drop,type,rank
```
Bổ trợ: `vendor/icd10/icd10_who2019_supplement.json` (3 101 mã WHO 2019 mà bản ICD-10 VN thiếu: chương R, V–Z…),
`vendor/rxnorm/rxnav_cache.json` (cache tra RxNav theo tên hoạt chất — chỉ tên thuốc rời máy, không PII).
