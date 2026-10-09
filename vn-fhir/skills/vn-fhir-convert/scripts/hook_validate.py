"""hook_validate.py — PostToolUse hook: tự chạy validator mỗi khi Claude ghi một tệp FHIR.

Claude Code gọi hook với JSON trên stdin: {"tool_name": "Write"|"Edit"|..., "tool_input": {"file_path": ...}, ...}.
Kích hoạt khi tệp .json vừa ghi có "resourceType" (một resource hoặc Bundle) hoặc tên kết thúc bằng .fhir.json.
  * không lỗi  -> exit 0, in tóm tắt (chỉ hiện trong transcript)
  * có lỗi     -> exit 2, in báo cáo ra stderr -> Claude nhìn thấy và phải sửa (vòng lặp chất lượng bắt buộc)
Vendor VN Core: biến môi trường VN_FHIR_VENDOR, hoặc tự tìm <cwd>/vendor (vnlib.find_vendor).
Có thể tắt tạm bằng VN_FHIR_HOOK=off.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    if os.environ.get("VN_FHIR_HOOK", "").lower() in ("off", "0", "false"):
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    fp = (payload.get("tool_input") or {}).get("file_path") or (payload.get("tool_response") or {}).get("filePath")
    if not fp or not str(fp).lower().endswith(".json"):
        return 0
    p = Path(fp)
    if not p.exists() or "validation" in p.parts:
        return 0
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if not (isinstance(obj, dict) and obj.get("resourceType")) and not p.name.endswith(".fhir.json"):
        return 0
    out = Path(tempfile.mkdtemp(prefix="vnfhir_hook_"))
    cmd = [sys.executable, "-I", str(HERE / "validate_fhir.py"), str(p), "--out", str(out), "--quiet"]
    if os.environ.get("VN_FHIR_VENDOR"):
        cmd += ["--vendor", os.environ["VN_FHIR_VENDOR"]]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run(cmd, capture_output=True, env=env)
    r.stderr = (r.stderr or b"").decode("utf-8", errors="replace")
    rep_path = out / "report.json"
    if not rep_path.exists():
        sys.stderr.write(f"[vn-fhir hook] validator không chạy được: {r.stderr[-800:]}\n")
        return 0
    rep = json.loads(rep_path.read_text(encoding="utf-8"))
    errs = [i for i in rep.get("issues", []) if i.get("severity") == "error"]
    warns = [i for i in rep.get("issues", []) if i.get("severity") == "warning"]
    if errs:
        lines = [f"[vn-fhir hook] {p.name}: {len(errs)} lỗi, {len(warns)} cảnh báo — sửa trước khi tiếp tục:"]
        for i in errs[:25]:
            lines.append(f"  - {i.get('resource')} {i.get('path')}: {i.get('message')}")
        if len(errs) > 25:
            lines.append(f"  … và {len(errs) - 25} lỗi khác (xem {rep_path})")
        sys.stderr.write("\n".join(lines) + "\n")
        return 2
    print(f"[vn-fhir hook] {p.name}: hợp lệ (0 lỗi, {len(warns)} cảnh báo)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
