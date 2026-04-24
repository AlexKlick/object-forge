from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml
from PIL import Image


def ensure_dir(path: str | Path) -> Path:
    out = Path(path)
    out.mkdir(parents=True, exist_ok=True)
    return out


def sha256_file(path: str | Path) -> str:
    file_path = Path(path)
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_json(path: str | Path, payload: Any) -> None:
    Path(path).write_text(json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8")


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_yaml(path: str | Path) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def save_yaml(path: str | Path, payload: Any) -> None:
    Path(path).write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def utc_timestamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def has_alpha(path: str | Path) -> bool:
    image = Image.open(path)
    return "A" in image.getbands()


def image_size(path: str | Path) -> tuple[int, int]:
    image = Image.open(path)
    return image.size


def relative_to(base: str | Path, path: str | Path) -> str:
    base_path = Path(base).resolve()
    target = Path(path).resolve()
    try:
        return str(target.relative_to(base_path))
    except ValueError:
        return str(target)


def env_replace(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if not value.startswith("${") or not value.endswith("}"):
        return value
    inner = value[2:-1]
    if ":" in inner:
        key, default = inner.split(":", 1)
    else:
        key, default = inner, ""
    return os.getenv(key, default)
