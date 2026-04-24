from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .io_utils import ensure_dir, sha256_file


@dataclass
class RunLayout:
    run_id: str
    run_dir: Path
    extraction_dir: Path
    normalization_dir: Path
    generation_dir: Path
    rendering_dir: Path
    manifest_path: Path
    logs_dir: Path


def create_run_layout(artifact_root: str | Path, image_path: str | Path) -> RunLayout:
    image_hash = sha256_file(image_path)[:16]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_id = f"run_{image_hash}_{timestamp}_{uuid4().hex[:8]}"
    run_dir = ensure_dir(Path(artifact_root) / run_id)
    extraction_dir = ensure_dir(run_dir / "extraction")
    normalization_dir = ensure_dir(run_dir / "normalization")
    generation_dir = ensure_dir(run_dir / "generation")
    rendering_dir = ensure_dir(run_dir / "rendering")
    logs_dir = ensure_dir(run_dir / "logs")
    manifest_path = run_dir / "manifest.json"
    return RunLayout(
        run_id=run_id,
        run_dir=run_dir,
        extraction_dir=extraction_dir,
        normalization_dir=normalization_dir,
        generation_dir=generation_dir,
        rendering_dir=rendering_dir,
        manifest_path=manifest_path,
        logs_dir=logs_dir,
    )
