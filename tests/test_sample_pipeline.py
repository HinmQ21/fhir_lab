"""Test đầu-cuối trên ca mẫu với provider mock (phát lại fixture LLM, không gọi mạng).

Chạy: python -m unittest tests.test_sample_pipeline -v
Fixture: tests/fixtures/llm/*.json (ghi bằng llm.record_dir khi chạy thật). Thiếu fixture -> test báo prompt đã đổi.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
ZIP = ROOT / "data" / "sample" / "HSBA-BN001736827.zip"


@unittest.skipUnless(ZIP.exists() and FIXTURES.exists() and any(FIXTURES.glob("*.json")), "thiếu ca mẫu hoặc fixture LLM")
class SamplePipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fhir_pipeline import load_config, run_case
        cls.tmp = Path(tempfile.mkdtemp(prefix="fhir_lab_test_"))
        cfg = load_config(ROOT / "config" / "pipeline.yaml", {"llm": {"provider": "mock", "cache_dir": str(FIXTURES), "legacy_cache_dir": None, "record_dir": None},
                                                             "paths": {"output": str(cls.tmp)}})
        cls.cfg = cfg
        cls.res = run_case(ZIP, cfg)
        cls.out = cls.tmp / "BN001736827"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_documents_extracted(self):
        man = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        forms = [d["form"] for d in man["documents"] if d.get("data_file")]
        self.assertEqual(len(forms), 16)
        self.assertIn("ban-tom-tat-ho-so-benh-an", forms)
        self.assertFalse(any("PATIENT_MISMATCH" in w for d in man["documents"] for w in d.get("warnings", [])))

    def test_clinical(self):
        c = json.loads((self.out / "02_clinical" / "clinical.json").read_text(encoding="utf-8"))
        self.assertEqual(c["patient"]["ma_benh_nhan"], "BN001736827")
        self.assertEqual(c["encounter"]["so_kham_benh"], "4001838")
        self.assertEqual(c["encounter"]["vao_vien_iso"], "2026-02-02T08:24:00+07:00")
        codes = {(d["type"], d["code"]) for d in c["diagnoses"]}
        self.assertIn(("DD", "H81.8"), codes)
        self.assertIn(("DD", "M50.2"), codes)
        self.assertEqual(len(c["lab_results"]), 52)
        self.assertEqual(len(c["service_requests"]), 18)
        self.assertEqual(c["medication_requests"][0]["code"], "A11DB")

    def test_fhir_and_validation(self):
        counts = self.res["fhir"]
        self.assertEqual(counts["Composition"], 1)
        self.assertEqual(counts["Patient"], 1)
        self.assertGreaterEqual(counts["Observation"], 90)
        doc = json.loads((self.out / "03_fhir" / "bundle-document.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["entry"][0]["resource"]["resourceType"], "Composition")
        self.assertEqual(self.res["validation"]["errors"], 0, self.res["validation"]["issues_by_key"])


if __name__ == "__main__":
    unittest.main()
