"""Loopback style sidecar: lazy GPU admission, serialized inference, exact alpha."""
from __future__ import annotations

import base64
import binascii
import io
import logging
import os
import threading
import time
import warnings
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from .style_backend import RenderParams, StyleBackend, build_backend
from .style_compose import crop_box, from_working, to_working

LOG = logging.getLogger("uvicorn.error.style")


@dataclass(frozen=True)
class StyleSettings:
    backend: str = "diffusers"
    device: str = "cuda:0"
    base_model: str = "stable-diffusion-v1-5/stable-diffusion-v1-5"
    controlnet: str = "lllyasviel/control_v11f1p_sd15_depth"
    ip_adapter: str = "h94/IP-Adapter"
    min_free_mb: int = 4500
    idle_unload_s: float = 180.0
    max_refs: int = 6
    max_seeds: int = 6
    frame_size: int = 2048
    max_image_bytes: int = 25_000_000

    @classmethod
    def from_env(cls) -> StyleSettings:
        defaults = cls()
        values = {}
        for name in cls.__dataclass_fields__:
            default = getattr(defaults, name)
            values[name] = type(default)(os.environ.get(f"STYLE_{name.upper()}", default))
        settings = cls(**values)
        if settings.backend not in ("diffusers", "fake"):
            raise ValueError("STYLE_BACKEND must be diffusers or fake")
        if any(getattr(settings, key) <= 0 for key in
               ("max_seeds", "frame_size", "max_image_bytes")):
            raise ValueError("style image and seed limits must be positive")
        if settings.max_refs < 0 or settings.min_free_mb < 0 or settings.idle_unload_s < 0:
            raise ValueError("style limits must be nonnegative")
        return settings


class RenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    view: str
    init_png: str
    depth_png: str
    refs: list[str] = Field(default_factory=list)
    prompt: str
    negative: str = ""
    seeds: list[StrictInt]
    strength: float = Field(default=0.62, gt=0, le=1)
    guidance: float = Field(default=6.5, ge=0)
    control_scale: float = Field(default=0.8, ge=0)
    ip_scale: float = Field(default=0.6, ge=0)
    steps: int = Field(default=28, strict=True, ge=1)
    long_side: int = Field(default=768, strict=True, ge=512, le=1024)

    @field_validator("long_side")
    @classmethod
    def multiple_of_64(cls, value: int) -> int:
        if value % 64:
            raise ValueError("long_side must be a multiple of 64")
        return value

    @field_validator("seeds")
    @classmethod
    def seed_range(cls, value: list[int]) -> list[int]:
        if any(seed < -(1 << 63) or seed >= (1 << 64) for seed in value):
            raise ValueError("seeds must be in torch's range [-2**63, 2**64-1]")
        return value

    @model_validator(mode="after")
    def positive_steps(self):
        if int(self.steps * self.strength) < 1:
            raise ValueError("strength * steps must provide at least one denoising step")
        return self


def decode_png(encoded: str, label: str, settings: StyleSettings) -> Image.Image:
    def invalid(reason: str):
        raise HTTPException(422, detail=f"{label}: {reason}")

    if len(encoded) > 4 * ((settings.max_image_bytes + 2) // 3):
        invalid("image exceeds STYLE_MAX_IMAGE_BYTES")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        invalid("invalid base64")
    if len(raw) > settings.max_image_bytes:
        invalid("image exceeds STYLE_MAX_IMAGE_BYTES")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format != "PNG":
                    invalid("image must be a PNG")
                image.verify()
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
                return image.copy()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        invalid("invalid or unsafe PNG")


def encode_png(image: Image.Image) -> str:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


class StyleState:
    def __init__(self, backend: StyleBackend, settings: StyleSettings, clock) -> None:
        self.backend = backend
        self.settings = settings
        self.clock = clock
        self.lock = threading.Lock()
        self.last_used: float | None = clock() if backend.loaded else None
        self.renders = 0

    def free_mb(self) -> float | None:
        if not self.backend.needs_gpu:
            return None
        return getattr(self.backend, "free_mb", lambda: None)()

    def maybe_unload(self, now: float) -> bool:
        if not self.lock.acquire(blocking=False):
            return False
        try:
            if (self.backend.loaded and self.last_used is not None
                    and now - self.last_used >= self.settings.idle_unload_s):
                self.backend.unload()
                return True
            return False
        finally:
            self.lock.release()


def create_style_app(backend: StyleBackend | None = None, *, clock=time.monotonic) -> FastAPI:
    settings = StyleSettings.from_env()
    state = StyleState(backend if backend is not None else build_backend(settings.backend, settings),
                       settings, clock)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stopped = threading.Event()

        def idle_worker():
            while not stopped.wait(15):
                try:
                    state.maybe_unload(clock())
                except Exception:
                    LOG.exception("style idle unload failed")

        worker = threading.Thread(target=idle_worker, name="style-idle-unload", daemon=True)
        worker.start()
        try:
            yield
        finally:
            stopped.set()
            worker.join(timeout=1)
            with state.lock:
                if state.backend.loaded:
                    state.backend.unload()

    app = FastAPI(title="Object Forge Style", lifespan=lifespan)
    app.state.style = state

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/v1/style/status")
    def status():
        return {
            "backend": state.backend.name, "loaded": state.backend.loaded,
            "busy": state.lock.locked(), "device": settings.device,
            "free_mb": state.free_mb(),
            "models": {"base": settings.base_model, "controlnet": settings.controlnet,
                       "ip_adapter": settings.ip_adapter},
            "last_used_s": None if state.last_used is None else max(0, clock() - state.last_used),
            "renders": state.renders,
        }

    @app.post("/v1/style/unload")
    def unload():
        with state.lock:
            if state.backend.loaded:
                state.backend.unload()
        return {"loaded": False}

    @app.post("/v1/style/render")
    def render(request: RenderRequest):
        if not 1 <= len(request.seeds) <= settings.max_seeds:
            raise HTTPException(422, detail=f"seeds must contain 1 to {settings.max_seeds} integers")
        if len(request.refs) > settings.max_refs:
            raise HTTPException(422, detail=f"refs must contain at most {settings.max_refs} images")
        init = decode_png(request.init_png, "init_png", settings)
        if init.mode != "RGBA" or init.size != (settings.frame_size, settings.frame_size):
            raise HTTPException(422, detail=f"init_png must be {settings.frame_size} square RGBA")
        mask = init.getchannel("A")
        if mask.getbbox() is None:
            raise HTTPException(422, detail="init_png alpha must be non-empty")
        depth = decode_png(request.depth_png, "depth_png", settings)
        if depth.size != init.size or depth.mode not in ("L", "I", "I;16"):
            raise HTTPException(422, detail="depth_png must match init size and have mode L/I/I;16")
        refs = [decode_png(ref, f"refs[{i}]", settings).convert("RGB")
                for i, ref in enumerate(request.refs)]
        try:
            box = crop_box(mask)
        except ValueError:
            # The twin uses alpha > 8 for cropping; retain the API's promise to
            # accept any nonempty alpha, including an entirely faint silhouette.
            box = crop_box(mask.point(lambda p: 255 if p else 0))
        params = RenderParams(request.strength, request.guidance, request.control_scale,
                              request.ip_scale, request.steps)
        with state.lock:
            # Recheck even when loaded: the other GPU lane can grow between calls.
            if state.backend.needs_gpu:
                free = state.free_mb()
                if free is None or free < settings.min_free_mb:
                    return JSONResponse(status_code=503, content={
                        "error": "gpu_busy", "free_mb": free, "needed_mb": settings.min_free_mb})
            started = time.perf_counter()
            candidates = []
            peak = None
            try:
                if not state.backend.loaded:
                    state.backend.load()
                init_rgb, depth_l, scale = to_working(init, depth, box, long_side=request.long_side)
                tokens = state.backend.count_tokens(request.prompt)
                for seed in request.seeds:
                    seed_start = time.perf_counter()
                    styled = state.backend.render(init_rgb, depth_l, refs, request.prompt,
                                                  request.negative, seed, params)
                    if styled.mode != "RGB" or styled.size != init_rgb.size:
                        raise RuntimeError("backend must return RGB at working size")
                    candidate = from_working(styled, box, frame_size=settings.frame_size, mask=mask)
                    candidates.append({"seed": seed, "png": encode_png(candidate),
                                       "working_size": list(init_rgb.size),
                                       "seconds": time.perf_counter() - seed_start})
                    state.renders += 1
                    measured = getattr(state.backend, "peak_mb", lambda: None)()
                    if measured is not None:
                        peak = measured if peak is None else max(peak, measured)
            except Exception:
                # Release failed inference's resident model before accepting work.
                if state.backend.loaded:
                    state.backend.unload()
                raise
            finally:
                state.last_used = clock()
            LOG.info("style render view=%r seeds=%s working_size=%s seconds=%.3f peak_mb=%s",
                     request.view, request.seeds, init_rgb.size, time.perf_counter() - started, peak)
            return {"candidates": candidates, "box": list(box), "scale": scale,
                    "prompt_tokens": tokens, "truncated": tokens is not None and tokens > 77,
                    "model": state.backend.name, "peak_mb": peak}

    return app


app = create_style_app()
