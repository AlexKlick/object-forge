"""API-owned Forge state. Run one API writer process per store root.

Worker processes use the HTTP API; they never edit records. The reentrant lock
serializes request threads, including claims and version allocation. JSON files
are replaced atomically. Version directories are published before their derived
index; listing rebuilds that index after an interrupted publication.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import wraps
import json
import math
import os
from pathlib import Path
import re
import shutil
from threading import RLock
from typing import Any
from uuid import uuid4

DEFAULT_PARAMS = {
    "iou": 0.70, "margin": 0.05, "allow_extra": False, "ownership_min": 0.5,
    "view_iou_warn": 0.90, "view_iou_fail": None, "atlas_tile": 1024, "turntable": 8,
}
STATES = ("uploaded", "matching", "review", "staged", "queued_bake", "baking", "ready")
TRANSITIONS = {a: {b, "failed"} for a, b in zip(STATES, STATES[1:])}
TRANSITIONS.update(ready=set(), failed=set())
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_IMAGE_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


class ForgeStoreError(ValueError):
    pass


class ForgeConflict(ForgeStoreError):
    pass


def locked(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class ForgeStore:
    def __init__(self, root: str | Path) -> None:
        Path(root).mkdir(parents=True, exist_ok=True)
        self.root = Path(root).resolve()
        self._lock = RLock()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _name(value: str) -> str:
        if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value) or ".." in value:
            raise ForgeStoreError("Invalid path identifier.")
        return value

    def confined(self, relative: str | Path) -> Path:
        value = str(relative)
        path = Path(value)
        if not path.parts or path.is_absolute() or ".." in path.parts or "\\" in value or "\x00" in value:
            raise ForgeStoreError("Invalid artifact path.")
        candidate = self.root / path
        # Reject all links, including internal aliases and dangling tmp links.
        for component in (candidate, *candidate.parents):
            if component == self.root:
                break
            if component.is_symlink():
                raise ForgeStoreError("Symlinks are not allowed in the Forge store.")
        if not candidate.resolve().is_relative_to(self.root):
            raise ForgeStoreError("Artifact path escapes the Forge root.")
        return candidate

    def _checked(self, path: Path) -> Path:
        return self.confined(path.relative_to(self.root))

    def _write_bytes(self, path: Path, data: bytes) -> None:
        path = self._checked(path)
        tmp = self._checked(path.with_name(path.name + ".tmp"))
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with tmp.open("wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def _write_json(self, path: Path, data: Any) -> None:
        # Serialize first, so invalid JSON cannot leave a partial temporary file.
        self._write_bytes(path, json.dumps(data, indent=2, allow_nan=False).encode("utf-8"))

    def _read(self, path: Path) -> Any:
        return json.loads(self._checked(path).read_text(encoding="utf-8"))

    def _variant_dir(self, asset: str, variant: str) -> Path:
        return self.confined(Path("assets") / self._name(asset) / "variants" / self._name(variant))

    def _job_path(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise ForgeStoreError("Invalid job id.")
        for path in self.root.glob(f"assets/*/variants/*/jobs/{job_id}/job.json"):
            return self._checked(path)
        raise FileNotFoundError("Job not found.")

    def _save_job(self, job: dict) -> dict:
        job["updated_at"] = self._now().isoformat()
        path = self._variant_dir(job["asset"], job["variant"]) / "jobs" / job["id"] / "job.json"
        self._write_json(path, job)
        return job

    @staticmethod
    def validate_params(params: dict | None) -> dict:
        if params is None:
            params = {}
        if not isinstance(params, dict) or set(params) - DEFAULT_PARAMS.keys():
            raise ForgeStoreError("Params must be an object containing supported parameter names.")
        values = {**DEFAULT_PARAMS, **params}
        for key in ("iou", "margin", "ownership_min", "view_iou_warn", "view_iou_fail"):
            value = values[key]
            if value is None and key == "view_iou_fail":
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ForgeStoreError(f"{key} must be a finite number between 0 and 1.")
        if type(values["allow_extra"]) is not bool:
            raise ForgeStoreError("allow_extra must be a boolean.")
        for key in ("atlas_tile", "turntable"):
            if type(values[key]) is not int or values[key] < 1:
                raise ForgeStoreError(f"{key} must be a positive integer.")
        return values

    def _views(self, values: list) -> list[str]:
        if not isinstance(values, list):
            raise ForgeStoreError("Views must be a list.")
        for view in values:
            self._name(view)
        if len(set(values)) != len(values):
            raise ForgeStoreError("Views must be unique.")
        return list(values)

    @locked
    def create_job(self, asset: str, variant: str, params: dict | None = None, *,
                   canonical_views: list[str] | None = None, intent: str = "fresh",
                   parent_job: str | None = None, parent_version: int | None = None,
                   replacement_views: list[str] | None = None) -> dict:
        self._variant_dir(asset, variant)
        values = self.validate_params(params)
        views = self._views([] if canonical_views is None else canonical_views)
        replacements = self._views([] if replacement_views is None else replacement_views)
        if intent not in {"fresh", "iterate_views", "iterate_params"}:
            raise ForgeStoreError("Invalid intent.")
        if intent == "fresh":
            if parent_job is not None or parent_version is not None or replacements:
                raise ForgeStoreError("Fresh jobs cannot have parents or replacement views.")
        else:
            if parent_job is None or parent_version is None:
                raise ForgeStoreError("Iteration requires parent_job and parent_version.")
            parent = self.get_job(parent_job)
            version = self.get_version(asset, variant, parent_version)
            if (parent["asset"], parent["variant"]) != (asset, variant) or version["job_id"] != parent_job:
                raise ForgeStoreError("Iteration parents must identify the same asset, variant and bake.")
            if canonical_views is None:
                views = parent["canonical_views"]
        if set(replacements) - set(views):
            raise ForgeStoreError("Replacement views must be canonical views.")
        job = {
            "id": uuid4().hex, "asset": asset, "variant": variant,
            "parent_job": parent_job, "parent_version": parent_version, "intent": intent,
            "state": "uploaded", "params": values, "canonical_views": views,
            "replacement_views": replacements, "uploads": [],
            "match": {"panels": [], "decisions": [], "views_missing": []},
            "created_at": self._now().isoformat(), "updated_at": self._now().isoformat(),
            "error": None, "accepted": False, "notes": [], "lease": None,
        }
        return self._save_job(job)

    @locked
    def get_job(self, job_id: str) -> dict:
        return self._read(self._job_path(job_id))

    @locked
    def list_jobs(self, *, state: str | None = None, asset: str | None = None) -> list[dict]:
        jobs = [self._read(p) for p in self.root.glob("assets/*/variants/*/jobs/*/job.json")]
        return sorted((j for j in jobs if (state is None or j["state"] == state)
                       and (asset is None or j["asset"] == asset)), key=lambda j: (j["created_at"], j["id"]))

    @locked
    def delete_job(self, job_id: str) -> None:
        job = self.get_job(job_id)
        if job["state"] == "baking":
            raise ForgeConflict("Cannot delete a baking job.")
        shutil.rmtree(self._job_path(job_id).parent)

    @locked
    def set_state(self, job_id: str, state: str, *, error: str | None = None) -> dict:
        job = self.get_job(job_id)
        if state not in TRANSITIONS[job["state"]]:
            raise ForgeConflict(f"Illegal transition {job['state']} -> {state}.")
        job["state"] = state
        job["error"] = error
        job["lease"] = None
        return self._save_job(job)

    @locked
    def record_upload(self, job_id: str, filename: str, media_type: str, payload: bytes) -> dict:
        job = self.get_job(job_id)
        if job["state"] != "uploaded" or len(job["uploads"]) >= 8:
            raise ForgeConflict("Uploads require an uploaded job with fewer than eight images.")
        if media_type not in _IMAGE_EXT or not payload:
            raise ForgeStoreError("A supported, nonempty image is required.")
        index = len(job["uploads"])
        stored = f"{index}.{_IMAGE_EXT[media_type]}"
        self._write_bytes(self._job_path(job_id).parent / "uploads" / stored, payload)
        record = {"index": index, "filename": Path(filename).name[:240], "media_type": media_type}
        job["uploads"].append(record)
        self._save_job(job)
        return record

    @locked
    def upload_file(self, job_id: str, index: int) -> tuple[Path, str]:
        job = self.get_job(job_id)
        upload = next((u for u in job["uploads"] if u["index"] == index), None)
        if upload is None:
            raise FileNotFoundError("Upload not found.")
        path = self._job_path(job_id).parent / "uploads" / f"{index}.{_IMAGE_EXT[upload['media_type']]}"
        return self._file(path), upload["media_type"]

    def _file(self, path: Path) -> Path:
        path = self._checked(path)
        if not path.is_file():
            raise FileNotFoundError("Artifact not found.")
        return path

    @locked
    def save_match_report(self, job_id: str, report: dict) -> dict:
        job = self.get_job(job_id)
        if job["state"] != "matching":
            raise ForgeConflict("Match reports require a matching job.")
        if not isinstance(report, dict) or not isinstance(report.get("panels", []), list):
            raise ForgeStoreError("Match report must contain panel findings.")
        self._write_json(self._job_path(job_id).parent / "match" / "match_report.json", report)
        job["match"].update(deepcopy(report))
        job["match"]["report_saved"] = True
        job["match"]["submitted"] = False
        return self._save_job(job)

    @locked
    def save_panel(self, job_id: str, panel_id: str, payload: bytes) -> Path:
        path = self._job_path(job_id).parent / "match" / "panels" / f"{self._name(panel_id)}.png"
        self._write_bytes(path, payload)
        return path

    @locked
    def panel_file(self, job_id: str, panel_id: str) -> Path:
        return self._file(self._job_path(job_id).parent / "match" / "panels" / f"{self._name(panel_id)}.png")

    @locked
    def save_staged_view(self, job_id: str, view: str, payload: bytes) -> Path:
        job = self.get_job(job_id)
        if view not in job["canonical_views"]:
            raise ForgeStoreError("Unknown canonical view.")
        path = self._job_path(job_id).parent / "staged" / "views" / f"{self._name(view)}.png"
        self._write_bytes(path, payload)
        return path

    @locked
    def review(self, job_id: str, mode: str, panels: list[dict], views_missing: list[str]) -> dict:
        job = self.get_job(job_id)
        if job["state"] != "review":
            raise ForgeConflict("Review decisions require a review job.")
        if mode not in {"draft", "submit"}:
            raise ForgeStoreError("Invalid review mode.")
        if job["match"].get("submitted"):
            raise ForgeConflict("Submitted review decisions are immutable.")
        canonical = set(job["canonical_views"])
        missing = set(self._views(views_missing))
        covered, ids = set(), set()
        for panel in panels:
            panel_id = self._name(panel["panel_id"])
            if panel_id in ids or panel["decision"] not in {"accept", "repin", "reject"}:
                raise ForgeStoreError("Invalid or duplicate panel decision.")
            ids.add(panel_id)
            iou = panel.get("iou")
            if iou is not None and (type(iou) not in (int, float) or not math.isfinite(iou) or not 0 <= iou <= 1):
                raise ForgeStoreError("Panel IoU must be between 0 and 1.")
            if panel["decision"] in {"accept", "repin"}:
                view = panel.get("view")
                if view not in canonical or view in covered:
                    raise ForgeStoreError("Accepted views must be canonical and unique.")
                covered.add(view)
        if missing - canonical or missing & covered:
            raise ForgeStoreError("Missing views must be uncovered canonical views.")
        if mode == "submit" and (not canonical or canonical - covered - missing):
            raise ForgeStoreError("Every canonical view must be covered or explicitly missing.")
        if job["match"].get("report_saved"):
            known = {p["panel_id"] for p in job["match"]["panels"]}
            if ids - known or (mode == "submit" and ids != known):
                raise ForgeStoreError("Every panel requires exactly one decision on submit.")
        job["match"]["decisions"] = deepcopy(panels)
        job["match"]["views_missing"] = sorted(missing)
        if mode == "submit":
            if job["match"].get("report_saved"):
                job["match"]["submitted"] = True
            else:  # Preserve P1 reportless/manual review clients.
                job["state"] = "staged"
        return self._save_job(job)

    def _worker_job(self, job_id: str, lease_id: str, state: str) -> dict:
        job = self.get_job(job_id)
        if not job["lease"] or job["state"] != state:
            raise ForgeConflict("Worker mutation requires a claimed job in the expected state.")
        self._check_lease(job, lease_id)
        return job

    @locked
    def patch_canonical_views(self, job_id: str, views: list[str], lease_id: str) -> dict:
        job = self._worker_job(job_id, lease_id, "matching")
        views = self._views(views)
        if not views or set(job["replacement_views"]) - set(views):
            raise ForgeStoreError("Render views must cover replacement views and be nonempty.")
        job["canonical_views"] = views
        return self._save_job(job)

    @locked
    def worker_panel(self, job_id: str, panel_id: str, payload: bytes, lease_id: str) -> None:
        self._worker_job(job_id, lease_id, "matching")
        self.save_panel(job_id, panel_id, payload)

    @locked
    def worker_match(self, job_id: str, report: dict, lease_id: str) -> dict:
        self._worker_job(job_id, lease_id, "matching")
        panels = report.get("panels")
        if not isinstance(panels, list):
            raise ForgeStoreError("Match report requires panels.")
        if any(not isinstance(p, dict) or "panel_id" not in p for p in panels):
            raise ForgeStoreError("Every finding requires a panel_id.")
        ids = [self._name(p["panel_id"]) for p in panels]
        if len(set(ids)) != len(ids):
            raise ForgeStoreError("Duplicate panel findings.")
        for panel_id in ids:
            self.panel_file(job_id, panel_id)
        allowed = {"panels", "views_claimed", "views_missing", "extras", "rejects", "extras_allowed"}
        if set(report) - allowed:
            raise ForgeStoreError("Unknown match report field.")
        self.save_match_report(job_id, report)
        return self.set_state(job_id, "review")

    @locked
    def worker_staged_view(self, job_id: str, view: str, payload: bytes, lease_id: str) -> None:
        job = self._worker_job(job_id, lease_id, "review")
        accepted = {p["view"] for p in job["match"]["decisions"] if p["decision"] != "reject"}
        if not job["match"].get("submitted") or view not in accepted:
            raise ForgeConflict("Staged images require submitted decisions for the view.")
        self.save_staged_view(job_id, view, payload)

    @locked
    def finish_staging(self, job_id: str, lease_id: str) -> dict:
        job = self._worker_job(job_id, lease_id, "review")
        if not job["match"].get("submitted"):
            raise ForgeConflict("Review has not been submitted.")
        expected = {p["view"] for p in job["match"]["decisions"] if p["decision"] != "reject"}
        directory = self._checked(self._job_path(job_id).parent / "staged" / "views")
        actual = {p.stem for p in directory.glob("*.png")}
        if actual != expected:
            raise ForgeConflict("Staged images must exactly cover submitted decisions.")
        for view in expected:
            self._file(directory / f"{view}.png")
        return self.set_state(job_id, "staged")

    @locked
    def append_worker_log(self, job_id: str, lines: list[str]) -> None:
        path = self._checked(self._job_path(job_id).parent / "worker.log")
        retained = deque(path.read_text(encoding="utf-8").splitlines() if path.exists() else [], maxlen=200)
        for line in lines:
            retained.extend(line.splitlines())
        self._write_bytes(path, ("\n".join(retained) + ("\n" if retained else "")).encode("utf-8"))

    @locked
    def claim_job(self, kind: str = "pipeline", stages: list[str] | None = None) -> dict | None:
        if kind not in {"pipeline", "critic"}:
            raise ForgeStoreError("Unknown worker kind.")
        if kind == "critic":  # Reserved; critic execution is Phase 5.
            return None
        now = self._now()
        for job in self.list_jobs():
            target = {"uploaded": "matching", "queued_bake": "baking"}.get(job["state"], job["state"])
            if stages is not None and target not in stages:
                continue
            lease = job["lease"]
            if lease and datetime.fromisoformat(lease["lease_expires_at"]) > now:
                continue
            if job["state"] in {"uploaded", "queued_bake"}:
                job["state"] = {"uploaded": "matching", "queued_bake": "baking"}[job["state"]]
            elif job["state"] == "review" and job["match"].get("submitted"):
                pass
            elif job["state"] == "matching":
                pass
            elif not lease or job["state"] != "baking":
                continue
            job["lease"] = {"kind": kind, "lease_id": uuid4().hex,
                            "lease_expires_at": (now + timedelta(seconds=300)).isoformat()}
            return self._save_job(job)
        return None

    def _check_lease(self, job: dict, lease_id: str | None) -> None:
        lease = job["lease"]
        if lease is None:
            if lease_id is not None:
                raise ForgeConflict("Job has no active lease.")
            return
        if lease_id != lease["lease_id"] or datetime.fromisoformat(lease["lease_expires_at"]) <= self._now():
            raise ForgeConflict("Missing, stale or expired worker lease.")

    @locked
    def heartbeat(self, job_id: str, lease_id: str) -> dict:
        job = self.get_job(job_id)
        self._check_lease(job, lease_id)
        job["lease"]["lease_expires_at"] = (self._now() + timedelta(seconds=300)).isoformat()
        return self._save_job(job)

    @locked
    def progress(self, job_id: str, *, state: str | None = None, markers: list[str] | None = None,
                 error: str | None = None, lease_id: str | None = None) -> dict:
        job = self.get_job(job_id)
        self._check_lease(job, lease_id)
        if job["state"] in {"ready", "failed"}:
            raise ForgeConflict("Terminal jobs cannot receive progress.")
        if state is not None:
            if state not in {"matching", "review", "baking", "failed"} or state not in TRANSITIONS[job["state"]]:
                raise ForgeConflict("Progress cannot bypass review, approval or completion.")
        if markers:
            self.append_worker_log(job_id, markers)
        if state is not None:
            return self.set_state(job_id, state, error=error)
        if job["lease"]:
            job = self.heartbeat(job_id, lease_id)
        job["error"] = error
        return self._save_job(job)

    def _version_dir(self, asset: str, variant: str, number: int) -> Path:
        if type(number) is not int or number < 1:
            raise ForgeStoreError("Version number must be a positive integer.")
        return self._checked(self._variant_dir(asset, variant) / "versions" / f"v{number}")

    @staticmethod
    def _summary(version: dict) -> dict:
        return {key: version[key] for key in ("number", "asset", "variant", "origin", "job_id",
                                              "created_at", "accepted", "metrics", "notes")}

    def _index(self, asset: str, variant: str) -> list[dict]:
        directory = self._variant_dir(asset, variant)
        versions = [self._read(p) for p in directory.glob("versions/v*/version.json")]
        summaries = [self._summary(v) for v in sorted(versions, key=lambda v: v["number"])]
        path = directory / "versions.json"
        if not path.exists() or self._read(path) != summaries:
            self._write_json(path, summaries)
        return summaries

    @locked
    def create_version(self, job_id: str, *, artifacts: dict[str, Any] | None = None,
                       metrics: dict | None = None, origin: str = "bake") -> dict:
        job = self.get_job(job_id)
        if origin not in {"bake", "trellis"}:
            raise ForgeStoreError("Unknown version origin.")
        if job["state"] != "baking":
            raise ForgeConflict("Versions require a baking job.")
        if metrics is None:
            metrics = {}
        if not isinstance(metrics, dict) or not isinstance(metrics.get("part_layers", {}), dict):
            raise ForgeStoreError("Metrics and part_layers must be objects.")
        json.dumps(metrics, allow_nan=False)
        versions = self._index(job["asset"], job["variant"])
        # Retry after a published version/index/job update was interrupted.
        for version in versions:
            if version["job_id"] == job_id:
                return self.get_version(job["asset"], job["variant"], version["number"])
        number = max((v["number"] for v in versions), default=0) + 1
        directory = self._version_dir(job["asset"], job["variant"], number)
        parent = (self.get_version(job["asset"], job["variant"], job["parent_version"])
                  if job["parent_version"] is not None else None)
        version = {
            "number": number, "asset": job["asset"], "variant": job["variant"],
            "origin": origin, "job_id": job_id,
            "lineage": {"parent_version": job["parent_version"],
                        "root_version": parent["lineage"]["root_version"] if parent else number},
            "inputs": {"uploads": deepcopy(job["uploads"]),
                       "replacement_views": job["replacement_views"],
                       "parent_views_inherited": [v for v in job["canonical_views"]
                                                  if parent and v not in job["replacement_views"]]},
            "metrics": {"part_layers": {}, **metrics}, "critic": {"status": "pending"},
            "created_at": self._now().isoformat(), "accepted": False, "notes": [],
        }
        prepared = {}
        for name, value in (artifacts or {}).items():
            target = self.confined(name)  # Validate the relative artifact name itself.
            relative = target.relative_to(self.root)
            if relative in prepared or any(relative in p.parents or p in relative.parents for p in prepared):
                raise ForgeStoreError("Artifact paths must be distinct files.")
            if name.endswith(".json"):
                if isinstance(value, bytes):
                    value = json.loads(value)
                elif isinstance(value, str):
                    value = json.loads(value)
                value = json.dumps(value, allow_nan=False, indent=2).encode("utf-8")
            elif not isinstance(value, bytes):
                value = value.encode("utf-8") if isinstance(value, str) else json.dumps(value, allow_nan=False).encode("utf-8")
            if len(value) > 4 * 1024 * 1024:
                raise ForgeStoreError("Each completion artifact is limited to 4 MiB.")
            prepared[relative] = value
        # Publish an entire version at once; failed writes never consume a number.
        pending = self._checked(directory.with_name(f".pending-{uuid4().hex}"))
        try:
            (pending / "artifacts").mkdir(parents=True)
            (pending / "critic").mkdir()
            for name, value in prepared.items():
                self._write_bytes(pending / "artifacts" / name, value)
            self._write_json(pending / "version.json", version)
            os.replace(pending, directory)
        finally:
            if pending.exists():
                shutil.rmtree(pending)
        self._index(job["asset"], job["variant"])
        return version

    @locked
    def complete(self, job_id: str, *, artifacts: dict | None = None, metrics: dict | None = None,
                 lease_id: str | None = None) -> dict:
        job = self.get_job(job_id)
        if job["state"] == "ready":
            # Completion retries are read-only and never allocate a second version.
            return self.get_version(job["asset"], job["variant"], job["version_number"])
        self._check_lease(job, lease_id)
        version = self.create_version(job_id, artifacts=artifacts, metrics=metrics)
        job.update(state="ready", lease=None, version_number=version["number"], error=None)
        self._save_job(job)
        return version

    @locked
    def get_version(self, asset: str, variant: str, number: int) -> dict:
        return self._read(self._version_dir(asset, variant, number) / "version.json")

    @locked
    def list_versions(self, asset: str | None = None, variant: str | None = None, *,
                      origin: str | None = None, accepted: bool | None = None,
                      q: str | None = None) -> list[dict]:
        versions = []
        for directory in self.root.glob("assets/*/variants/*"):
            a, v = directory.parent.parent.name, directory.name
            if (asset is not None and a != asset) or (variant is not None and v != variant):
                continue
            versions.extend(self._index(a, v))
        return [v for v in versions if (origin is None or v["origin"] == origin)
                and (accepted is None or v["accepted"] == accepted)
                and (not q or q.casefold() in json.dumps(v, ensure_ascii=False).casefold())]

    @locked
    def artifact_file(self, asset: str, variant: str, number: int, name: str) -> Path:
        self.confined(name)
        return self._file(self._version_dir(asset, variant, number) / "artifacts" / name)

    @locked
    def accept_version(self, asset: str, variant: str, number: int, accepted: bool = True) -> dict:
        version = self.get_version(asset, variant, number)
        version["accepted"] = accepted
        self._write_json(self._version_dir(asset, variant, number) / "version.json", version)
        try:
            job = self.get_job(version["job_id"])
        except FileNotFoundError:
            pass
        else:
            job["accepted"] = accepted
            self._save_job(job)
        self._index(asset, variant)
        return version

    @locked
    def append_note(self, *, author: str, text: str, job_id: str | None = None,
                    asset: str | None = None, variant: str | None = None,
                    number: int | None = None) -> dict:
        if not author.strip() or not text.strip():
            raise ForgeStoreError("Notes require an author and nonempty text.")
        note = {"at": self._now().isoformat(), "author": author, "text": text}
        if job_id is not None:
            record = self.get_job(job_id)
            record["notes"].append(note)
            return self._save_job(record)
        record = self.get_version(asset, variant, number)
        record["notes"].append(note)
        self._write_json(self._version_dir(asset, variant, number) / "version.json", record)
        self._index(asset, variant)
        return record

    @locked
    def list_assets(self) -> list[dict]:
        pairs = {(j["asset"], j["variant"]) for j in self.list_jobs()}
        pairs.update((v["asset"], v["variant"]) for v in self.list_versions())
        return [{"asset": a, "variant": v} for a, v in sorted(pairs)]

    @locked
    def status(self) -> dict:
        return {"enabled": True, "store_root": str(self.root),
                "jobs_active": sum(j["state"] not in {"ready", "failed"} for j in self.list_jobs()),
                "versions_total": len(self.list_versions())}
