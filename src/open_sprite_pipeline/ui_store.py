from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from .interactive_segmentation import PromptPoint, SegmentationResult
from .io_utils import load_json, save_json, sha256_file, utc_timestamp


_SAFE_ID = re.compile(r"^[a-f0-9]{32}$")
_SEGMENT_FILES = {
    "mask": "mask.png",
    "cutout": "cutout.png",
    "metadata": "metadata.json",
}
_MESH_FILES = {
    "model.obj",
    "model.mtl",
    "texture.png",
}


class UiStoreError(ValueError):
    pass


@dataclass(frozen=True)
class StoredUpload:
    image_id: str
    source_path: Path
    width: int
    height: int
    original_name: str
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "image_id": self.image_id,
            "width": self.width,
            "height": self.height,
            "original_name": self.original_name,
            "sha256": self.sha256,
        }


class UiAssetStore:
    def __init__(
        self,
        root: str | Path,
        *,
        max_upload_bytes: int = 20 * 1024 * 1024,
        max_image_pixels: int = 36_000_000,
    ) -> None:
        root_path = Path(root)
        root_path.mkdir(parents=True, exist_ok=True)
        self.root = root_path.resolve()
        self.max_upload_bytes = int(max_upload_bytes)
        self.max_image_pixels = int(max_image_pixels)

    @staticmethod
    def _validate_id(value: str, label: str) -> str:
        if not _SAFE_ID.fullmatch(value):
            raise UiStoreError(f"Invalid {label}.")
        return value

    def _confined(self, path: Path) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise UiStoreError("Resolved UI artifact path escapes the UI root.") from exc
        return resolved

    def _upload_dir(self, image_id: str) -> Path:
        self._validate_id(image_id, "image id")
        return self._confined(self.root / image_id)

    def _segment_dir(self, image_id: str, segment_id: str) -> Path:
        self._validate_id(segment_id, "segment id")
        return self._confined(self._upload_dir(image_id) / "segments" / segment_id)

    def create_upload(self, original_name: str | None, payload: bytes) -> StoredUpload:
        if not payload:
            raise UiStoreError("The uploaded file is empty.")
        if len(payload) > self.max_upload_bytes:
            raise UiStoreError(
                f"The uploaded image exceeds the {self.max_upload_bytes // (1024 * 1024)} MB limit."
            )

        try:
            with Image.open(BytesIO(payload)) as candidate:
                candidate.verify()
            with Image.open(BytesIO(payload)) as candidate:
                image = ImageOps.exif_transpose(candidate)
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > self.max_image_pixels:
                    raise UiStoreError(
                        f"Image dimensions exceed the {self.max_image_pixels:,}-pixel limit."
                    )
                mode = "RGBA" if "A" in image.getbands() else "RGB"
                normalized = image.convert(mode)
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise UiStoreError("The uploaded file is not a supported image.") from exc

        image_id = uuid4().hex
        upload_dir = self._confined(self.root / image_id)
        upload_dir.mkdir(parents=False, exist_ok=False)
        source_path = upload_dir / "source.png"
        normalized.save(source_path, format="PNG", optimize=True)
        record = StoredUpload(
            image_id=image_id,
            source_path=source_path,
            width=width,
            height=height,
            original_name=Path(original_name or "image").name[:240],
            sha256=sha256_file(source_path),
        )
        save_json(
            upload_dir / "metadata.json",
            {
                **record.to_dict(),
                "source_path": str(source_path),
                "created_at_utc": utc_timestamp(),
            },
        )
        return record

    def get_upload(self, image_id: str) -> StoredUpload:
        upload_dir = self._upload_dir(image_id)
        metadata_path = upload_dir / "metadata.json"
        source_path = upload_dir / "source.png"
        if not metadata_path.is_file() or not source_path.is_file():
            raise FileNotFoundError("Uploaded image not found.")
        payload = load_json(metadata_path)
        return StoredUpload(
            image_id=image_id,
            source_path=source_path,
            width=int(payload["width"]),
            height=int(payload["height"]),
            original_name=str(payload["original_name"]),
            sha256=str(payload["sha256"]),
        )

    def save_segment(
        self,
        image_id: str,
        result: SegmentationResult,
        points: list[PromptPoint],
        box: tuple[float, float, float, float] | None,
    ) -> dict[str, Any]:
        upload = self.get_upload(image_id)
        mask = np.asarray(result.mask, dtype=bool)
        if mask.shape != (upload.height, upload.width):
            raise UiStoreError(
                f"Segmenter returned mask shape {mask.shape}; expected {(upload.height, upload.width)}."
            )
        if not mask.any():
            raise UiStoreError("The segmenter returned an empty mask. Add or adjust a positive point.")

        ys, xs = np.nonzero(mask)
        x1, x2 = int(xs.min()), int(xs.max()) + 1
        y1, y2 = int(ys.min()), int(ys.max()) + 1
        padding = max(2, round(max(x2 - x1, y2 - y1) * 0.025))
        crop_box = (
            max(0, x1 - padding),
            max(0, y1 - padding),
            min(upload.width, x2 + padding),
            min(upload.height, y2 + padding),
        )

        segment_id = uuid4().hex
        segment_dir = self._segment_dir(image_id, segment_id)
        segment_dir.mkdir(parents=True, exist_ok=False)
        mask_path = segment_dir / "mask.png"
        cutout_path = segment_dir / "cutout.png"

        mask_image = Image.fromarray(mask.astype(np.uint8) * 255, mode="L")
        mask_image.save(mask_path, format="PNG", optimize=True)
        with Image.open(upload.source_path) as source_image:
            source = source_image.convert("RGBA")
        source.putalpha(mask_image)
        source.crop(crop_box).save(cutout_path, format="PNG", optimize=True)

        coverage = float(mask.mean())
        payload = {
            "image_id": image_id,
            "segment_id": segment_id,
            "engine": result.engine,
            "score": result.score,
            "fallback_reason": result.fallback_reason,
            "bbox": [x1, y1, x2, y2],
            "crop_bbox": list(crop_box),
            "coverage": coverage,
            "points": [
                {"x": point.x, "y": point.y, "label": point.label} for point in points
            ],
            "box": list(box) if box is not None else None,
            "mask_path": str(mask_path),
            "cutout_path": str(cutout_path),
            "created_at_utc": utc_timestamp(),
        }
        save_json(segment_dir / "metadata.json", payload)
        return payload

    def get_segment(self, image_id: str, segment_id: str) -> dict[str, Any]:
        segment_dir = self._segment_dir(image_id, segment_id)
        metadata_path = segment_dir / "metadata.json"
        if not metadata_path.is_file():
            raise FileNotFoundError("Segment not found.")
        return load_json(metadata_path)

    def upload_file(self, image_id: str) -> Path:
        return self.get_upload(image_id).source_path

    def segment_file(self, image_id: str, segment_id: str, kind: str) -> Path:
        filename = _SEGMENT_FILES.get(kind)
        if filename is None:
            raise UiStoreError("Unsupported segment artifact kind.")
        path = self._segment_dir(image_id, segment_id) / filename
        if not path.is_file():
            raise FileNotFoundError("Segment artifact not found.")
        return path

    def segment_cutout(self, image_id: str, segment_id: str) -> Path:
        return self.segment_file(image_id, segment_id, "cutout")

    def mesh_dir(self, image_id: str, segment_id: str) -> Path:
        self.get_segment(image_id, segment_id)
        mesh_dir = self._segment_dir(image_id, segment_id) / "mesh"
        mesh_dir.mkdir(parents=True, exist_ok=True)
        return self._confined(mesh_dir)

    def mesh_file(self, image_id: str, segment_id: str, filename: str) -> Path:
        if filename not in _MESH_FILES:
            raise UiStoreError("Unsupported mesh artifact.")
        path = self._segment_dir(image_id, segment_id) / "mesh" / filename
        if not path.is_file():
            raise FileNotFoundError("Mesh artifact not found.")
        return path
