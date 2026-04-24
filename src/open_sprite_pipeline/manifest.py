from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .domain import ItemRunResult
from .io_utils import save_json, sha256_file, utc_timestamp


_SINGLE_ARTIFACT_KEYS = {
    "cutout_path",
    "mask_path",
    "normalized_rgba_path",
    "normalized_rgb_path",
    "original_cutout_path",
    "primary_asset_path",
    "sprite_sheet_path",
}
_MULTI_ARTIFACT_KEYS = {
    "auxiliary_assets",
    "frame_paths",
    "preview_paths",
}


def _iter_artifact_paths(value: Any, parent_key: str | None = None) -> Iterable[str]:
    if value is None:
        return
    if isinstance(value, dict):
        for key, nested in value.items():
            yield from _iter_artifact_paths(nested, parent_key=str(key))
        return
    if isinstance(value, list):
        if parent_key in _MULTI_ARTIFACT_KEYS:
            for item in value:
                if isinstance(item, str):
                    yield item
        else:
            for item in value:
                yield from _iter_artifact_paths(item, parent_key=parent_key)
        return
    if parent_key in _SINGLE_ARTIFACT_KEYS and isinstance(value, str):
        yield value


def _artifact_hashes(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    seen: dict[str, str] = {}
    for item in items:
        for path_value in _iter_artifact_paths(item):
            path = Path(path_value)
            if path.is_file():
                seen[str(path)] = sha256_file(path)
    return [
        {"path": path, "sha256": digest}
        for path, digest in sorted(seen.items(), key=lambda entry: entry[0])
    ]


def write_manifest(
    manifest_path: str | Path,
    run_id: str,
    environment: str,
    input_image_path: str | Path,
    items: list[ItemRunResult],
    run_dir: str | Path | None = None,
    request: dict[str, Any] | None = None,
    render_preset: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item_payloads = [item.to_dict() for item in items]
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "run_dir": str(run_dir or Path(manifest_path).parent),
        "created_at_utc": utc_timestamp(),
        "environment": environment,
        "request": dict(request or {}),
        "render_preset": dict(render_preset or {}),
        "input_image": {
            "path": str(Path(input_image_path)),
            "sha256": sha256_file(input_image_path),
        },
        "items": item_payloads,
        "artifact_hashes": _artifact_hashes(item_payloads),
    }
    save_json(manifest_path, payload)
    return payload
