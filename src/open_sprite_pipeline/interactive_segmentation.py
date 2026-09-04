from __future__ import annotations

from collections import deque
from contextlib import nullcontext
from dataclasses import dataclass
import importlib.util
from pathlib import Path
from threading import Lock
from typing import Any

import numpy as np
from PIL import Image, ImageFilter


@dataclass(frozen=True)
class PromptPoint:
    x: float
    y: float
    label: int


@dataclass
class SegmentationResult:
    mask: np.ndarray
    engine: str
    score: float | None = None
    fallback_reason: str | None = None


class ColorFloodSegmenter:
    """Small deterministic fallback for hosts where SAM2 cannot be loaded.

    The fallback is intentionally identified in every result. It is useful for
    keeping the review workflow operable, but it is not represented as model
    inference or as equivalent-quality evidence to SAM2.
    """

    engine = "color_flood_fallback"

    def __init__(self, tolerance: float = 62.0) -> None:
        self.tolerance = float(tolerance)

    def status(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "available": True,
            "loaded": True,
            "model_id": None,
            "device": "cpu",
        }

    @staticmethod
    def _connected(candidate: np.ndarray, seeds: list[tuple[int, int]]) -> np.ndarray:
        height, width = candidate.shape
        selected = np.zeros_like(candidate, dtype=bool)
        queue: deque[tuple[int, int]] = deque()
        for x, y in seeds:
            if 0 <= x < width and 0 <= y < height and candidate[y, x]:
                selected[y, x] = True
                queue.append((x, y))

        while queue:
            x, y = queue.popleft()
            for next_x, next_y in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if (
                    0 <= next_x < width
                    and 0 <= next_y < height
                    and candidate[next_y, next_x]
                    and not selected[next_y, next_x]
                ):
                    selected[next_y, next_x] = True
                    queue.append((next_x, next_y))
        return selected

    def segment(
        self,
        image_path: str | Path,
        points: list[PromptPoint],
        box: tuple[float, float, float, float] | None = None,
    ) -> SegmentationResult:
        image = Image.open(image_path).convert("RGB")
        original_size = image.size
        scale = min(1.0, 1024.0 / max(image.size))
        if scale < 1.0:
            image = image.resize(
                (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                Image.Resampling.LANCZOS,
            )

        pixels = np.asarray(image, dtype=np.float32)
        height, width = pixels.shape[:2]
        positive = [
            (
                min(width - 1, max(0, round(point.x * scale))),
                min(height - 1, max(0, round(point.y * scale))),
            )
            for point in points
            if point.label == 1
        ]
        negative = [
            (
                min(width - 1, max(0, round(point.x * scale))),
                min(height - 1, max(0, round(point.y * scale))),
            )
            for point in points
            if point.label == 0
        ]
        if not positive:
            raise ValueError("At least one positive point is required.")

        seed_colors = np.stack([pixels[y, x] for x, y in positive])
        color_distance = np.sqrt(
            ((pixels[:, :, None, :] - seed_colors[None, None, :, :]) ** 2).sum(axis=3)
        ).min(axis=2)
        candidate = color_distance <= self.tolerance

        if box is not None:
            x1, y1, x2, y2 = box
            scaled_box = (
                max(0, min(width, round(min(x1, x2) * scale))),
                max(0, min(height, round(min(y1, y2) * scale))),
                max(0, min(width, round(max(x1, x2) * scale))),
                max(0, min(height, round(max(y1, y2) * scale))),
            )
            box_mask = np.zeros_like(candidate)
            box_mask[scaled_box[1] : scaled_box[3], scaled_box[0] : scaled_box[2]] = True
            candidate &= box_mask

        selected = self._connected(candidate, positive)
        if not selected.any():
            for x, y in positive:
                selected[y, x] = True

        mask_image = Image.fromarray(selected.astype(np.uint8) * 255, mode="L")
        mask_image = mask_image.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
        selected = np.asarray(mask_image) > 127

        if negative:
            negative_colors = np.stack([pixels[y, x] for x, y in negative])
            negative_distance = np.sqrt(
                ((pixels[:, :, None, :] - negative_colors[None, None, :, :]) ** 2).sum(axis=3)
            ).min(axis=2)
            selected &= negative_distance > min(40.0, self.tolerance * 0.7)
            radius = max(3, round(min(width, height) * 0.012))
            yy, xx = np.ogrid[:height, :width]
            for x, y in negative:
                selected[(xx - x) ** 2 + (yy - y) ** 2 <= radius**2] = False

        for x, y in positive:
            selected[y, x] = True

        if image.size != original_size:
            selected = np.asarray(
                Image.fromarray(selected.astype(np.uint8) * 255, mode="L").resize(
                    original_size, Image.Resampling.NEAREST
                )
            ) > 127

        return SegmentationResult(mask=selected, engine=self.engine, score=None)


class Sam2InteractiveSegmenter:
    """Lazy, lock-protected SAM2 point-prompt inference with named fallback."""

    engine = "sam2"

    def __init__(
        self,
        model_id: str = "facebook/sam2.1-hiera-tiny",
        device: str = "cpu",
        *,
        local_files_only: bool = False,
        allow_fallback: bool = True,
        fallback: ColorFloodSegmenter | None = None,
    ) -> None:
        self.model_id = model_id
        self.requested_device = device
        self.local_files_only = local_files_only
        self.allow_fallback = allow_fallback
        self.fallback = fallback or ColorFloodSegmenter()
        self._model: Any | None = None
        self._processor: Any | None = None
        self._torch: Any | None = None
        self._device = device
        self._load_error: str | None = None
        self._lock = Lock()

    def status(self) -> dict[str, Any]:
        dependencies = all(
            importlib.util.find_spec(module) is not None
            for module in ("torch", "transformers")
        )
        return {
            "engine": self.engine,
            "available": dependencies,
            "loaded": self._model is not None,
            "model_id": self.model_id,
            "device": self._device,
            "requested_device": self.requested_device,
            "local_files_only": self.local_files_only,
            "allow_fallback": self.allow_fallback,
            "last_load_error": self._load_error,
            "fallback": self.fallback.status(),
        }

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
            from transformers import Sam2Model, Sam2Processor

            device = self.requested_device
            if device.startswith("cuda") and not torch.cuda.is_available():
                device = "cpu"
            processor = Sam2Processor.from_pretrained(
                self.model_id,
                local_files_only=self.local_files_only,
            )
            model = Sam2Model.from_pretrained(
                self.model_id,
                local_files_only=self.local_files_only,
            )
            model.to(device)
            model.eval()
            self._torch = torch
            self._processor = processor
            self._model = model
            self._device = device
            self._load_error = None
        except Exception as exc:  # noqa: BLE001
            self._load_error = f"{type(exc).__name__}: {exc}"
            raise

    def _segment_sam2(
        self,
        image_path: str | Path,
        points: list[PromptPoint],
        box: tuple[float, float, float, float] | None,
    ) -> SegmentationResult:
        self._load()
        assert self._torch is not None
        assert self._processor is not None
        assert self._model is not None

        image = Image.open(image_path).convert("RGB")
        positive_count = sum(point.label == 1 for point in points)
        if positive_count == 0:
            raise ValueError("At least one positive point is required.")

        processor_args: dict[str, Any] = {
            "images": image,
            "input_points": [[[[point.x, point.y] for point in points]]],
            "input_labels": [[[point.label for point in points]]],
            "return_tensors": "pt",
        }
        if box is not None:
            x1, y1, x2, y2 = box
            processor_args["input_boxes"] = [
                [[min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]]
            ]

        inputs = self._processor(**processor_args).to(self._device)
        autocast = nullcontext()
        if self._device.startswith("cuda"):
            autocast = self._torch.autocast(device_type="cuda", dtype=self._torch.bfloat16)
        with self._torch.inference_mode(), autocast:
            outputs = self._model(**inputs, multimask_output=True)

        masks = self._processor.post_process_masks(
            outputs.pred_masks.cpu(), inputs["original_sizes"].cpu()
        )[0]
        scores = outputs.iou_scores.detach().float().cpu()
        point_batch = 0
        best = int(scores[0, point_batch].argmax().item())
        mask = masks[point_batch, best].detach().cpu().numpy() > 0
        score = float(scores[0, point_batch, best].item())
        return SegmentationResult(mask=mask, engine=self.engine, score=score)

    def segment(
        self,
        image_path: str | Path,
        points: list[PromptPoint],
        box: tuple[float, float, float, float] | None = None,
    ) -> SegmentationResult:
        with self._lock:
            try:
                return self._segment_sam2(image_path, points, box)
            except Exception as exc:  # noqa: BLE001
                if not self.allow_fallback:
                    raise
                fallback_result = self.fallback.segment(image_path, points, box)
                fallback_result.fallback_reason = f"{type(exc).__name__}: {exc}"
                return fallback_result
