"""Opt-in Forge HTTP surface; no execution or model dependencies.

Multipart jobs accept ``files`` or ``files[]`` and JSON strings for ``params``,
``canonical_views`` and ``replacement_views``. Completion artifacts are JSON
values, UTF-8 strings, or {"encoding": "base64", "data": "..."} blobs. Claimed
jobs require the returned lease.lease_id on progress and completion; progress
without a state is a heartbeat. Critic claims return 204 until Phase 5.
"""
from __future__ import annotations

import base64
import binascii
import hmac
from io import BytesIO
import json
import os
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from PIL import Image, UnidentifiedImageError
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from .forge_store import ForgeConflict, ForgeStore, ForgeStoreError


class ForgeRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def mapped(request: Request):
            try:
                return await handler(request)
            except StarletteHTTPException as exc:
                if exc.status_code == 400 and request.method == "POST" and request.url.path == "/v1/forge/jobs":
                    raise HTTPException(422, exc.detail) from exc
                raise
            except FileNotFoundError as exc:
                raise HTTPException(404, "Forge record or artifact not found.") from exc
            except ForgeConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            except (ForgeStoreError, ValueError) as exc:
                # File helpers deliberately hide rejected filesystem paths.
                raise HTTPException(404 if request.method == "GET" else 422, str(exc)) from exc
        return mapped


forge_router = APIRouter(prefix="/v1/forge", route_class=ForgeRoute)


def store(request: Request) -> ForgeStore:
    return request.app.state.forge_store


def worker_auth(request: Request) -> None:
    token = os.getenv("FORGE_WORKER_TOKEN", "")
    supplied = request.headers.get("X-Forge-Worker", "")
    if token and not hmac.compare_digest(supplied.encode(), token.encode()):
        raise HTTPException(403, "Invalid Forge worker token.")


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PanelDecision(Payload):
    panel_id: str
    decision: Literal["accept", "repin", "reject"]
    view: str | None = None
    iou: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)


class Review(Payload):
    mode: Literal["draft", "submit"]
    panels: list[PanelDecision]
    views_missing: list[str] = Field(default_factory=list)


class Progress(Payload):
    state: str | None = None
    markers: list[str] = Field(default_factory=list, max_length=200)
    error: str | None = None
    lease_id: str | None = None


class Claim(Payload):
    kind: Literal["pipeline", "critic"] = "pipeline"
    stages: list[Literal["matching", "review"]] | None = None


class CanonicalViews(Payload):
    canonical_views: list[str]
    lease_id: str


class MatchReport(Payload):
    report: dict[str, Any]
    lease_id: str


class Lease(Payload):
    lease_id: str


class Complete(Payload):
    job_id: str
    artifacts: dict[str, Any] = Field(default_factory=dict, max_length=64)
    metrics: dict[str, Any] = Field(default_factory=dict)
    lease_id: str | None = None


class Accept(Payload):
    accepted: bool = True


class Note(Payload):
    author: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=10000)


@forge_router.get("/status")
def status(request: Request):
    return store(request).status()


@forge_router.get("/assets")
def assets(request: Request):
    return store(request).list_assets()


@forge_router.post("/jobs", status_code=201)
async def create_job(request: Request):
    async with request.form(max_files=8, max_fields=16) as form:
        files = form.getlist("files") + form.getlist("files[]")
        if not 1 <= len(files) <= 8 or not all(isinstance(f, UploadFile) for f in files):
            raise HTTPException(422, "Upload between one and eight images.")
        params = json.loads(form.get("params", "{}"))
        if not isinstance(params, dict):
            raise ForgeStoreError("Params must be a JSON object.")
        canonical = form.get("canonical_views")
        # Accept the Phase 1 canonical list in params as well as a form field.
        embedded_views = params.pop("canonical_views", None)
        canonical = json.loads(canonical) if canonical is not None else embedded_views
        replacements = json.loads(form.get("replacement_views", "[]"))
        parent_version = form.get("parent_version")
        prepared = []
        for upload in files:
            data = await upload.read(20 * 1024 * 1024 + 1)
            if not data or len(data) > 20 * 1024 * 1024:
                raise ForgeStoreError("Each image must be nonempty and at most 20 MiB.")
            try:
                with Image.open(BytesIO(data)) as image:
                    media_type = Image.MIME.get(image.format)
                    if image.width * image.height > 36_000_000:
                        raise ForgeStoreError("Image exceeds 36 million pixels.")
                    image.verify()
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
                raise ForgeStoreError("Upload is not a supported image.") from exc
            if media_type not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
                raise ForgeStoreError("Unsupported image format.")
            prepared.append((upload.filename or "image", media_type, data))
        target = store(request)
        # Keep a job invisible to concurrent claims until every upload is stored.
        with target._lock:
            job = target.create_job(
                form.get("asset", ""), form.get("variant", ""), params,
                canonical_views=canonical, intent=form.get("intent", "fresh"),
                parent_job=form.get("parent_job"),
                parent_version=int(parent_version) if parent_version is not None else None,
                replacement_views=replacements,
            )
            try:
                for filename, media_type, data in prepared:
                    target.record_upload(job["id"], filename, media_type, data)
            except Exception:
                target.delete_job(job["id"])
                raise
            return target.get_job(job["id"])


@forge_router.get("/jobs")
def jobs(request: Request, state: str | None = None, asset: str | None = None):
    return store(request).list_jobs(state=state, asset=asset)


@forge_router.get("/jobs/{job_id}")
def job(request: Request, job_id: str):
    return store(request).get_job(job_id)


@forge_router.delete("/jobs/{job_id}", status_code=204)
def delete_job(request: Request, job_id: str):
    store(request).delete_job(job_id)
    return Response(status_code=204)


@forge_router.get("/jobs/{job_id}/uploads/{index}")
def upload(request: Request, job_id: str, index: int):
    path, media_type = store(request).upload_file(job_id, index)
    return FileResponse(path, media_type=media_type)


@forge_router.get("/jobs/{job_id}/panels/{panel_id}")
def panel(request: Request, job_id: str, panel_id: str):
    return FileResponse(store(request).panel_file(job_id, panel_id), media_type="image/png")


@forge_router.post("/jobs/{job_id}/review")
def review(request: Request, job_id: str, body: Review):
    return store(request).review(job_id, body.mode, [p.model_dump() for p in body.panels], body.views_missing)


@forge_router.patch("/jobs/{job_id}", dependencies=[Depends(worker_auth)])
def canonical_views(request: Request, job_id: str, body: CanonicalViews):
    return store(request).patch_canonical_views(job_id, body.canonical_views, body.lease_id)


@forge_router.post("/jobs/{job_id}/match", dependencies=[Depends(worker_auth)])
def match_report(request: Request, job_id: str, body: MatchReport):
    return store(request).worker_match(job_id, body.report, body.lease_id)


async def worker_image(request: Request) -> tuple[bytes, str]:
    # Host worker owns image processing; this endpoint only stores opaque bytes.
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > 20 * 1024 * 1024:
            raise ForgeStoreError("Worker images are limited to 20 MiB.")
    if not payload:
        raise ForgeStoreError("Worker image must be nonempty.")
    return bytes(payload), request.headers.get("X-Forge-Lease", "")


@forge_router.post("/jobs/{job_id}/panels/{panel_id}", dependencies=[Depends(worker_auth)])
async def save_panel(request: Request, job_id: str, panel_id: str):
    payload, lease = await worker_image(request)
    store(request).worker_panel(job_id, panel_id, payload, lease)
    return {"panel_id": panel_id}


@forge_router.post("/jobs/{job_id}/staged/views/{view}.png", dependencies=[Depends(worker_auth)])
async def staged_view(request: Request, job_id: str, view: str):
    payload, lease = await worker_image(request)
    store(request).worker_staged_view(job_id, view, payload, lease)
    return {"view": view}


@forge_router.post("/jobs/{job_id}/staged", dependencies=[Depends(worker_auth)])
def staged(request: Request, job_id: str, body: Lease):
    return store(request).finish_staging(job_id, body.lease_id)


@forge_router.post("/jobs/{job_id}/approve")
def approve(request: Request, job_id: str):
    return store(request).set_state(job_id, "queued_bake")


@forge_router.post("/jobs/{job_id}/progress", dependencies=[Depends(worker_auth)])
def progress(request: Request, job_id: str, body: Progress):
    return store(request).progress(job_id, **body.model_dump())


@forge_router.post("/worker/claim", dependencies=[Depends(worker_auth)])
def claim(request: Request, body: Claim):
    claimed = store(request).claim_job(body.kind, body.stages)
    return claimed if claimed is not None else Response(status_code=204)


@forge_router.post("/worker/complete", dependencies=[Depends(worker_auth)])
def complete(request: Request, body: Complete):
    artifacts = {}
    total = 0
    for name, value in body.artifacts.items():
        if isinstance(value, dict) and value.get("encoding") == "base64":
            if set(value) != {"encoding", "data"} or not isinstance(value["data"], str):
                raise ForgeStoreError("Blob artifacts require encoding and base64 data.")
            try:
                value = base64.b64decode(value["data"], validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ForgeStoreError("Invalid base64 artifact.") from exc
        size = len(value) if isinstance(value, bytes) else len(json.dumps(value, allow_nan=False).encode())
        total += size
        if total > 8 * 1024 * 1024:
            raise ForgeStoreError("Completion artifacts are limited to 8 MiB total.")
        artifacts[name] = value
    return store(request).complete(body.job_id, artifacts=artifacts, metrics=body.metrics, lease_id=body.lease_id)


@forge_router.get("/library")
def library(request: Request, asset: str | None = None, origin: str | None = None,
            accepted: bool | None = None, q: str | None = None):
    return store(request).list_versions(asset=asset, origin=origin, accepted=accepted, q=q)


_VERSION = "/assets/{asset}/variants/{variant}/versions"


@forge_router.get(_VERSION)
def versions(request: Request, asset: str, variant: str):
    return store(request).list_versions(asset, variant)


@forge_router.get(_VERSION + "/{number}")
def version(request: Request, asset: str, variant: str, number: int):
    return store(request).get_version(asset, variant, number)


@forge_router.get(_VERSION + "/{number}/artifacts/{path:path}")
def artifact(request: Request, asset: str, variant: str, number: int, path: str):
    return FileResponse(store(request).artifact_file(asset, variant, number, path))


@forge_router.post(_VERSION + "/{number}/accept")
def accept(request: Request, asset: str, variant: str, number: int, body: Accept | None = None):
    return store(request).accept_version(asset, variant, number, body.accepted if body else True)


@forge_router.post(_VERSION + "/{number}/notes")
def note(request: Request, asset: str, variant: str, number: int, body: Note):
    return store(request).append_note(asset=asset, variant=variant, number=number, **body.model_dump())


@forge_router.get(_VERSION + "/{number}/layers")
def layers(request: Request, asset: str, variant: str, number: int):
    return store(request).get_version(asset, variant, number)["metrics"].get("part_layers", {})
