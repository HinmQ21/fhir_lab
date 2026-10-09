"""fhir_agent — harness chuyển HSBA (PDF, TT 32/2023) -> clinical JSON v2 -> FHIR R4 VN Core 0.10.0, cắm được nhiều LLM.

Dựng lại quy trình 7 bước của skill `vn-fhir/skills/vn-fhir-convert` thành workflow xác định:
script điều phối + công cụ của plugin (inventory, term_lookup/vnlib, quality_check, build_fhir, validate_fhir, compare);
LLM (OpenAI-compatible hoặc Gemini) chỉ làm phần ngữ nghĩa theo từng nhiệm vụ nhỏ, không bao giờ tự ghi mã chuẩn.

    python -m fhir_agent run data/sample/HSBA-BN001736827.zip --provider glm --gt gt/BN001736827/fhir
"""
__version__ = "0.1.0"
