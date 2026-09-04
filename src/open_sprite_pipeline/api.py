from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

try:
    from fastapi import FastAPI, File, HTTPException, Request, UploadFile
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel, Field
except Exception as exc:  # noqa: BLE001
    raise RuntimeError(
        "FastAPI dependencies are not installed. Install with: pip install -e '.[api]'"
    ) from exc

from .cli import build_orchestrator
from .generation_queue import GenerationQueueFullError, GpuGenerationQueue
from .interactive_segmentation import (
    ColorFloodSegmenter,
    PromptPoint,
    Sam2InteractiveSegmenter,
)
from .settings import load_config
from .silhouette_mesh import create_silhouette_mesh
from .ui_store import UiAssetStore, UiStoreError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = Path(__file__).resolve().parent / "web"
DEFAULT_CONFIG_PATH = os.getenv("OPEN_SPRITE_PIPELINE_CONFIG", "configs/app.example.yaml")


def _truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class RunRequest(BaseModel):
    image: str
    prompt: str | None = None
    mode: str = "hero"
    parts_hint: int | None = None
    provider: str | None = None
    mock: bool = False


class PointRequest(BaseModel):
    x: float = Field(ge=0)
    y: float = Field(ge=0)
    label: Literal[0, 1] = 1


class SegmentRequest(BaseModel):
    points: list[PointRequest] = Field(min_length=1, max_length=32)
    box: tuple[float, float, float, float] | None = None


class SegmentGenerateRequest(BaseModel):
    generation: Literal["silhouette", "pipeline"] = "silhouette"
    prompt: str | None = None
    mode: Literal["hero", "draft", "part_aware"] = "hero"
    parts_hint: int | None = Field(default=None, ge=1, le=64)
    provider: str | None = None
    mock: bool = True
    grid_size: int = Field(default=72, ge=16, le=160)
    depth: float = Field(default=0.16, gt=0, le=2)


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (UiStoreError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")


def create_app(
    *,
    config_path: str | Path | None = None,
    ui_root: str | Path | None = None,
    segmenter: Any | None = None,
    allow_real_generation: bool | None = None,
) -> FastAPI:
    resolved_config = Path(config_path or DEFAULT_CONFIG_PATH)
    if not resolved_config.is_absolute():
        resolved_config = (PROJECT_ROOT / resolved_config).resolve()
    resolved_ui_root = Path(
        ui_root or os.getenv("OPEN_SPRITE_UI_ROOT", str(PROJECT_ROOT / ".runs" / "ui"))
    )
    if not resolved_ui_root.is_absolute():
        resolved_ui_root = (PROJECT_ROOT / resolved_ui_root).resolve()

    if segmenter is None:
        engine = os.getenv("OPEN_SPRITE_SEGMENTER", "sam2").strip().lower()
        if engine == "fallback":
            segmenter = ColorFloodSegmenter()
        else:
            segmenter = Sam2InteractiveSegmenter(
                model_id=os.getenv(
                    "OPEN_SPRITE_SAM2_MODEL", "facebook/sam2.1-hiera-tiny"
                ),
                device=os.getenv("OPEN_SPRITE_SAM2_DEVICE", "cpu"),
                local_files_only=_truthy(os.getenv("OPEN_SPRITE_SAM2_LOCAL_ONLY")),
                allow_fallback=_truthy(
                    os.getenv("OPEN_SPRITE_SEGMENTATION_ALLOW_FALLBACK"), default=True
                ),
            )

    real_generation_allowed = (
        _truthy(os.getenv("OPEN_SPRITE_ALLOW_REAL_GENERATION"))
        if allow_real_generation is None
        else bool(allow_real_generation)
    )
    max_upload_mb = int(os.getenv("OPEN_SPRITE_MAX_UPLOAD_MB", "20"))
    store = UiAssetStore(resolved_ui_root, max_upload_bytes=max_upload_mb * 1024 * 1024)

    application = FastAPI(title="Open Sprite Pipeline", version="0.2.0")
    application.state.config_path = resolved_config
    application.state.store = store
    application.state.segmenter = segmenter
    application.state.allow_real_generation = real_generation_allowed
    application.state.real_generation_queue = GpuGenerationQueue(
        max_waiting=int(os.getenv("OPEN_SPRITE_GPU_QUEUE_LIMIT", "8"))
    )

    if WEB_ROOT.is_dir():
        application.mount("/assets", StaticFiles(directory=WEB_ROOT), name="ui-assets")

    def artifact_root() -> Path:
        config = load_config(application.state.config_path)
        configured = Path(config["app"]["artifact_root"])
        if not configured.is_absolute():
            configured = PROJECT_ROOT / configured
        configured.mkdir(parents=True, exist_ok=True)
        return configured.resolve()

    def artifact_path(relative_path: str) -> Path:
        root = artifact_root()
        candidate = (root / relative_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise UiStoreError("Artifact path escapes the configured artifact root.") from exc
        if not candidate.is_file():
            raise FileNotFoundError("Pipeline artifact not found.")
        return candidate

    def artifact_url(path_value: str | Path | None) -> str | None:
        if path_value is None:
            return None
        root = artifact_root()
        candidate = Path(path_value)
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        candidate = candidate.resolve()
        try:
            relative = candidate.relative_to(root)
        except ValueError as exc:
            raise UiStoreError("Pipeline returned an artifact outside its configured root.") from exc
        return f"/v1/artifacts/{quote(relative.as_posix(), safe='/')}"

    # --- UI run records: generation state that survives a page refresh.
    # Records live under the UI store root (bind-mounted in the container),
    # one JSON file per run, written at submission and updated on completion.
    def _runs_root() -> Path:
        root = resolved_ui_root / "generation_runs"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _write_run_record(record: dict[str, Any]) -> None:
        path = _runs_root() / f"{record['record_key']}.json"
        path.write_text(json.dumps(record, indent=1), encoding="utf-8")

    def _read_run_record(record_key: str) -> dict[str, Any] | None:
        if not record_key.replace("-", "").isalnum():
            return None
        path = _runs_root() / f"{record_key}.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None

    def _list_run_records(limit: int = 20) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in _runs_root().glob("*.json"):
            try:
                records.append(json.loads(path.read_text(encoding="utf-8")))
            except ValueError:
                continue
        records.sort(key=lambda record: str(record.get("submitted_at", "")), reverse=True)
        return records[:limit]

    @application.get("/", include_in_schema=False)
    def ui_index() -> FileResponse:
        index = WEB_ROOT / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=404, detail="Interactive UI assets are not installed.")
        return FileResponse(index)

    @application.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/v1/ui/status")
    def ui_status(request: Request) -> dict[str, Any]:
        queue_status = request.app.state.real_generation_queue.snapshot()
        return {
            "status": "ok",
            "segmentation": request.app.state.segmenter.status(),
            "generation": {
                "silhouette_preview": "ready",
                "mock_pipeline": "ready",
                "real_pipeline_allowed": request.app.state.allow_real_generation,
                "real_pipeline_busy": queue_status["busy"],
                "real_pipeline_queue_depth": queue_status["waiting"],
                "real_pipeline_queue_limit": queue_status["max_waiting"],
                "real_pipeline_boundary": (
                    None
                    if request.app.state.allow_real_generation
                    else "Disabled until the dedicated GPU lane is explicitly released."
                ),
            },
            "limits": {"max_upload_mb": max_upload_mb},
        }

    @application.post("/v1/ui/uploads")
    async def upload_image(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
        try:
            payload = await file.read(request.app.state.store.max_upload_bytes + 1)
            upload = request.app.state.store.create_upload(file.filename, payload)
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        return {
            **upload.to_dict(),
            "image_url": f"/v1/ui/uploads/{upload.image_id}/source",
        }

    @application.get("/v1/ui/uploads/{image_id}/source")
    def uploaded_image(request: Request, image_id: str) -> FileResponse:
        try:
            return FileResponse(request.app.state.store.upload_file(image_id))
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc

    @application.post("/v1/ui/uploads/{image_id}/segments")
    def segment_image(
        request: Request,
        image_id: str,
        payload: SegmentRequest,
    ) -> dict[str, Any]:
        try:
            upload = request.app.state.store.get_upload(image_id)
            points = [
                PromptPoint(x=point.x, y=point.y, label=point.label)
                for point in payload.points
            ]
            if not any(point.label == 1 for point in points):
                raise UiStoreError("At least one positive point is required.")
            for point in points:
                if point.x >= upload.width or point.y >= upload.height:
                    raise UiStoreError("A prompt point falls outside the uploaded image.")
            result = request.app.state.segmenter.segment(upload.source_path, points, payload.box)
            segment = request.app.state.store.save_segment(
                image_id, result, points, payload.box
            )
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        segment_id = segment["segment_id"]
        return {
            **{
                key: segment[key]
                for key in (
                    "image_id",
                    "segment_id",
                    "engine",
                    "score",
                    "fallback_reason",
                    "bbox",
                    "crop_bbox",
                    "coverage",
                )
            },
            "mask_url": f"/v1/ui/uploads/{image_id}/segments/{segment_id}/mask",
            "cutout_url": f"/v1/ui/uploads/{image_id}/segments/{segment_id}/cutout",
        }

    @application.get("/v1/ui/uploads/{image_id}/segments/{segment_id}/{kind}")
    def segment_artifact(
        request: Request,
        image_id: str,
        segment_id: str,
        kind: Literal["mask", "cutout", "metadata"],
    ) -> FileResponse:
        try:
            return FileResponse(request.app.state.store.segment_file(image_id, segment_id, kind))
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc

    @application.get("/v1/ui/uploads/{image_id}/segments/{segment_id}/mesh/{filename}")
    def segment_mesh_artifact(
        request: Request,
        image_id: str,
        segment_id: str,
        filename: str,
    ) -> FileResponse:
        try:
            return FileResponse(
                request.app.state.store.mesh_file(image_id, segment_id, filename)
            )
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc

    @application.post("/v1/ui/uploads/{image_id}/segments/{segment_id}/generate")
    def generate_segment(
        request: Request,
        image_id: str,
        segment_id: str,
        payload: SegmentGenerateRequest,
    ) -> dict[str, Any]:
        try:
            cutout = request.app.state.store.segment_cutout(image_id, segment_id)
            if payload.generation == "silhouette":
                generated = create_silhouette_mesh(
                    cutout,
                    request.app.state.store.mesh_dir(image_id, segment_id),
                    grid_size=payload.grid_size,
                    depth=payload.depth,
                )
                base = f"/v1/ui/uploads/{image_id}/segments/{segment_id}/mesh"
                return {
                    "status": "success",
                    "generation": generated,
                    "primary_asset_url": f"{base}/model.obj",
                    "material_url": f"{base}/model.mtl",
                    "texture_url": f"{base}/texture.png",
                    "asset_type": "obj",
                    "preview_urls": [],
                    "frame_urls": [],
                    "sprite_sheet_url": None,
                }

            if not payload.mock and not request.app.state.allow_real_generation:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Real generation is disabled while the dedicated GPU lane is occupied. "
                        "Use the silhouette preview or explicit mock pipeline."
                    ),
                )

            # persist the run before the (possibly ~20 minute) blocking call so
            # a refreshed page can poll /v1/ui/runs/{record_key}
            record_key = (
                f"ur-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
                f"-{uuid.uuid4().hex[:8]}"
            )
            run_record: dict[str, Any] = {
                "record_key": record_key,
                "status": "running",
                "image_id": image_id,
                "segment_id": segment_id,
                "generation": payload.generation,
                "mock": bool(payload.mock),
                "mode": payload.mode,
                "provider": payload.provider,
                "submitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            _write_run_record(run_record)

            def execute_pipeline() -> dict[str, Any]:
                orchestrator = build_orchestrator(
                    request.app.state.config_path,
                    mock=payload.mock,
                )
                return orchestrator.run(
                    image_path=cutout,
                    prompt=payload.prompt,
                    mode=payload.mode,
                    parts_hint=payload.parts_hint,
                    provider_override=payload.provider,
                )

            try:
                manifest = (
                    execute_pipeline()
                    if payload.mock
                    else request.app.state.real_generation_queue.run(execute_pipeline)
                )
            except Exception as exc:  # noqa: BLE001
                run_record["status"] = "failed"
                run_record["error"] = f"{type(exc).__name__}: {exc}"
                run_record["completed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                _write_run_record(run_record)
                raise

            item = manifest["items"][0] if manifest.get("items") else {}
            generation = item.get("generation") or {}
            rendering = item.get("rendering") or {}
            response = {
                "status": item.get("status", "failed"),
                "run_id": manifest.get("run_id"),
                "record_key": record_key,
                "manifest": manifest,
                "primary_asset_url": artifact_url(generation.get("primary_asset_path")),
                "asset_type": Path(generation.get("primary_asset_path", "")).suffix.lstrip("."),
                "preview_urls": [
                    artifact_url(path) for path in generation.get("preview_paths", [])
                ],
                "frame_urls": [
                    artifact_url(path) for path in rendering.get("frame_paths", [])
                ],
                "sprite_sheet_url": artifact_url(rendering.get("sprite_sheet_path")),
            }
            item_errors = [str(error) for error in (item.get("errors") or [])]
            run_record["status"] = "completed" if response["status"] == "success" else "failed"
            run_record["completed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            run_record["response"] = response
            if run_record["status"] == "failed":
                run_record["error"] = ("; ".join(item_errors) or "Generation did not return a mesh.")[:500]
            _write_run_record(run_record)
            return response
        except HTTPException:
            raise
        except GenerationQueueFullError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc

    @application.get("/v1/ui/runs")
    def ui_runs_list(limit: int = 20) -> dict[str, Any]:
        return {"runs": _list_run_records(max(1, min(limit, 100)))}

    @application.get("/v1/ui/runs/{record_key}")
    def ui_run_detail(record_key: str) -> dict[str, Any]:
        record = _read_run_record(record_key)
        if record is None:
            raise HTTPException(status_code=404, detail="Unknown run record.")
        return record

    @application.get("/v1/artifacts/{relative_path:path}")
    def pipeline_artifact(relative_path: str) -> FileResponse:
        try:
            return FileResponse(artifact_path(relative_path))
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc

    @application.post("/v1/pipeline/run")
    def run_pipeline(request: RunRequest) -> dict[str, Any]:
        if not application.state.config_path.exists():
            raise HTTPException(
                status_code=500,
                detail=f"Config not found: {application.state.config_path}",
            )
        orchestrator = build_orchestrator(application.state.config_path, mock=request.mock)
        try:

            def execute_pipeline() -> dict[str, Any]:
                return orchestrator.run(
                    image_path=request.image,
                    prompt=request.prompt,
                    mode=request.mode,
                    parts_hint=request.parts_hint,
                    provider_override=request.provider,
                )

            return (
                execute_pipeline()
                if request.mock
                else application.state.real_generation_queue.run(execute_pipeline)
            )
        except GenerationQueueFullError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return application


app = create_app()
