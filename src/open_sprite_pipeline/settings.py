from __future__ import annotations

from pathlib import Path
from typing import Any

from .io_utils import env_replace, load_yaml


_PATHISH_SUFFIXES = (
    "_path",
    "_dir",
    "_bin",
    "_checkpoint",
    "_config",
)


def _maybe_resolve_path(key: str | None, value: Any, base_dir: Path) -> Any:
    if not isinstance(value, str):
        return value
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if Path(value).is_absolute():
        return value
    if key and key.endswith(_PATHISH_SUFFIXES):
        return str((base_dir / value).resolve())
    return value


def _walk_env(value: Any, base_dir: Path, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {k: _walk_env(v, base_dir=base_dir, key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk_env(v, base_dir=base_dir) for v in value]
    replaced = env_replace(value)
    return _maybe_resolve_path(key, replaced, base_dir=base_dir)


def load_config(config_path: str | Path) -> dict[str, Any]:
    config_file = Path(config_path).resolve()
    raw = load_yaml(config_file)
    return _walk_env(raw, base_dir=config_file.parent)


def get_render_preset(config: dict[str, Any]) -> dict[str, Any]:
    preset_name = config["app"]["render_preset"]
    preset_doc = load_yaml(config["render_presets_path"])
    return preset_doc["presets"][preset_name]
