from __future__ import annotations

import json
import unicodedata
from pathlib import Path


def dump(obj, path: str | Path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def load(path: str | Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def nfc(s):
    return unicodedata.normalize("NFC", s) if isinstance(s, str) else s
