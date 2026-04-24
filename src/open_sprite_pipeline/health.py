from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any


def reason(
    code: str,
    message: str,
    field: str | None = None,
    path: str | Path | None = None,
) -> dict[str, str]:
    payload = {"code": code, "message": message}
    if field:
        payload["field"] = field
    if path is not None:
        payload["path"] = str(path)
    return payload


def health_payload(
    component: str,
    component_id: str,
    enabled: bool,
    reasons: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    reason_list = reasons or []
    if not enabled:
        status = "disabled"
    elif reason_list:
        status = "blocked_pending_environment"
    else:
        status = "ready"
    return {
        "component": component,
        "id": component_id,
        "enabled": enabled,
        "available": enabled and not reason_list,
        "status": status,
        "reasons": reason_list,
    }


def first_reason_code(payload: dict[str, Any], fallback: str = "MODEL_UNAVAILABLE") -> str:
    reasons = payload.get("reasons") or []
    if reasons:
        return str(reasons[0].get("code") or fallback)
    if not payload.get("enabled", False):
        return "MODEL_DISABLED"
    return fallback


def check_executable(value: str | Path | None, field: str) -> dict[str, str] | None:
    if not value:
        return reason("MISSING_EXECUTABLE", "Executable path is not configured.", field=field)
    text = str(value)
    if os.sep not in text and (os.altsep is None or os.altsep not in text):
        if shutil.which(text):
            return None
        return reason("MISSING_EXECUTABLE", "Executable is not on PATH.", field=field, path=text)
    path = Path(text)
    if not path.is_file() or not os.access(path, os.X_OK):
        return reason("MISSING_EXECUTABLE", "Executable path is missing or not executable.", field=field, path=path)
    return None


def check_file(
    value: str | Path | None,
    field: str,
    code: str = "MISSING_CONFIG_PATH",
) -> dict[str, str] | None:
    if not value:
        return reason(code, "Required file path is not configured.", field=field)
    path = Path(value)
    if not path.is_file():
        return reason(code, "Required file path is missing.", field=field, path=path)
    return None


def check_dir(
    value: str | Path | None,
    field: str,
    code: str = "MISSING_REPO_DIR",
) -> dict[str, str] | None:
    if not value:
        return reason(code, "Required directory path is not configured.", field=field)
    path = Path(value)
    if not path.is_dir():
        return reason(code, "Required directory path is missing.", field=field, path=path)
    return None
