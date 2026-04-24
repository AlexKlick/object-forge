from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any


def _convert(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {k: _convert(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _convert(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_convert(v) for v in value]
    return value


@dataclass
class BBox:
    x1: float
    y1: float
    x2: float
    y2: float

    def as_int_tuple(self) -> tuple[int, int, int, int]:
        return (int(self.x1), int(self.y1), int(self.x2), int(self.y2))

    def width(self) -> float:
        return max(0.0, self.x2 - self.x1)

    def height(self) -> float:
        return max(0.0, self.y2 - self.y1)


@dataclass
class Detection:
    item_id: str
    label: str
    score: float
    bbox: BBox
    mask_path: Path | None = None
    cutout_path: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _convert(self)


@dataclass
class NormalizedItem:
    item_id: str
    label: str
    normalized_rgba_path: Path
    normalized_rgb_path: Path | None
    original_cutout_path: Path
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _convert(self)


@dataclass
class RouteDecision:
    provider_id: str
    candidates_considered: list[str]
    reason: str
    blocked_candidates: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _convert(self)


@dataclass
class GeneratedAsset:
    provider_id: str
    primary_asset_path: Path
    auxiliary_assets: list[Path] = field(default_factory=list)
    preview_paths: list[Path] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _convert(self)


@dataclass
class RenderedSpriteSet:
    frame_paths: list[Path]
    sprite_sheet_path: Path | None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _convert(self)


@dataclass
class QualityReport:
    metrics: dict[str, Any]
    needs_review: bool
    review_reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return _convert(self)


@dataclass
class ItemRunResult:
    item_id: str
    label: str
    status: str
    extraction: dict[str, Any]
    normalization: dict[str, Any]
    route: dict[str, Any]
    generation: dict[str, Any] | None
    rendering: dict[str, Any] | None
    quality: dict[str, Any] | None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _convert(self)
