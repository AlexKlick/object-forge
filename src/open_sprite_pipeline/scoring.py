from __future__ import annotations

from pathlib import Path
from statistics import mean, pstdev

import numpy as np
from PIL import Image, ImageFilter

from .domain import QualityReport


def _coverage(image: Image.Image) -> float:
    rgba = image.convert("RGBA")
    alpha = np.asarray(rgba.getchannel("A"), dtype=np.float32)
    return float((alpha > 0).mean())


def _sharpness(image: Image.Image) -> float:
    gray = image.convert("L")
    edges = gray.filter(ImageFilter.FIND_EDGES)
    values = np.asarray(edges, dtype=np.float32)
    return float(values.mean())


def score_renders(
    frame_paths: list[str | Path],
    thresholds: dict[str, float | int],
) -> QualityReport:
    metrics: dict[str, float | int] = {
        "frame_count": len(frame_paths),
    }
    if not frame_paths:
        return QualityReport(
            metrics=metrics,
            needs_review=True,
            review_reasons=["no frames rendered"],
        )

    coverages = []
    sharpnesses = []
    for path in frame_paths:
        image = Image.open(path)
        coverages.append(_coverage(image))
        sharpnesses.append(_sharpness(image))

    metrics["coverage_mean"] = round(mean(coverages), 6)
    metrics["coverage_std"] = round(pstdev(coverages), 6) if len(coverages) > 1 else 0.0
    metrics["sharpness_mean"] = round(mean(sharpnesses), 6)

    reasons: list[str] = []
    if metrics["frame_count"] < int(thresholds.get("min_frame_count", 8)):
        reasons.append("frame_count below threshold")
    if float(metrics["coverage_mean"]) < float(thresholds.get("min_coverage_mean", 0.02)):
        reasons.append("coverage_mean below threshold")
    if float(metrics["sharpness_mean"]) < float(thresholds.get("min_sharpness_mean", 1.0)):
        reasons.append("sharpness_mean below threshold")

    return QualityReport(
        metrics=metrics,
        needs_review=bool(reasons),
        review_reasons=reasons,
    )
