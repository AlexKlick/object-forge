#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from contextlib import nullcontext
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from torchvision.ops import box_convert

from sam2.build_sam import build_sam2
from sam2.sam2_image_predictor import SAM2ImagePredictor
from grounding_dino.groundingdino.util.inference import load_model, load_image, predict


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--sam2-checkpoint", required=True)
    parser.add_argument("--sam2-model-config", required=True)
    parser.add_argument("--grounding-dino-config", required=True)
    parser.add_argument("--grounding-dino-checkpoint", required=True)
    parser.add_argument("--box-threshold", type=float, default=0.35)
    parser.add_argument("--text-threshold", type=float, default=0.25)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--multimask-output", action="store_true")
    return parser.parse_args()


def normalize_prompt(prompt: str) -> str:
    value = prompt.strip().lower()
    if not value.endswith("."):
        value += "."
    return value


def save_mask_and_cutout(
    base_image: Image.Image,
    mask: np.ndarray,
    bbox_xyxy: list[float],
    output_dir: Path,
    item_id: str,
) -> tuple[Path, Path]:
    mask_u8 = (mask.astype(np.uint8)) * 255
    full_mask = Image.fromarray(mask_u8, mode="L")
    mask_path = output_dir / f"{item_id}_mask.png"
    full_mask.save(mask_path)

    rgba = base_image.copy().convert("RGBA")
    rgba.putalpha(full_mask)
    x1, y1, x2, y2 = [int(v) for v in bbox_xyxy]
    crop = rgba.crop((x1, y1, x2, y2))
    cutout_path = output_dir / f"{item_id}_cutout.png"
    crop.save(cutout_path)
    return mask_path, cutout_path


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    device = args.device
    if not torch.cuda.is_available():
        device = "cpu"

    prompt = normalize_prompt(args.prompt)

    sam2_model = build_sam2(args.sam2_model_config, args.sam2_checkpoint, device=device)
    sam2_predictor = SAM2ImagePredictor(sam2_model)

    grounding_model = load_model(
        model_config_path=args.grounding_dino_config,
        model_checkpoint_path=args.grounding_dino_checkpoint,
        device=device,
    )

    image_source, image = load_image(args.image)
    sam2_predictor.set_image(image_source)

    boxes, confidences, labels = predict(
        model=grounding_model,
        image=image,
        caption=prompt,
        box_threshold=args.box_threshold,
        text_threshold=args.text_threshold,
        device=device,
    )

    height, width, _ = image_source.shape
    detections: list[dict] = []

    if len(boxes) == 0:
        Path(args.output_json).write_text(
            json.dumps(
                {
                    "image_path": args.image,
                    "width": width,
                    "height": height,
                    "detections": [],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return 0

    boxes = boxes * torch.tensor([width, height, width, height])
    input_boxes = box_convert(boxes=boxes, in_fmt="cxcywh", out_fmt="xyxy").cpu().numpy()

    autocast_context = nullcontext()
    if device.startswith("cuda"):
        autocast_context = torch.autocast(device_type="cuda", dtype=torch.bfloat16)
        if torch.cuda.get_device_properties(0).major >= 8:
            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True

    with torch.inference_mode(), autocast_context:
        masks, scores, _ = sam2_predictor.predict(
            point_coords=None,
            point_labels=None,
            box=input_boxes,
            multimask_output=args.multimask_output,
        )

    if args.multimask_output:
        best = np.argmax(scores, axis=1)
        masks = masks[np.arange(masks.shape[0]), best]
        scores = scores[np.arange(scores.shape[0]), best]

    if masks.ndim == 4:
        masks = masks.squeeze(1)

    base_image = Image.open(args.image).convert("RGBA")
    confidences = confidences.cpu().numpy().tolist() if hasattr(confidences, "cpu") else list(confidences)

    for index, (label, bbox, score, gdino_score) in enumerate(
        zip(labels, input_boxes, scores.tolist(), confidences)
    ):
        item_id = f"item_{index:03d}"
        mask_path, cutout_path = save_mask_and_cutout(
            base_image=base_image,
            mask=masks[index].astype(bool),
            bbox_xyxy=bbox.tolist(),
            output_dir=output_dir,
            item_id=item_id,
        )
        detections.append(
            {
                "item_id": item_id,
                "label": str(label),
                "score": float(score),
                "bbox": [float(v) for v in bbox.tolist()],
                "mask_path": str(mask_path),
                "cutout_path": str(cutout_path),
                "metadata": {
                    "extractor": "grounded_sam2",
                    "grounding_score": float(gdino_score),
                    "device": device,
                },
            }
        )

    payload = {
        "image_path": args.image,
        "width": width,
        "height": height,
        "detections": detections,
    }
    Path(args.output_json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
