"""Đọc .env (không bao giờ in giá trị) và thay ${VAR} trong cấu hình."""
from __future__ import annotations

import os
import re
from pathlib import Path

_VAR = re.compile(r"\$\{([A-Za-z0-9_.]+)(?::-([^}]*))?\}")


def load_dotenv(path: str | Path | None, override: bool = False) -> list[str]:
    """Nạp KEY=VALUE vào os.environ. Trả về danh sách tên biến đã nạp (không trả giá trị)."""
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        return []
    names = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if override or k not in os.environ:
            os.environ[k] = v
        names.append(k)
    return names


def expand(value):
    """Thay ${VAR} / ${VAR:-mặc định} bằng biến môi trường (đệ quy trong dict/list). Biến thiếu -> mặc định hoặc chuỗi rỗng."""
    if isinstance(value, str):
        return _VAR.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand(v) for v in value]
    return value


def secrets() -> list[str]:
    """Các giá trị bí mật (để che trong log/lỗi)."""
    out = []
    for k, v in os.environ.items():
        if v and len(v) >= 12 and re.search(r"KEY|TOKEN|SECRET|PASSWORD", k, re.I):
            out.append(v)
    return out


def scrub(text: str) -> str:
    for s in secrets():
        text = text.replace(s, "***")
    return text
