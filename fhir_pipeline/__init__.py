"""fhir_pipeline — HSBA (zip PDF) -> JSON tài liệu TT32 -> clinical JSON -> FHIR R4 VN Core 0.10.0.

Model AI nằm sau một giao diện duy nhất (fhir_pipeline.llm.JsonLLM): đổi provider/model bằng config, không đổi code nghiệp vụ.
"""
from .config import Facility, PipelineConfig, load_config
from .llm import JsonLLM, LLMSettings, make_llm
from .pipeline import run_case

__all__ = ["Facility", "PipelineConfig", "load_config", "JsonLLM", "LLMSettings", "make_llm", "run_case"]
__version__ = "0.1.0"
