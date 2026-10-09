"""Điều phối 4 bước cho một ca: extract -> normalize -> fhir -> validate.

    from fhir_pipeline import load_config, run_case
    cfg = load_config("config/pipeline.yaml")
    summary = run_case("data/sample/HSBA-BN001736827.zip", cfg)
"""
from __future__ import annotations

import logging
import re
import shutil
import time
from pathlib import Path

from . import pdf as _pdf
from . import terminology as _t
from .config import PipelineConfig
from .extract import run_extract
from .fhir_builder import FhirBuilder
from .llm import make_llm
from .normalize import load_docs, normalize, summary as clinical_summary
from .util import dump, load
from .validate import validate_dir

log = logging.getLogger("fhir_pipeline")
ALL_STEPS = ("extract", "normalize", "fhir", "validate")


def prepare(cfg: PipelineConfig):
    _t.configure(cfg.path(cfg.paths.vendor))
    _pdf.configure_fonts(cfg.paths.fonts)


def case_id_from_zip(zip_path: str | Path) -> str:
    return re.sub(r"^HSBA-", "", Path(zip_path).stem)


def run_case(zip_path: str | Path | None, cfg: PipelineConfig, *, llm=None, case: str | None = None,
             steps: tuple[str, ...] = ALL_STEPS, progress=None) -> dict:
    """Chạy các bước được chọn. zip_path có thể None nếu bỏ qua bước extract (đã có output/<case>)."""
    prepare(cfg)
    case = case or (case_id_from_zip(zip_path) if zip_path else None)
    if not case:
        raise ValueError("cần zip_path hoặc case")
    out = cfg.path(cfg.paths.output) / case
    out.mkdir(parents=True, exist_ok=True)
    timings, result = {}, {"case": case, "out_dir": str(out)}
    need_llm = any(s in steps for s in ("extract", "normalize"))
    llm = llm or (make_llm(cfg.llm) if need_llm else None)

    if "extract" in steps:
        t0 = time.time()
        man = run_extract(zip_path, out, cfg, llm, progress=progress)
        timings["extract"] = round(time.time() - t0, 1)
        result["documents"] = [(d["seq"], d["form"], d.get("warnings", [])) for d in man["documents"]]
    if "normalize" in steps:
        t0 = time.time()
        man, docs = load_docs(out)
        try:
            rel = out.relative_to(cfg.root).as_posix()
        except ValueError:
            rel = out.as_posix()
        clinical = normalize(man, docs, llm, max_tokens_coding=cfg.rules.max_tokens_coding, raw_url_prefix=f"{rel}/00_raw")
        dump(clinical, out / "02_clinical" / "clinical.json")
        timings["normalize"] = round(time.time() - t0, 1)
        result["clinical"] = clinical_summary(clinical)
    if "fhir" in steps:
        t0 = time.time()
        clinical = load(out / "02_clinical" / "clinical.json")
        built = FhirBuilder(clinical, cfg.facility.as_dict(), cfg.path(cfg.paths.vendor), case).build()
        fhir = out / "03_fhir"
        shutil.rmtree(fhir / "resources", ignore_errors=True)
        for r in built["resources"]:
            dump(r, fhir / "resources" / f"{r['resourceType']}-{r['id']}.json")
        dump(built["document"], fhir / "bundle-document.json")
        dump(built["transaction"], fhir / "bundle-transaction.json")
        timings["fhir"] = round(time.time() - t0, 1)
        from collections import Counter
        result["fhir"] = dict(Counter(r["resourceType"] for r in built["resources"]))
        if built.get("warnings"):
            result["fhir_warnings"] = built["warnings"]
            dump({"warnings": built["warnings"]}, fhir / "build-warnings.json")
    if "validate" in steps:
        t0 = time.time()
        rep = validate_dir(out / "03_fhir", out / "04_validation", cfg.path(cfg.paths.vendor), case)
        timings["validate"] = round(time.time() - t0, 1)
        result["validation"] = {"errors": rep["errors"], "warnings": rep["warnings"], "issues_by_key": rep["issues_by_key"]}
    result["timings_s"] = timings
    if llm is not None:
        result["llm"] = {"provider": getattr(llm, "name", "?"), "model": llm.s.model, **llm.stats}
    dump(result, out / "run_summary.json")
    return result
