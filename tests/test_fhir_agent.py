"""Kiểm thử offline cho harness fhir_agent (không gọi API). Chạy: python -m pytest tests/test_fhir_agent.py -q"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fhir_agent import env, llm  # noqa: E402
from fhir_agent.assemble import _num  # noqa: E402
from fhir_agent.privacy import PrivacyGuard  # noqa: E402

SYN = ROOT / "tests" / "fixtures" / "synthetic-case"


def test_parse_json_loose():
    assert llm.parse_json_loose('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.parse_json_loose('Kết quả: {"a": [1, 2,]} xong') == {"a": [1, 2]}


def test_env_expand_default(monkeypatch):
    monkeypatch.setenv("FA_X", "abc")
    monkeypatch.delenv("FA_MISSING", raising=False)
    assert env.expand({"u": "${FA_X}/v1", "m": "${FA_MISSING:-gpt}", "e": "${FA_MISSING}"}) == {"u": "abc/v1", "m": "gpt", "e": ""}


def test_num():
    assert _num("50.600") == 50600
    assert _num("1.250.000") == 1250000
    assert _num("18,43") == 18.43
    assert _num("82") == 82
    assert _num("< 5") is None


def test_openai_url_normalisation():
    for base, want in [("http://h:8000", "http://h:8000/v1/chat/completions"), ("http://h/v1", "http://h/v1/chat/completions"),
                       ("http://h/v1/chat/completions", "http://h/v1/chat/completions"),
                       ("https://generativelanguage.googleapis.com/v1beta/openai", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")]:
        p = llm.Profile.from_dict("t", {"kind": "openai_compat", "base_url": base, "api_key": "k", "model": "m"})
        assert llm.make_client(p).url == want


def test_privacy_roundtrip():
    texts = [f.read_text(encoding="utf-8") for f in sorted(SYN.glob("*.txt"))]
    g = PrivacyGuard()
    for t in texts:
        g.learn(t)
    masked = g.mask(texts[0])
    for secret in ("NGUYỄN THỊ MẪU", "HT2259900112233", "025158009911", "0912000111", "14/03/1958", "Phạm Minh Thử", "Lê Văn Giả"):
        assert secret not in masked, secret
    # không che nhầm tiêu đề / số liệu lâm sàng
    for keep in ("PHIẾU KHÁM BỆNH VÀO VIỆN", "145/85", "Tỉnh Phú Thọ", "J18.1", "Viêm phổi"):
        assert keep in masked, keep
    out = g.unmask({"name": {"text": next(t for t, v in g.back.items() if v.startswith("Nguyễn Thị"))}, "list": ["x"]})
    assert out["name"]["text"] == "Nguyễn Thị Mẫu"


def test_mock_client(tmp_path):
    (tmp_path / "admin.json").write_text(json.dumps({"patient": {"id": "pat-1"}}), encoding="utf-8")
    c = llm.make_client(llm.Profile.from_dict("mock", {"kind": "mock", "model": "mock", "base_url": str(tmp_path)}))
    out, _ = c.json("admin", "s", "u")
    assert out["patient"]["id"] == "pat-1"


def test_replay_end_to_end(tmp_path):
    """Đầu-cuối không mạng: phát lại phản hồi trích xuất Gemini của ca tổng hợp -> assemble, coding, cổng 1, build, cổng 2, review."""
    from fhir_agent.runner import load_config, run_case
    cfg = load_config(ROOT / "config" / "agent.yaml")
    run = run_case(SYN, cfg, "mock", case="SYNTH001", out=tmp_path / "run", replay=ROOT / "tests" / "fixtures" / "synthetic-replay",
                   repair_rounds=0, use_cache=False, log=lambda *a: None)
    assert run["quality"]["errors"] == 0, run["quality"]
    assert run["fhir"]["resources"] >= 100
    rep = json.loads((tmp_path / "run" / "fhir" / "validation" / "report.json").read_text(encoding="utf-8"))
    errs = [i for i in rep["issues"] if i["severity"] == "error"]
    # còn lại chỉ được là mã xét nghiệm cần bước LLM choose:loinc (không chạy khi replay offline)
    assert all("vn-lab-observation-code-system" in i["message"] for i in errs), errs
    clin = json.loads((tmp_path / "run" / "clinical.json").read_text(encoding="utf-8"))
    texts = " ".join(f.read_text(encoding="utf-8") for f in SYN.glob("*.txt"))
    for p in clin["practitioners"]:
        assert p["name"]["text"] in texts, p["name"]             # họ tên nguyên văn trên phiếu
    for o in clin["observations"]:
        for c in (o.get("code") or {}).get("codings") or []:
            assert c.get("source") in ("curated", "lookup", "document", "conceptmap"), c   # không mã nào do LLM tự ghi
    assert (tmp_path / "run" / "review.md").exists()


def test_pdf_gid_font_decode():
    """Phiếu CĐHA in bằng font không ToUnicode: giải mã GID -> Unicode (chỉ chạy khi có ca mẫu + font Times)."""
    import zipfile
    from fhir_agent.pdftext import decode_pdf
    z = ROOT / "data" / "sample" / "HSBA-BN001736827.zip"
    if not z.exists():
        return
    with zipfile.ZipFile(z) as zf:
        pdfs = [m for m in zf.infolist() if m.filename.lower().endswith(".pdf")]
        decoded = [(m.filename, decode_pdf(zf.read(m))) for m in pdfs]
    hits = [(f, pages) for f, (pages, n) in decoded if pages]
    if not hits:
        return                       # máy không có font Times New Roman
    assert len(hits) == 5
    for f, pages in hits:
        t = "\n".join(pages)
        assert "Kết luận" in t or "KẾT LUẬN" in t.upper(), f
        assert "=" not in t, f


if __name__ == "__main__":      # chạy không cần pytest: python tests/test_fhir_agent.py
    import inspect
    import tempfile
    import traceback

    class _MP:
        def setenv(self, k, v):
            os.environ[k] = v

        def delenv(self, k, raising=True):
            os.environ.pop(k, None)

    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            kw = {}
            for a in inspect.signature(fn).parameters:
                kw[a] = _MP() if a == "monkeypatch" else Path(tempfile.mkdtemp())
            try:
                fn(**kw)
                print(f"PASS {name}")
            except Exception:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}\n{traceback.format_exc()}")
    sys.exit(1 if fails else 0)
