from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from PIL import Image

from .domain import BBox, Detection
from .health import check_executable, check_file, health_payload, reason
from .io_utils import ensure_dir, load_json
from .runtime import run_command


class BaseExtractor(ABC):
    def preflight(self) -> dict[str, Any]:
        return health_payload(
            component="extractor",
            component_id=self.__class__.__name__,
            enabled=True,
        )

    @abstractmethod
    def extract(
        self,
        image_path: str | Path,
        prompt: str | None,
        output_dir: str | Path,
    ) -> list[Detection]:
        raise NotImplementedError


class SimpleAlphaExtractor(BaseExtractor):
    def preflight(self) -> dict[str, Any]:
        return health_payload("extractor", "simple_alpha", enabled=True)

    def extract(
        self,
        image_path: str | Path,
        prompt: str | None,
        output_dir: str | Path,
    ) -> list[Detection]:
        del prompt
        output_path = ensure_dir(output_dir)
        image = Image.open(image_path).convert("RGBA")
        alpha = image.getchannel("A")
        bbox = alpha.getbbox()
        if bbox is None:
            bbox = (0, 0, image.width, image.height)
        item_id = "item_000"
        cutout_path = output_path / f"{item_id}.png"
        cropped = image.crop(bbox)
        cropped.save(cutout_path)
        return [
            Detection(
                item_id=item_id,
                label="object",
                score=1.0,
                bbox=BBox(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
                cutout_path=cutout_path,
                mask_path=None,
                metadata={"extractor": "simple_alpha"},
            )
        ]


class GroundedSAM2Extractor(BaseExtractor):
    def __init__(self, config: dict[str, Any], project_root: str | Path) -> None:
        self.config = config
        self.project_root = Path(project_root)

    def preflight(self) -> dict[str, Any]:
        enabled = bool(self.config.get("enabled", False))
        reasons = []
        if not enabled:
            reasons.append(reason("MODEL_DISABLED", "Grounded-SAM-2 extractor is disabled."))
        else:
            for check in [
                check_executable(self.config.get("python_bin"), "python_bin"),
                check_file(
                    self.project_root / "backend_adapters" / "grounded_sam2_extract.py",
                    "adapter",
                    code="MISSING_ADAPTER",
                ),
                check_file(self.config.get("sam2_checkpoint"), "sam2_checkpoint"),
                check_file(self.config.get("sam2_model_config"), "sam2_model_config"),
                check_file(self.config.get("grounding_dino_config"), "grounding_dino_config"),
                check_file(
                    self.config.get("grounding_dino_checkpoint"),
                    "grounding_dino_checkpoint",
                ),
            ]:
                if check:
                    reasons.append(check)
        return health_payload("extractor", "grounded_sam2", enabled, reasons)

    def extract(
        self,
        image_path: str | Path,
        prompt: str | None,
        output_dir: str | Path,
    ) -> list[Detection]:
        if not prompt:
            raise ValueError("GroundedSAM2Extractor requires a grounding prompt.")
        cfg = self.config
        python_bin = cfg["python_bin"]
        adapter = self.project_root / "backend_adapters" / "grounded_sam2_extract.py"
        out_dir = ensure_dir(output_dir)
        result_json = out_dir / "grounded_sam2_results.json"
        command = [
            python_bin,
            str(adapter),
            "--image",
            str(image_path),
            "--prompt",
            prompt,
            "--output-dir",
            str(out_dir),
            "--output-json",
            str(result_json),
            "--sam2-checkpoint",
            str(cfg["sam2_checkpoint"]),
            "--sam2-model-config",
            str(cfg["sam2_model_config"]),
            "--grounding-dino-config",
            str(cfg["grounding_dino_config"]),
            "--grounding-dino-checkpoint",
            str(cfg["grounding_dino_checkpoint"]),
            "--box-threshold",
            str(cfg.get("box_threshold", 0.35)),
            "--text-threshold",
            str(cfg.get("text_threshold", 0.25)),
            "--device",
            str(cfg.get("device", "cuda")),
        ]
        if cfg.get("multimask_output", False):
            command.append("--multimask-output")
        run_command(command, workdir=self.project_root, log_path=Path(out_dir) / "extract.log")
        payload = load_json(result_json)
        detections: list[Detection] = []
        for item in payload["detections"]:
            detections.append(
                Detection(
                    item_id=item["item_id"],
                    label=item["label"],
                    score=float(item["score"]),
                    bbox=BBox(
                        x1=float(item["bbox"][0]),
                        y1=float(item["bbox"][1]),
                        x2=float(item["bbox"][2]),
                        y2=float(item["bbox"][3]),
                    ),
                    mask_path=Path(item["mask_path"]) if item.get("mask_path") else None,
                    cutout_path=Path(item["cutout_path"]) if item.get("cutout_path") else None,
                    metadata=item.get("metadata", {}),
                )
            )
        logging.info("Grounded-SAM-2 extracted %s items", len(detections))
        return detections


def build_extractor(config: dict[str, Any], project_root: str | Path) -> BaseExtractor:
    extractor_cfg = config["extractor"]
    kind = extractor_cfg["kind"]
    if kind == "simple_alpha":
        return SimpleAlphaExtractor()
    if kind == "grounded_sam2":
        return GroundedSAM2Extractor(extractor_cfg["grounded_sam2"], project_root=project_root)
    raise ValueError(f"Unsupported extractor kind: {kind}")
