"""Cấu hình pipeline (YAML/JSON) — mọi thứ "không in trên phiếu" nằm ở đây.

Thay model: sửa khối `llm`. Thay cơ sở KCB: sửa khối `facility`. Đường dẫn: khối `paths`.
Giá trị dạng ${ENV_VAR} được thay bằng biến môi trường.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .llm import LLMSettings

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_\.]*)\}")


def _expand(v: Any) -> Any:
    if isinstance(v, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), ""), v)
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_expand(x) for x in v]
    return v


@dataclass
class Facility:
    slug: str = "bvdk-phutho"
    base_url: str = "http://his.bvdk-phutho.vn/fhir"
    ma_cskcb: str | None = None
    ten_day_du: str = "Bệnh viện Đa khoa tỉnh Phú Thọ"
    so_y_te: str = "Sở Y tế Phú Thọ"
    province: tuple = ("25", "Tỉnh Phú Thọ")
    ward: tuple | None = ("08527", "Xã Bản Nguyên")
    mrn_system: str = "http://his.bvdk-phutho.vn/sid/mrn"
    staff_system: str = "http://his.bvdk-phutho.vn/sid/staff-id"
    order_system: str = "http://his.bvdk-phutho.vn/sid/y-lenh"

    def as_dict(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        d["province"] = tuple(d["province"])
        d["ward"] = tuple(d["ward"]) if d["ward"] else None
        return d


@dataclass
class Paths:
    root: str = "."
    templates: str = "config/templates"
    vendor: str = "vendor"
    output: str = "output"
    fonts: list = field(default_factory=lambda: [r"C:\Windows\Fonts\times.ttf", "/usr/share/fonts/truetype/msttcorefonts/Times_New_Roman.ttf"])


@dataclass
class Rules:
    """Luật phân loại phiếu theo tên file (regex, không phân biệt hoa thường) — có thể thay bằng classifier ngoài."""
    skip: list = field(default_factory=lambda: [
        [r"gi[aấ]y\s*v[aà]o\s*vi[eệ]n|giấy vv|_vv\b", "giay-vao-vien"],
        [r"_cls\b|\bcls\.pdf", "phieu-cls"],
        [r"t[eé]\s*ng[aã]", "phieu-danh-gia-nguy-co-te-nga"],
        [r"dinh\s*d[uưỡ]+ng", "phieu-sang-loc-dinh-duong"],
    ])
    forms: list = field(default_factory=lambda: [
        [r"kh[aá]m\s*b[eệ]nh\s*v[aà]o\s*vi[eệ]n", "phieu-kham-benh-vao-vien"],
        [r"huy[eế]t\s*h[oọ]c", "ket-qua-xet-nghiem-huyet-hoc"],
        [r"sinh\s*h[oó]a", "ket-qua-xet-nghiem-sinh-hoa-mau"],
        [r"n[uư][oớ]c\s*ti[eể]u", "phieu-xet-nghiem-nuoc-dich"],
        [r"c[oộ]ng\s*h[uưở]+ng\s*t[uừ]", "phieu-chup-cong-huong-tu"],
        [r"si[eê]u\s*[aâ]m", "phieu-sieu-am-doppler"],
        [r"[dđ]i[eệ]n\s*n[aã]o", "phieu-dien-nao-do"],
        [r"theo\s*d[oõ]i\s*v[aà]\s*ch[aă]m\s*s[oó]c|ch[aă]m\s*s[oó]c", "phieu-cham-soc"],
        [r"h[oộ]i\s*ch[aẩ]n", "trich-bien-ban-hoi-chan"],
        [r"[dđ]i[eề]u\s*tr[iị]", "phieu-dieu-tri"],
        [r"t[oó]m\s*t[aắ]t", "ban-tom-tat-ho-so-benh-an"],
        [r"c[oô]ng\s*khai", "phieu-cong-khai-dich-vu"],
        [r"thanh\s*to[aá]n|b[aả]ng\s*k[eê]", "bang-ke-chi-phi-kcb"],
    ])
    appendix_markers: list = field(default_factory=lambda: ["BẢNG QUY ƯỚC MÃ HOÁ", "HƯỚNG DẪN+GHI PHIẾU"])
    max_tokens_extract: int = 24000
    max_tokens_coding: int = 8000


@dataclass
class PipelineConfig:
    llm: LLMSettings = field(default_factory=LLMSettings)
    facility: Facility = field(default_factory=Facility)
    paths: Paths = field(default_factory=Paths)
    rules: Rules = field(default_factory=Rules)

    @property
    def root(self) -> Path:
        return Path(self.paths.root).resolve()

    def path(self, p: str) -> Path:
        q = Path(p)
        return q if q.is_absolute() else self.root / q


def _build(cls, data: dict | None):
    data = data or {}
    names = {f.name for f in fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ValueError(f"{cls.__name__}: khoá không hợp lệ {sorted(unknown)}")
    return cls(**data)


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> PipelineConfig:
    """Đọc YAML/JSON (nếu có), áp overrides (dict lồng nhau), thay ${ENV}."""
    data: dict = {}
    if path:
        p = Path(path)
        text = p.read_text(encoding="utf-8")
        if p.suffix.lower() in (".yaml", ".yml"):
            import yaml
            data = yaml.safe_load(text) or {}
        else:
            data = json.loads(text)
    for k, v in (overrides or {}).items():
        if isinstance(v, dict):
            data.setdefault(k, {}).update(v)
        else:
            data[k] = v
    data = _expand(data)
    cfg = PipelineConfig(llm=_build(LLMSettings, data.get("llm")), facility=_build(Facility, data.get("facility")),
                         paths=_build(Paths, data.get("paths")), rules=_build(Rules, data.get("rules")))
    if path and cfg.paths.root == ".":
        cfg.paths.root = str(Path(path).resolve().parent.parent if Path(path).resolve().parent.name == "config" else Path(path).resolve().parent)
    for attr in ("cache_dir", "legacy_cache_dir", "record_dir"):
        v = getattr(cfg.llm, attr)
        if v:
            setattr(cfg.llm, attr, str(cfg.path(v)))
    return cfg
