from __future__ import annotations

import os
from pathlib import Path

try:
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel
except Exception as exc:  # noqa: BLE001
    raise RuntimeError(
        "FastAPI dependencies are not installed. Install with: pip install -e '.[api]'"
    ) from exc

from .cli import build_orchestrator


CONFIG_PATH = os.getenv("OPEN_SPRITE_PIPELINE_CONFIG", "configs/app.example.yaml")
app = FastAPI(title="Open Sprite Pipeline", version="0.1.0")


class RunRequest(BaseModel):
    image: str
    prompt: str | None = None
    mode: str = "hero"
    parts_hint: int | None = None
    provider: str | None = None
    mock: bool = False


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/pipeline/run")
def run_pipeline(request: RunRequest) -> dict:
    config_path = Path(CONFIG_PATH)
    if not config_path.exists():
        raise HTTPException(status_code=500, detail=f"Config not found: {config_path}")
    orchestrator = build_orchestrator(config_path, mock=request.mock)
    try:
        return orchestrator.run(
            image_path=request.image,
            prompt=request.prompt,
            mode=request.mode,
            parts_hint=request.parts_hint,
            provider_override=request.provider,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
