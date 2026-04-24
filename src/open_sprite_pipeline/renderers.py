from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from PIL import Image

from .domain import GeneratedAsset, NormalizedItem, RenderedSpriteSet
from .io_utils import ensure_dir
from .runtime import run_command


def _pack_sprite_sheet(
    frame_paths: list[Path],
    output_dir: str | Path,
    max_columns: int = 4,
) -> tuple[Path | None, dict[str, Any]]:
    frames = [Path(path) for path in frame_paths]
    metadata: dict[str, Any] = {
        "frame_order": [str(path) for path in frames],
        "sprite_sheet_columns": 0,
        "sprite_sheet_rows": 0,
    }
    if not frames:
        return None, metadata

    with Image.open(frames[0]) as first_image:
        width, height = first_image.size
    columns = min(max_columns, len(frames))
    rows = (len(frames) + columns - 1) // columns
    canvas = Image.new("RGBA", (width * columns, height * rows), (0, 0, 0, 0))
    for index, frame_path in enumerate(frames):
        with Image.open(frame_path) as opened:
            image = opened.convert("RGBA")
        x = (index % columns) * width
        y = (index // columns) * height
        canvas.alpha_composite(image, (x, y))

    sheet_path = Path(output_dir) / "sprite_sheet.png"
    canvas.save(sheet_path)
    metadata.update(
        {
            "sprite_sheet_columns": columns,
            "sprite_sheet_rows": rows,
            "frame_width": width,
            "frame_height": height,
        }
    )
    return sheet_path, metadata


class BaseRenderer(ABC):
    @abstractmethod
    def is_enabled(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def render(
        self,
        asset: GeneratedAsset,
        item: NormalizedItem,
        output_dir: str | Path,
        preset: dict[str, Any],
    ) -> RenderedSpriteSet:
        raise NotImplementedError


class MockRenderer(BaseRenderer):
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def is_enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def render(
        self,
        asset: GeneratedAsset,
        item: NormalizedItem,
        output_dir: str | Path,
        preset: dict[str, Any],
    ) -> RenderedSpriteSet:
        del asset
        out_dir = ensure_dir(output_dir)
        source = Image.open(item.normalized_rgba_path).convert("RGBA")
        frames: list[Path] = []
        total = int(preset.get("views", 8))
        resolution = int(preset.get("resolution", source.width))
        source = source.resize((resolution, resolution), Image.LANCZOS)
        for index in range(total):
            frame = source.rotate(-360.0 * index / max(total, 1), resample=Image.BICUBIC)
            frame_path = out_dir / f"frame_{index:03d}.png"
            frame.save(frame_path)
            frames.append(frame_path)
        sheet, sheet_metadata = _pack_sprite_sheet(frames, out_dir)
        return RenderedSpriteSet(
            frame_paths=frames,
            sprite_sheet_path=sheet,
            metadata={
                "renderer": "mock",
                "views": total,
                "render_preset": dict(preset),
                **sheet_metadata,
            },
        )


class BlenderRenderer(BaseRenderer):
    def __init__(self, config: dict[str, Any], project_root: str | Path) -> None:
        self.config = config
        self.project_root = Path(project_root)

    def is_enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    def render(
        self,
        asset: GeneratedAsset,
        item: NormalizedItem,
        output_dir: str | Path,
        preset: dict[str, Any],
    ) -> RenderedSpriteSet:
        del item
        out_dir = ensure_dir(output_dir)
        blender_bin = self.config["blender_bin"]
        script = self.project_root / self.config["script_path"]
        command = [
            blender_bin,
            "-b",
            "-P",
            str(script),
            "--",
            "--input",
            str(asset.primary_asset_path),
            "--output-dir",
            str(out_dir),
            "--views",
            str(preset["views"]),
            "--resolution",
            str(preset["resolution"]),
            "--elevation-deg",
            str(preset["elevation_deg"]),
            "--radius",
            str(preset["radius"]),
            "--engine",
            str(preset.get("engine", "CYCLES")),
        ]
        if preset.get("transparent_background", True):
            command.append("--transparent-background")
        run_command(command, workdir=self.project_root, log_path=out_dir / "render.log")
        frames = sorted(out_dir.glob("frame_*.png"))
        sheet, sheet_metadata = _pack_sprite_sheet(frames, out_dir)
        return RenderedSpriteSet(
            frame_paths=frames,
            sprite_sheet_path=sheet,
            metadata={
                "renderer": "blender",
                "views": len(frames),
                "render_preset": dict(preset),
                **sheet_metadata,
            },
        )


def build_renderer(config: dict[str, Any], project_root: str | Path) -> BaseRenderer:
    renderer_cfg = config["renderer"]
    kind = renderer_cfg["kind"]
    if kind == "mock":
        return MockRenderer(renderer_cfg["mock"])
    if kind == "blender":
        return BlenderRenderer(renderer_cfg["blender"], project_root=project_root)
    raise ValueError(f"Unsupported renderer kind: {kind}")
