from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image

from .domain import Detection, NormalizedItem
from .io_utils import ensure_dir


class ImageNormalizer:
    def __init__(self, config: dict[str, Any]) -> None:
        self.canvas_size = int(config["canvas_size"])
        self.pad_fraction = float(config.get("pad_fraction", 0.08))
        self.background_color = tuple(config.get("background_color", [255, 255, 255]))
        self.preserve_alpha = bool(config.get("preserve_alpha", True))
        self.save_rgb_version = bool(config.get("save_rgb_version", True))

    def normalize(
        self,
        detection: Detection,
        output_dir: str | Path,
    ) -> NormalizedItem:
        out_dir = ensure_dir(output_dir)
        if detection.cutout_path is None:
            raise ValueError("Detection must include cutout_path for normalization.")
        image = Image.open(detection.cutout_path).convert("RGBA")
        width, height = image.size
        padded_extent = int(max(width, height) * (1.0 + self.pad_fraction * 2))
        padded_extent = max(padded_extent, max(width, height))
        canvas = Image.new("RGBA", (padded_extent, padded_extent), (0, 0, 0, 0))
        x = (padded_extent - width) // 2
        y = (padded_extent - height) // 2
        canvas.alpha_composite(image, (x, y))
        canvas = canvas.resize((self.canvas_size, self.canvas_size), Image.LANCZOS)
        rgba_path = out_dir / f"{detection.item_id}_normalized_rgba.png"
        canvas.save(rgba_path)

        rgb_path = None
        if self.save_rgb_version:
            rgb_canvas = Image.new("RGBA", canvas.size, (*self.background_color, 255))
            rgb_canvas.alpha_composite(canvas)
            rgb_final = rgb_canvas.convert("RGB")
            rgb_path = out_dir / f"{detection.item_id}_normalized_rgb.png"
            rgb_final.save(rgb_path)

        return NormalizedItem(
            item_id=detection.item_id,
            label=detection.label,
            normalized_rgba_path=rgba_path,
            normalized_rgb_path=rgb_path,
            original_cutout_path=detection.cutout_path,
            metadata={
                "canvas_size": self.canvas_size,
                "pad_fraction": self.pad_fraction,
                "background_color": list(self.background_color),
                "preserve_alpha": self.preserve_alpha,
            },
        )
