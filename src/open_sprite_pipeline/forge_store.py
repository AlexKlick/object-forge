"""API-owned Forge state. Run one API writer process per store root.

Worker processes use the HTTP API; they never edit records. The reentrant lock
serializes request threads, including claims and version allocation. JSON files
are replaced atomically. Version directories are published before their derived
index; listing rebuilds that index after an interrupted publication.
"""
from __future__ import annotations

from .forge_policy import Policy, decide_review, decide_bake, decide_version, palette_only

from collections import deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import wraps
import json
from io import BytesIO
from PIL import Image, UnidentifiedImageError
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
    def __init__(self, root: str | Path, policy: Policy | None = None) -> None:
        self.policy = policy if policy is not None else Policy.from_env()
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

    @locked
    def save_catalog(self, payload) -> dict:
        required = {"specs", "prompts", "assets_root", "published_at"}
        if not isinstance(payload, dict) or not required <= payload.keys() or payload.keys() - required - {"errors"}:
            raise ForgeStoreError("Invalid catalog fields.")

        def string(value):
            if not isinstance(value, str) or len(value) > 200:
                raise ForgeStoreError("Catalog strings must be at most 200 characters.")
            return value

        def names(value):
            if not isinstance(value, list):
                raise ForgeStoreError("Catalog names must be lists.")
            return sorted(self._name(string(name)) for name in value)

        if (not isinstance(payload["specs"], list) or len(payload["specs"]) > 500
                or not isinstance(payload["prompts"], list) or len(payload["prompts"]) > 500):
            raise ForgeStoreError("Catalog permits at most 500 specs and 500 prompts.")
        specs = []
        for spec in payload["specs"]:
            if not isinstance(spec, dict) or set(spec) != {"asset", "variants", "views"}:
                raise ForgeStoreError("Invalid catalog spec.")
            specs.append({"asset": self._name(string(spec["asset"])),
                          "variants": names(spec["variants"]), "views": names(spec["views"])})
        published_at = string(payload["published_at"])
        try:
            stamp = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("Missing timezone")
        except ValueError as exc:
            raise ForgeStoreError("Catalog published_at must be an ISO-8601 timestamp with timezone.") from exc
        result = {"specs": sorted(specs, key=lambda spec: (spec["asset"], spec["variants"], spec["views"])),
                  "prompts": names(payload["prompts"]), "assets_root": string(payload["assets_root"]),
                  "published_at": published_at}
        if "errors" in payload:
            if not isinstance(payload["errors"], list):
                raise ForgeStoreError("Catalog errors must be a list.")
            errors = []
            for error in payload["errors"]:
                if not isinstance(error, dict) or set(error) != {"file", "error"}:
                    raise ForgeStoreError("Invalid catalog error.")
                errors.append({key: string(error[key]) for key in ("file", "error")})
            result["errors"] = sorted(errors, key=lambda error: (error["file"], error["error"]))
        self._write_json(self.confined("catalog.json"), result)
        return result

    @locked
    def get_catalog(self) -> dict:
        path = self.confined("catalog.json")
        return self._read(path) if path.exists() else {"specs": [], "prompts": [], "published_at": None}

    @staticmethod
    def validate_image(data: bytes) -> str:
        """The same image validation for standalone uploads and set attachments."""
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
        if media_type not in _IMAGE_EXT:
            raise ForgeStoreError("Unsupported image format.")
        return media_type

    def _set_dir(self, set_id: str) -> Path:
        return self.confined(Path("sets") / self._name(set_id))

    @locked
    def create_set(self, manifest_text: str, board_files: list, pair_files: dict) -> dict:
        from .forge_sets import load_manifest
        manifest = load_manifest(manifest_text)
        required = {p["asset"] + "/" + p["variant"] for p in manifest["requires"]}
        for key in pair_files:
            parts = key.split("/")
            if len(parts) != 2:
                raise ForgeStoreError("Invalid path identifier.")
            for part in parts:
                self._name(part)
            if key not in required:
                raise ForgeStoreError(f"set {manifest['name']}: pair {key} is not required")

        def uploads(files):
            result = {}
            for filename, media_type, data in files:
                if media_type not in _IMAGE_EXT:
                    raise ForgeStoreError("A supported, nonempty image is required.")
                detected = self.validate_image(data)
                name = Path(filename).name
                if name in result:
                    raise ForgeStoreError(f"set {manifest['name']}: duplicate upload {name}")
                result[name] = (name, detected, data)
            return result

        board = uploads(board_files)
        per_pair = {key: uploads(files) for key, files in pair_files.items()}
        prepared = []

        def resolve(names, files, directory):
            records = []
            for index, name in enumerate(names):
                if name not in files:
                    raise ForgeStoreError(f"set {manifest['name']}: missing upload {name}")
                filename, media_type, data = files[name]
                relative = f"{directory}/{index}.{_IMAGE_EXT[media_type]}"
                prepared.append((relative, data))
                records.append({"index": index, "filename": filename, "media_type": media_type,
                                "path": relative})
            return records

        board_records = resolve(manifest["style"]["board"], board, "style")
        pair_records = {}
        for pair in manifest["requires"]:
            key = pair["asset"] + "/" + pair["variant"]
            directory = "pairs/" + pair["asset"] + "__" + pair["variant"]
            files = per_pair.get(key, {})
            pair_records[key] = {
                "style": resolve(pair.get("style_refs", []), files, directory + "/style"),
                "sources": resolve(pair["sources"], files, directory + "/sources"),
            }
        at = self._now().isoformat()
        result = {"id": uuid4().hex, "name": manifest["name"], "manifest": manifest,
                  "created_at": at, "updated_at": at, "pairs": {}, "launches": [],
                  "files": {"style": board_records, "pairs": pair_records}}
        directory = self._set_dir(result["id"])
        try:
            self._write_bytes(directory / "manifest.yaml", manifest_text.encode("utf-8"))
            for relative, data in prepared:
                self._write_bytes(directory / relative, data)
            self._write_json(directory / "set.json", result)
            return self.get_set(result["id"])
        except Exception:
            if directory.exists():
                shutil.rmtree(self._checked(directory))
            raise

    @locked
    def get_set(self, set_id: str) -> dict:
        from .forge_sets import coverage, pair_plan, unbound_kinds, unplaced_assets
        result = self._read(self._set_dir(set_id) / "set.json")
        jobs = self.list_jobs(set_id=set_id)
        pairs = {}
        for pair in result["manifest"]["requires"]:
            asset, variant = self._name(pair["asset"]), self._name(pair["variant"])
            children = [j for j in jobs if (j["asset"], j["variant"]) == (asset, variant)]
            job = children[-1] if children else {}
            versions = self.list_versions(asset, variant)
            version = max(versions, key=lambda v: (v["created_at"], v["number"])) if versions else None
            attention = job.get("attention") or (version or {}).get("attention")
            state = job.get("state", "planned")
            last_skip = next((s for launch in reversed(result["launches"])
                              for s in launch["skipped"] if (s["asset"], s["variant"]) == (asset, variant)
                              and s["reason"] == "no spec in catalog"), {})
            reason = job.get("error") or (attention or {}).get("reason")
            if (not job and last_skip and asset not in
                    {spec["asset"] for spec in self.get_catalog()["specs"]}):
                state, reason = "blocked", last_skip["reason"]
            complete = False
            if version:
                report_path = self._version_dir(asset, variant, version["number"]) / "artifacts/bake_report.json"
                if self._checked(report_path).exists():
                    complete = not self._read(report_path).get("views_missing", [])
                elif "views_missing" in version["metrics"]:
                    complete = not version["metrics"]["views_missing"]
            pairs[asset + "/" + variant] = {
                "job_id": job.get("id"), "intent": job.get("intent", pair_plan(result["manifest"], pair)["intent"]),
                "state": state, "accepted": bool(version and version["accepted"]),
                "version": version["number"] if version else None, "attention": attention,
                "policy_mode": job.get("policy", {}).get("mode", self.policy.mode), "reason": reason,
                "views_complete": complete,
            }
        result["pairs"] = pairs
        result.update(coverage(pairs))
        result["unbound_kinds"] = unbound_kinds(result["manifest"], [k for k, p in pairs.items() if p["version"] is not None])
        result["unplaced_assets"] = unplaced_assets(result["manifest"])
        result["attention"] = [key for key, pair in pairs.items() if pair["attention"]]
        return result

    @locked
    def list_sets(self) -> list[dict]:
        results = []
        for path in self.root.glob("sets/*/set.json"):
            item = self.get_set(path.parent.name)
            results.append({key: item[key] for key in ("id", "name", "created_at")} | {
                "requested_pairs": item["coverage"]["requested_pairs"],
                "coverage": {"percent": item["coverage"]["percent"]}, "attention": len(item["attention"])})
        return sorted(results, key=lambda s: (s["created_at"], s["id"]))

    @locked
    def set_style_file(self, set_id: str, n: int, asset: str | None = None,
                       variant: str | None = None) -> tuple[Path, str]:
        directory = self._set_dir(set_id)
        item = self._read(directory / "set.json")
        refs = item["files"]["style"]
        if asset is not None:
            key = self._name(asset) + "/" + self._name(variant)
            refs = item["files"]["pairs"].get(key, {}).get("style", [])
        ref = next((r for r in refs if r["index"] == n), None)
        if ref is None:
            raise FileNotFoundError("Style reference not found.")
        return self._file(directory / ref["path"]), ref["media_type"]

    @locked
    def launch_set(self, set_id: str, *, force: bool = False) -> dict:
        return self._launch_set(set_id, force=force, retry=False)

    @locked
    def retry_set(self, set_id: str) -> dict:
        return self._launch_set(set_id, force=False, retry=True)

    def _launch_set(self, set_id: str, *, force: bool, retry: bool) -> dict:
        from .forge_sets import pair_plan
        if type(force) is not bool:
            raise ForgeStoreError("force must be boolean.")
        directory = self._set_dir(set_id)
        item = self._read(directory / "set.json")
        jobs = self.list_jobs(set_id=set_id)
        known = {spec["asset"] for spec in self.get_catalog()["specs"]}
        launched, skipped = [], []
        try:
            for pair in item["manifest"]["requires"]:
                asset, variant = self._name(pair["asset"]), self._name(pair["variant"])
                key = asset + "/" + variant
                children = [j for j in jobs if (j["asset"], j["variant"]) == (asset, variant)]
                latest = children[-1] if children else {}
                reason = None
                if any(j["state"] not in {"ready", "failed"} for j in children):
                    reason = "live child job"
                elif retry and not (latest.get("state") == "failed" or latest.get("attention")):
                    reason = "not failed or attention"
                elif not force and self.list_versions(asset, variant, accepted=True):
                    reason = "accepted version"
                plan = pair_plan(item["manifest"], pair)
                if not reason and plan["intent"] == "from_spec" and asset not in known:
                    reason = "no spec in catalog"
                if reason:
                    skipped.append({"asset": asset, "variant": variant, "reason": reason})
                    continue
                generate = {"style": plan["style"]}
                if plan["intent"] == "from_spec":
                    generate.update(spec_asset=asset, palette_only=plan["palette_only"])
                else:
                    generate.update({k: pair[k] for k in ("height_hint", "floor_height") if k in pair})
                job = self.create_job(asset, variant, {"turntable": pair["turntable"]},
                                      intent=plan["intent"], generate=generate, set_id=set_id)
                launched.append({"asset": asset, "variant": variant, "job_id": job["id"]})
                refs = (item["files"]["pairs"][key]["style"] if "style_refs" in pair else item["files"]["style"])
                if plan["style"]:
                    for ref in refs:
                        self.record_style_ref(job["id"], ref["filename"], ref["media_type"],
                                              self._file(directory / ref["path"]).read_bytes())
                if plan["intent"] == "generate":
                    for source in item["files"]["pairs"][key]["sources"]:
                        self.record_upload(job["id"], source["filename"], source["media_type"],
                                           self._file(directory / source["path"]).read_bytes())
            item["updated_at"] = self._now().isoformat()
            item["launches"].append({"at": item["updated_at"], "force": force,
                                     "launched": launched, "skipped": skipped})
            self._write_json(directory / "set.json", item)
        except Exception:
            for child in launched:
                self.delete_job(child["job_id"])
            raise
        return {"launched": launched, "skipped": skipped}

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
        self._write_json(path, {k: v for k, v in job.items() if k != "worker_log"})
        return self.get_job(job["id"])

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
            minimum = 0 if key == "turntable" else 1
            if type(values[key]) is not int or values[key] < minimum:
                raise ForgeStoreError(f"{key} must be an integer >= {minimum}.")
        return values

    def _views(self, values: list) -> list[str]:
        if not isinstance(values, list):
            raise ForgeStoreError("Views must be a list.")
        for view in values:
            self._name(view)
        if len(set(values)) != len(values):
            raise ForgeStoreError("Views must be unique.")
        return list(values)

    @staticmethod
    def _positive(value, name, maximum):
        if type(value) not in (int, float) or not math.isfinite(value) or not 1 <= value <= maximum:
            raise ForgeStoreError(f"{name} must be finite and between 1 and {maximum}.")
        return value

    @staticmethod
    def validate_style(style):
        if style is None:
            return None
        defaults = {"enabled": False, "seeds_per_view": 3, "strength": 0.62,
                    "guidance": 6.5, "control_scale": 0.8, "ip_scale": 0.6,
                    "steps": 28, "long_side": 768, "prompt_override": None,
                    "negative_override": None, "seed_base": 1000}
        if not isinstance(style, dict) or set(style) - set(defaults):
            raise ForgeStoreError("Invalid style settings.")
        result = defaults | style
        if type(result["enabled"]) is not bool:
            raise ForgeStoreError("style.enabled must be boolean.")
        bounds = {"seeds_per_view": (1, 6), "strength": (0.2, 0.95), "guidance": (1, 15),
                  "control_scale": (0, 1.5), "ip_scale": (0, 1.5), "steps": (8, 50),
                  "long_side": (512, 1024), "seed_base": (0, None)}
        for key, (lo, hi) in bounds.items():
            value = result[key]
            integer = key in {"seeds_per_view", "steps", "long_side", "seed_base"}
            if (type(value) not in ((int,) if integer else (int, float))
                    or (type(value) is float and not math.isfinite(value))
                    or value < lo or (hi is not None and value > hi)):
                raise ForgeStoreError(f"Invalid style.{key}.")
        if result["long_side"] % 64:
            raise ForgeStoreError("style.long_side must be a multiple of 64.")
        for key in ("prompt_override", "negative_override"):
            if result[key] is not None and (not isinstance(result[key], str) or len(result[key]) > 400):
                raise ForgeStoreError(f"style.{key} must be at most 400 characters.")
        return result

    def validate_from_spec(self, value, asset):
        value = {} if value is None else value
        if not isinstance(value, dict) or set(value) - {"spec_asset", "palette_only", "style"}:
            raise ForgeStoreError("Invalid from_spec settings.")
        palette_only = value.get("palette_only", False)
        if type(palette_only) is not bool:
            raise ForgeStoreError("palette_only must be boolean.")
        style = self.validate_style(value.get("style"))
        if palette_only and style and style["enabled"]:
            raise ForgeStoreError("palette_only jobs cannot style")
        spec_asset = self._name(value.get("spec_asset", asset))
        known = sorted({spec["asset"] for spec in self.get_catalog()["specs"]})
        if known and spec_asset not in known:
            raise ForgeStoreError(f"Unknown spec asset {spec_asset}; known: {', '.join(known)}")
        return {"spec_asset": spec_asset, "palette_only": palette_only,
                "style": style, "blockout": None, "regenerations": 0, "segment_refs": []}

    def validate_generate(self, value):
        value = {} if value is None else value
        if not isinstance(value, dict) or set(value) - {"segment_refs", "height_hint", "floor_height", "blockout", "regenerations", "style"}:
            raise ForgeStoreError("Invalid generate settings.")
        if value.get("blockout") is not None or type(value.get("regenerations", 0)) is not int or value.get("regenerations", 0) != 0:
            raise ForgeStoreError("New generate jobs cannot supply blockouts or regenerations.")
        refs = value.get("segment_refs", [])
        if not isinstance(refs, list) or len(refs) > 8:
            raise ForgeStoreError("segment_refs must contain zero to eight references.")
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) != {"image_id", "segment_id"}:
                raise ForgeStoreError("Each segment reference requires image_id and segment_id.")
            for identifier in ref.values():
                self._name(identifier)
        return {"segment_refs": deepcopy(refs),
                "height_hint": self._positive(value.get("height_hint", 12.0), "height_hint", 300),
                "floor_height": self._positive(value.get("floor_height", 3.0), "floor_height", 10),
                "blockout": None, "regenerations": 0, "style": self.validate_style(value.get("style"))}

    def validate_edit(self, edit):
        if not isinstance(edit, dict) or not edit or set(edit) - {"height", "floor_height", "plinth_floors", "tower", "palette"}:
            raise ForgeStoreError("Edit requires at least one supported field.")
        for key, maximum in (("height", 300), ("floor_height", 10)):
            if key in edit:
                self._positive(edit[key], key, maximum)
        if "plinth_floors" in edit and (type(edit["plinth_floors"]) is not int or not 1 <= edit["plinth_floors"] <= 40):
            raise ForgeStoreError("plinth_floors must be an integer between 1 and 40.")
        if "tower" in edit:
            tower = edit["tower"]
            if not isinstance(tower, dict) or set(tower) - {"enabled", "width", "location"} or type(tower.get("enabled")) is not bool:
                raise ForgeStoreError("Tower edit requires boolean enabled and optional width/location.")
            if "width" in tower:
                width = tower["width"]
                if type(width) not in (int, float) or not math.isfinite(width) or not 0 < width <= 300:
                    raise ForgeStoreError("Tower width must be positive and at most 300.")
            if "location" in tower and tower["location"] not in ("rear_center", "front_center", "center"):
                raise ForgeStoreError("Invalid tower location.")
            if not tower["enabled"] and set(tower) & {"width", "location"}:
                raise ForgeStoreError("Tower width/location requires an enabled tower.")
        if "palette" in edit:
            palette = edit["palette"]
            if not isinstance(palette, dict) or not palette:
                raise ForgeStoreError("Palette edit requires role/hex entries.")
            for role, color in palette.items():
                self._name(role)
                if not isinstance(color, str) or not re.fullmatch(r"[0-9a-fA-F]{6}", color):
                    raise ForgeStoreError("Palette colors must be six hex digits without #.")
        return deepcopy(edit)

    @locked
    def regenerate_blockout(self, job_id: str, hints: dict) -> dict:
        job = self.get_job(job_id)
        if job["intent"] == "from_spec":
            raise ForgeConflict("Authored specs are not regenerated; edit the blockout instead")
        if job["intent"] != "generate" or job["state"] != "review" or job["match"].get("submitted"):
            raise ForgeConflict("Regeneration requires an unsubmitted generate review.")
        generate = job["generate"]
        if generate["regenerations"] >= 8:
            raise ForgeConflict("At most eight regenerations are allowed.")
        if not isinstance(hints, dict) or set(hints) - {"height_hint", "floor_height", "tower_override", "palette_hex"}:
            raise ForgeStoreError("Invalid regeneration hints.")
        for key, maximum in (("height_hint", 300), ("floor_height", 10)):
            if key in hints:
                self._positive(hints[key], key, maximum)
        tower = hints.get("tower_override", "keep")
        if isinstance(tower, dict):
            if set(tower) - {"width", "location"}:
                raise ForgeStoreError("Invalid tower override.")
            width = tower.get("width", 1)
            if type(width) not in (int, float) or not math.isfinite(width) or width <= 0:
                raise ForgeStoreError("Tower width must be positive and finite.")
            if tower.get("location", "center") not in {"center", "rear_center", "front_center"}:
                raise ForgeStoreError("Invalid tower location.")
        elif tower not in ("none", "keep"):
            raise ForgeStoreError("Invalid tower override.")
        palette = hints.get("palette_hex", {})
        if not isinstance(palette, dict):
            raise ForgeStoreError("palette_hex must map palette roles to hex colors.")
        roles = (generate.get("blockout") or {}).get("palette", {})
        for role, color in palette.items():
            if role not in roles or not isinstance(color, str) or not re.fullmatch(r"#?[0-9a-fA-F]{6}", color):
                raise ForgeStoreError("Invalid palette role or hex color.")
        # Merge individual palette edits so changing height does not discard them.
        palette = {**generate.get("palette_hex", {}), **palette}
        generate.update(deepcopy(hints))
        if palette:
            generate["palette_hex"] = palette
        generate["regenerations"] += 1
        generate["blockout"] = None
        job.pop("style", None)
        job["match"] = {"panels": [], "decisions": [], "views_missing": []}
        job.update(state="matching", lease=None, error=None)
        return self._save_job(job)

    @locked
    def worker_render(self, job_id: str, view: str, payload: bytes, lease_id: str):
        job = self._worker_job(job_id, lease_id, "matching")
        if (not job.get("generate") or view not in job["canonical_views"]):
            raise ForgeConflict("Renders require a canonical generate view.")
        self._write_bytes(self._job_path(job_id).parent / "renders" / f"{self._name(view)}.png", payload)

    @locked
    def worker_blockout(self, job_id: str, payload: dict, renders: dict[str, bytes], lease_id: str):
        job = self._worker_job(job_id, lease_id, "matching")
        if not job.get("generate"):
            raise ForgeConflict("Blockouts require generate intent.")
        required = {"params", "palette", "confidence", "assumptions", "next_view", "synth_report", "views", "spec_yaml"}
        if not isinstance(payload, dict) or set(payload) != required:
            raise ForgeStoreError("Invalid blockout payload fields.")
        data = payload["spec_yaml"]
        if not isinstance(data, bytes) or not data or len(data) > 2 * 1024 * 1024:
            raise ForgeStoreError("Blockout spec must contain at most 2 MiB of bytes.")
        public = {key: value for key, value in payload.items() if key != "spec_yaml"}
        if any(not isinstance(public[key], dict) for key in ("params", "palette", "confidence", "synth_report")):
            raise ForgeStoreError("Invalid blockout objects.")
        params = public["params"]
        if set(params) != {"footprint", "height", "plinth", "tower"}:
            raise ForgeStoreError("Invalid blockout params.")
        for section, keys in ((params["footprint"], ("width", "depth")), (params["plinth"], ("floors", "floor_height"))):
            if section is None and keys == ("floors", "floor_height"):
                continue
            if not isinstance(section, dict) or set(section) != set(keys):
                raise ForgeStoreError("Invalid blockout geometry.")
            for key in keys:
                value = section[key]
                if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                    raise ForgeStoreError("Blockout dimensions must be positive and finite.")
        if type(params["height"]) not in (int, float) or not math.isfinite(params["height"]) or params["height"] <= 0:
            raise ForgeStoreError("Invalid blockout height.")
        tower = params["tower"]
        if tower is not None:
            if not isinstance(tower, dict) or set(tower) != {"width", "floors", "location"}:
                raise ForgeStoreError("Invalid blockout tower.")
            for key in ("width", "floors"):
                if type(tower[key]) not in (int, float) or not math.isfinite(tower[key]) or tower[key] <= 0:
                    raise ForgeStoreError("Invalid blockout tower dimension.")
            if tower["location"] not in {"center", "front_center", "rear_center"}:
                raise ForgeStoreError("Invalid blockout tower location.")
        if not isinstance(public["assumptions"], list) or any(not isinstance(a, str) for a in public["assumptions"]) or not isinstance(public["next_view"], str):
            raise ForgeStoreError("Invalid blockout guidance.")
        for role, color in public["palette"].items():
            self._name(role)
            if not isinstance(color, dict) or "hex" not in color or not isinstance(color["hex"], str) or not re.fullmatch(r"#?[0-9a-fA-F]{6}", color["hex"]):
                raise ForgeStoreError("Invalid blockout palette.")
        views = self._views(public["views"])
        if not views or views != job["canonical_views"] or set(renders) - set(views):
            raise ForgeStoreError("Blockout views must match canonical views.")
        json.dumps(public, allow_nan=False)
        for view in views:
            if view in renders:
                if not isinstance(renders[view], bytes):
                    raise ForgeStoreError("Render must be bytes.")
                self.confined(self._job_path(job_id).parent.relative_to(self.root) / "renders" / f"{view}.png")
            else:
                self.blockout_file(job_id, f"renders/{view}.png")
        for view, image in renders.items():
            self.worker_render(job_id, view, image, lease_id)
        self._write_bytes(self._job_path(job_id).parent / "blockout/spec.yaml", data)
        job["generate"]["blockout"] = deepcopy(public)
        return self._save_job(job)

    @locked
    def blockout_file(self, job_id: str, name: str) -> Path:
        job = self.get_job(job_id)
        if not job.get("generate"):
            raise FileNotFoundError("No blockout for this job.")
        if name == "spec.yaml":
            relative = "blockout/spec.yaml"
        elif name.startswith("renders/") and name.endswith(".png"):
            view = self._name(name[len("renders/"):-4])
            if view not in job["canonical_views"]:
                raise FileNotFoundError("Unknown render.")
            relative = name
        else:
            raise ForgeStoreError("Invalid blockout artifact.")
        return self._file(self._job_path(job_id).parent / relative)

    @locked
    def create_job(self, asset: str, variant: str, params: dict | None = None, *,
                   canonical_views: list[str] | None = None, intent: str = "fresh",
                   parent_job: str | None = None, parent_version: int | None = None,
                   replacement_views: list[str] | None = None, generate: dict | None = None,
                   set_id: str | None = None) -> dict:
        self._variant_dir(asset, variant)
        if set_id is not None:
            self._read(self._set_dir(set_id) / "set.json")
        values = self.validate_params(params)
        views = self._views([] if canonical_views is None else canonical_views)
        replacements = self._views([] if replacement_views is None else replacement_views)
        if intent not in {"fresh", "iterate_views", "iterate_params", "generate", "from_spec", "iterate_blockout"}:
            raise ForgeStoreError("Invalid intent.")
        if intent in {"fresh", "generate", "from_spec"}:
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
        inherited = []
        if intent in {"iterate_views", "iterate_params"}:
            if views != parent["canonical_views"]:
                raise ForgeStoreError("Iteration must preserve parent canonical views.")
            inherited = version["inputs"]["staged_views"]
            for view in inherited:
                self.staged_view_file(parent_job, view)
        if intent == "iterate_blockout":
            if not parent.get("generate"):
                raise ForgeStoreError("Blockout iteration requires a generate-family parent.")
            if "blockout/spec.yaml" not in version["artifacts"]:
                raise ForgeStoreError("Parent version requires blockout/spec.yaml.")
            self.artifact_file(asset, variant, parent_version, "blockout/spec.yaml")
            if replacements:
                raise ForgeStoreError("Blockout iteration rematches all sources; replacement views are invalid.")
        if set(replacements) - set(views):
            raise ForgeStoreError("Replacement views must be canonical views.")
        job = {
            "id": uuid4().hex, "asset": asset, "variant": variant,
            "parent_job": parent_job, "parent_version": parent_version, "intent": intent,
            "state": "uploaded", "params": values, "canonical_views": views,
            "replacement_views": replacements, "uploads": [], "style_refs": [],
            "inputs": {"parent_views_inherited": list(inherited)},
            "match": {"panels": [], "decisions": [], "views_missing": []},
            "created_at": self._now().isoformat(), "updated_at": self._now().isoformat(),
            "error": None, "accepted": False, "notes": [], "lease": None,
        }
        if set_id is not None:
            job["set_id"] = set_id
        if intent == "generate":
            job["generate"] = self.validate_generate(generate)
        elif intent == "from_spec":
            job["generate"] = self.validate_from_spec(generate, asset)
        elif intent == "iterate_blockout":
            if not isinstance(generate, dict) or set(generate) != {"edit"}:
                raise ForgeStoreError("Blockout iteration requires generate.edit only.")
            job["generate"] = self.validate_generate({"segment_refs": parent.get("generate", {}).get("segment_refs", [])})
            job["generate"]["edit"] = self.validate_edit(generate["edit"])
            # Keep references through successive edits without copying source bytes.
            job["inputs"]["parent_uploads"] = ([u["index"] for u in parent["uploads"]]
                                                or list(parent["inputs"].get("parent_uploads", [])))
        elif generate is not None:
            raise ForgeStoreError("Generate settings require generate intent.")
        elif intent in {"iterate_params", "iterate_views"} and parent.get("generate"):
            # A generated version's spec belongs to the selected version, never
            # to the companion tree. Descendants keep workspace execution.
            self.artifact_file(asset, variant, parent_version, "blockout/spec.yaml")
            job["generate"] = deepcopy(parent["generate"])
            job["generate"].update(style=None, segment_refs=[])
            job["inputs"]["workspace_parent"] = True
        return self._save_job(job)

    @locked
    def get_job(self, job_id: str) -> dict:
        return self._read_job(self._job_path(job_id))

    def _read_job(self, path: Path) -> dict:
        job = self._read(path)
        log = self._checked(path.parent / "worker.log")
        job["worker_log"] = log.read_text().splitlines() if log.exists() else []
        return job

    @locked
    def list_jobs(self, *, state: str | None = None, asset: str | None = None,
                  set_id: str | None = None) -> list[dict]:
        if set_id is not None:
            self._name(set_id)
        jobs = [self._read_job(p) for p in self.root.glob("assets/*/variants/*/jobs/*/job.json")]
        return sorted((j for j in jobs if (state is None or j["state"] == state)
                       and (asset is None or j["asset"] == asset)
                       and (set_id is None or j.get("set_id") == set_id)), key=lambda j: (j["created_at"], j["id"]))

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
        if job["intent"] == "iterate_blockout":
            raise ForgeStoreError("Blockout edits use inherited sources.")
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
    def record_style_ref(self, job_id: str, filename: str, media_type: str, data: bytes) -> dict:
        job = self.get_job(job_id)
        refs = job.setdefault("style_refs", [])
        if job["state"] != "uploaded":
            raise ForgeConflict("Style references require an uploaded job.")
        if len(refs) >= 6:
            raise ForgeStoreError("At most six style references are allowed.")
        if media_type not in _IMAGE_EXT or not data:
            raise ForgeStoreError("A supported, nonempty image is required.")
        index = len(refs)
        self._write_bytes(self._job_path(job_id).parent / "style/refs" / f"{index}.{_IMAGE_EXT[media_type]}", data)
        record = {"index": index, "filename": Path(filename).name[:240], "media_type": media_type}
        refs.append(record)
        self._save_job(job)
        return record

    @locked
    def style_ref_file(self, job_id: str, n: int) -> tuple[Path, str]:
        job = self.get_job(job_id)
        ref = next((r for r in job.get("style_refs", []) if r["index"] == n), None)
        if ref is None:
            raise FileNotFoundError("Style reference not found.")
        return (self._file(self._job_path(job_id).parent / "style/refs" / f"{n}.{_IMAGE_EXT[ref['media_type']]}"),
                ref["media_type"])

    def _style_path(self, job_id: str, view: str, seed: int) -> Path:
        job = self.get_job(job_id)
        self._name(view)
        if job["canonical_views"] and view not in job["canonical_views"]:
            raise ForgeStoreError("Unknown style view.")
        if type(seed) is not int or seed < 0:
            raise ForgeStoreError("Style seed must be a non-negative integer.")
        return self._checked(self._job_path(job_id).parent / "style" / view / f"{seed}.png")

    @locked
    def worker_style_candidate(self, job_id: str, view: str, seed: int, png_bytes: bytes, lease_id: str):
        self._worker_job(job_id, lease_id, "matching")
        self._write_bytes(self._style_path(job_id, view, seed), png_bytes)

    @locked
    def style_candidate_file(self, job_id: str, view: str, seed: int) -> Path:
        return self._file(self._style_path(job_id, view, seed))

    @locked
    def worker_style_report(self, job_id: str, report: dict, lease_id: str):
        job = self._worker_job(job_id, lease_id, "matching")
        if not isinstance(report, dict) or set(report) != {"views", "prompt_tokens", "model", "refs", "params"}:
            raise ForgeStoreError("Invalid style report fields.")
        if not isinstance(report["views"], dict) or set(report["views"]) != set(job["canonical_views"]):
            raise ForgeStoreError("Style report must cover canonical views.")
        if (type(report["refs"]) is not int or report["refs"] != len(job.get("style_refs", []))
                or not isinstance(report["model"], str) or not isinstance(report["prompt_tokens"], dict)):
            raise ForgeStoreError("Invalid style report metadata.")
        if not isinstance(report["params"], dict):
            raise ForgeStoreError("Style report params must be an object.")
        self.validate_style(report["params"])
        for view, entry in report["views"].items():
            if not isinstance(entry, dict) or set(entry) != {"seeds", "chosen", "metrics"}:
                raise ForgeStoreError("Invalid style view report.")
            seeds = entry["seeds"]
            if (not isinstance(seeds, list) or not 1 <= len(seeds) <= 6
                    or any(type(seed) is not int or seed < 0 for seed in seeds)
                    or len(set(seeds)) != len(seeds) or type(entry["chosen"]) is not int
                    or entry["chosen"] not in seeds or not isinstance(entry["metrics"], dict)
                    or set(entry["metrics"]) != {str(seed) for seed in seeds}):
                raise ForgeStoreError("Invalid style seeds or metrics.")
            for seed in seeds:
                self.style_candidate_file(job_id, view, seed)
        json.dumps(report, allow_nan=False)
        self._write_json(self._job_path(job_id).parent / "style/report.json", report)
        job["style"] = deepcopy(report)
        return self._save_job(job)

    @locked
    def get_style_report(self, job_id: str) -> dict:
        if not self.get_job(job_id).get("style"):
            raise FileNotFoundError("No style report for this job.")
        return self._read(self._file(self._job_path(job_id).parent / "style/report.json"))

    @locked
    def upload_file(self, job_id: str, index: int) -> tuple[Path, str]:
        job = self.get_job(job_id)
        upload = next((u for u in job["uploads"] if u["index"] == index), None)
        if upload is None:
            if job["intent"] == "iterate_blockout" and index in job["inputs"]["parent_uploads"]:
                return self.upload_file(job["parent_job"], index)
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
    def review(self, job_id: str, mode: str, panels: list[dict], views_missing: list[str],
               *, actor: str = "human") -> dict:
        job = self.get_job(job_id)
        if job["state"] != "review":
            raise ForgeConflict("Review decisions require a review job.")
        if mode not in {"draft", "submit"}:
            raise ForgeStoreError("Invalid review mode.")
        if job["match"].get("submitted"):
            raise ForgeConflict("Submitted review decisions are immutable.")
        canonical = set(job["canonical_views"])
        missing = set(self._views(views_missing))
        inherited = set(job.get("inputs", {}).get("parent_views_inherited", []))
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
        if missing - canonical or missing & (covered | inherited):
            raise ForgeStoreError("Missing views must be uncovered canonical views.")
        if mode == "submit" and (not canonical or canonical - covered - inherited - missing):
            raise ForgeStoreError("Every canonical view must be covered or explicitly missing.")
        if job["match"].get("report_saved"):
            known = {p["panel_id"] for p in job["match"]["panels"]}
            if ids - known or (mode == "submit" and ids != known):
                raise ForgeStoreError("Every panel requires exactly one decision on submit.")
        job["match"]["decisions"] = deepcopy(panels)
        job["match"]["views_missing"] = sorted(missing)
        if mode == "submit":
            job["match"]["submitted_by"] = actor
            if actor == "human":
                job.pop("attention", None)
                audit = job.setdefault("policy", {"mode": self.policy.mode})
                previous = audit.get("review")
                audit["review"] = {"action": "submit", "actor": actor, "at": self._now().isoformat(),
                                   "panels": deepcopy(panels), "views_missing": sorted(missing),
                                   "thresholds": self.policy.thresholds, "applied": True}
                if previous:
                    audit["review"]["recommendation"] = previous
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
        if job["parent_job"] and job["intent"] != "iterate_blockout" and views != job["canonical_views"]:
            raise ForgeStoreError("Iteration must preserve parent canonical views.")
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
        job = self.get_job(job_id)
        receipt = job.get("policy_receipts", {}).get("review")
        if receipt is not None and receipt == lease_id:
            saved = self._read(self._job_path(job_id).parent / "match/match_report.json")
            if report != saved:
                raise ForgeConflict("Match retry changed the evidence.")
            return job
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
        job = self.set_state(job_id, "review")
        if self.policy.mode != "off":
            decision = decide_review(self.policy, job, report)
            applied = self.policy.mode == "enforce" and decision["action"] == "submit"
            if applied:
                job = self.review(job_id, "submit", decision["panels"], decision["views_missing"], actor="policy")
            job.setdefault("policy", {}).update(mode=self.policy.mode, review={
                **decision, "actor": "policy", "at": self._now().isoformat(), "applied": applied})
            if decision["action"] == "escalate":
                job["attention"] = {"reason": "review", "detail": decision["reasons"], "at": self._now().isoformat()}
            job.setdefault("policy_receipts", {})["review"] = lease_id
            return self._save_job(job)
        return job

    @locked
    def worker_staged_view(self, job_id: str, view: str, payload: bytes, lease_id: str) -> None:
        job = self._worker_job(job_id, lease_id, "review")
        accepted = {p["view"] for p in job["match"]["decisions"] if p["decision"] != "reject"}
        accepted.update(job.get("inputs", {}).get("parent_views_inherited", []))
        if not job["match"].get("submitted") or view not in accepted:
            raise ForgeConflict("Staged images require submitted decisions for the view.")
        self.save_staged_view(job_id, view, payload)

    def staged_view_file(self, job_id: str, view: str) -> Path:
        return self._file(self._job_path(job_id).parent / "staged" / "views" / f"{self._name(view)}.png")

    @locked
    def finish_staging(self, job_id: str, lease_id: str) -> dict:
        job = self.get_job(job_id)
        if lease_id is not None and job.get("policy_receipts", {}).get("bake") == lease_id:
            return job
        job = self._worker_job(job_id, lease_id, "review")
        if not job["match"].get("submitted"):
            raise ForgeConflict("Review has not been submitted.")
        expected = {p["view"] for p in job["match"]["decisions"] if p["decision"] != "reject"}
        inherited = set(job.get("inputs", {}).get("parent_views_inherited", [])) - expected
        expected.update(inherited)
        directory = self._checked(self._job_path(job_id).parent / "staged" / "views")
        actual = {p.stem for p in directory.glob("*.png")}
        if actual != expected:
            raise ForgeConflict("Staged images must exactly cover submitted decisions.")
        for view in expected:
            self._file(directory / f"{view}.png")
        job.setdefault("inputs", {})["parent_views_inherited"] = sorted(inherited)
        self._save_job(job)
        job = self.set_state(job_id, "staged")
        if self.policy.mode != "off":
            action = decide_bake(self.policy, job)
            applied = self.policy.mode == "enforce" and action == "approve"
            if applied:
                job = self.set_state(job_id, "queued_bake")
            job.setdefault("policy", {}).update(mode=self.policy.mode, bake={
                "action": action, "actor": "policy", "at": self._now().isoformat(),
                "thresholds": self.policy.thresholds, "applied": applied})
            if action == "escalate":
                job["attention"] = {"reason": "bake", "detail": ["Automatic baking is disabled."],
                                    "at": self._now().isoformat()}
            job.setdefault("policy_receipts", {})["bake"] = lease_id
            return self._save_job(job)
        return job

    @locked
    def approve(self, job_id: str) -> dict:
        job = self.set_state(job_id, "queued_bake")
        job.pop("attention", None)
        return self._save_job(job)

    @locked
    def attention(self) -> dict:
        jobs = [j for j in self.list_jobs() if j.get("attention")]
        jobs.sort(key=lambda j: (j["attention"]["at"], j["created_at"], j["id"]))
        versions = [v for v in self.list_versions() if v.get("attention")]
        versions.sort(key=lambda v: (v["attention"]["at"], v["asset"], v["variant"], v["number"]))
        return {"jobs": [{k: v for k, v in j.items() if k not in {"worker_log", "policy_receipts"}}
                         for j in jobs], "versions": versions}

    @locked
    def policy_override(self, job_id: str, action: str, author: str) -> dict:
        if action not in {"submit", "approve", "accept", "dismiss"} or not isinstance(author, str) or not author.strip():
            raise ForgeStoreError("Override requires a supported action and author.")
        job = self.get_job(job_id)
        decision = None
        if action == "submit":
            decision = decide_review(self.policy, job, job["match"])
            if decision["views_missing"] and not palette_only(job):
                raise ForgeConflict("Policy override would leave canonical views uncovered.")
            job = self.review(job_id, "submit", decision["panels"], decision["views_missing"])
        elif action == "approve":
            job = self.approve(job_id)
        elif action == "accept":
            if job.get("version_number") is None:
                raise ForgeConflict("Job has no published version.")
            self.accept_version(job["asset"], job["variant"], job["version_number"], True)
            job = self.get_job(job_id)
        job.pop("attention", None)
        if job.get("version_number") is not None:
            version = self.get_version(job["asset"], job["variant"], job["version_number"])
            version.pop("attention", None)
            self._write_json(self._version_dir(job["asset"], job["variant"], job["version_number"]) / "version.json", version)
            self._index(job["asset"], job["variant"])
        record = {"action": action, "author": author, "actor": "human", "at": self._now().isoformat(),
                  "thresholds": self.policy.thresholds}
        if decision is not None:
            record["decision"] = decision
        job.setdefault("policy", {"mode": self.policy.mode}).setdefault("overrides", []).append(record)
        return self._save_job(job)

    @locked
    def append_worker_log(self, job_id: str, lines: list[str]) -> None:
        path = self._checked(self._job_path(job_id).parent / "worker.log")
        retained = deque(path.read_text(encoding="utf-8").splitlines() if path.exists() else [], maxlen=200)
        for line in lines:
            retained.extend(line.splitlines())
        self._write_bytes(path, ("\n".join(retained) + ("\n" if retained else "")).encode("utf-8"))

    @locked
    def claim_job(self, kind: str = "pipeline", stages: list[str] | None = None,
                  job_id: str | None = None) -> dict | None:
        if kind not in {"pipeline", "critic"}:
            raise ForgeStoreError("Unknown worker kind.")
        if kind == "critic":
            return self.claim_critic()
        now = self._now()
        jobs = self.list_jobs()
        for job in jobs:
            if job_id is not None and job["id"] != job_id:
                continue
            target = {"uploaded": "matching", "queued_bake": "baking"}.get(job["state"], job["state"])
            if stages is not None and target not in stages:
                continue
            lease = job["lease"]
            if lease and datetime.fromisoformat(lease["lease_expires_at"]) > now:
                continue
            if target == "baking" and any(
                    other["id"] != job["id"] and other["state"] == "baking"
                    and (other["asset"], other["variant"]) == (job["asset"], job["variant"])
                    and other["lease"]
                    and datetime.fromisoformat(other["lease"]["lease_expires_at"]) > now
                    for other in jobs):
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
                                              "created_at", "accepted", "metrics", "notes", "lineage")} | {"state": "ready", "artifacts": version.get("artifacts", []), "critic": version.get("critic", {"status": "pending"}), "style": version.get("style", version.get("metrics", {}).get("style")),
            "attention": version.get("attention"),
            "policy": {key: version.get("policy", {})[key] for key in ("mode", "action")
                       if key in version.get("policy", {})}} | (
                           {"set_id": version["set_id"]} if version.get("set_id") is not None else {})

    @locked
    def claim_critic(self) -> dict | None:
        now = self._now()
        for summary in sorted(self.list_versions(), key=lambda v: (v["created_at"], v["number"]), reverse=True):
            if summary["critic"]["status"] != "pending":
                continue
            version = self.get_version(summary["asset"], summary["variant"], summary["number"])
            # Version publication precedes job completion. Critic cannot claim in that gap.
            if not version.get("job_id"):
                continue
            try:
                job = self.get_job(version["job_id"])
            except FileNotFoundError:
                continue
            if job["state"] != "ready":
                continue
            lease = version.get("critic_lease")
            if lease and datetime.fromisoformat(lease["lease_expires_at"]) > now:
                continue
            version["critic_lease"] = {"kind": "critic", "lease_id": uuid4().hex,
                                       "lease_expires_at": (now + timedelta(seconds=300)).isoformat()}
            self._write_json(self._version_dir(version["asset"], version["variant"], version["number"]) / "version.json", version)
            return version
        return None

    @locked
    def get_critic(self, asset: str, variant: str, number: int) -> dict:
        version = self.get_version(asset, variant, number)
        critic = version.get("critic", {"status": "pending"})
        if critic["status"] == "pending":
            return critic
        return self._read(self._version_dir(asset, variant, number) / "critic" / "critic.json")

    @locked
    def rerun_critic(self, asset: str, variant: str, number: int) -> dict:
        version = self.get_version(asset, variant, number)
        if version["origin"] == "trellis" and not version.get("job_id"):
            return self.get_critic(asset, variant, number)
        version.update(critic={"status": "pending"}, critic_lease=None)
        self._write_json(self._version_dir(asset, variant, number) / "version.json", version)
        self._index(asset, variant)
        return version["critic"]

    @locked
    def complete_critic(self, asset: str, variant: str, number: int, lease_id: str, verdict: dict) -> dict:
        from .forge_critic import validate_verdict

        version = self.get_version(asset, variant, number)
        if lease_id is not None and version.get("policy_critic_receipt") == lease_id and version["critic"]["status"] != "pending":
            saved = self.get_critic(asset, variant, number)
            if {k: v for k, v in saved.items() if k != "at"} != {k: v for k, v in verdict.items() if k != "at"}:
                raise ForgeConflict("Critic retry changed the evidence.")
            return saved
        lease = version.get("critic_lease")
        if (not lease or lease["lease_id"] != lease_id
                or datetime.fromisoformat(lease["lease_expires_at"]) <= self._now()
                or version["critic"]["status"] != "pending"):
            raise ForgeConflict("Missing, stale or expired critic lease.")
        status = verdict.get("status")
        if status not in {"pass", "warn", "fail", "error", "skipped"}:
            raise ForgeStoreError("Invalid critic status.")
        if status in {"pass", "warn", "fail"}:
            validate_verdict({key: verdict.get(key) for key in ("overall", "score", "issues", "summary")})
            if verdict["overall"] != status:
                raise ForgeStoreError("Critic status must match overall.")
        elif verdict.get("score") is not None or verdict.get("issues") != []:
            raise ForgeStoreError("Unavailable critic must have no score or issues.")
        if any(not isinstance(verdict.get(key), str) for key in ("summary", "model")):
            raise ForgeStoreError("Critic requires summary and model.")
        if "excerpt" in verdict and (not isinstance(verdict["excerpt"], str) or len(verdict["excerpt"]) > 400):
            raise ForgeStoreError("Critic excerpt must be at most 400 characters.")
        verdict = {**verdict, "at": self._now().isoformat()}
        version["critic"] = {key: verdict[key] for key in ("status", "score", "summary", "model", "at")}
        version["critic"]["issues"] = len(verdict["issues"])
        version["critic_lease"] = None
        directory = self._version_dir(asset, variant, number)
        self._write_json(directory / "critic" / "critic.json", verdict)
        self._write_json(directory / "version.json", version)
        if self.policy.mode != "off":
            decision = decide_version(self.policy, version)
            applied = self.policy.mode == "enforce" and decision["action"] == "accept"
            if applied:
                version = self.accept_version(asset, variant, number, True)
            record = {**decision, "mode": self.policy.mode, "actor": "policy", "at": verdict["at"],
                      "applied": applied}
            version["policy"] = record
            version["policy_critic_receipt"] = lease_id
            if decision["action"] == "flag":
                version["attention"] = {"reason": "critic", "detail": decision["reasons"], "at": verdict["at"]}
            else:
                version.pop("attention", None)
            self._write_json(directory / "version.json", version)
            if version.get("job_id"):
                try:
                    job = self.get_job(version["job_id"])
                except FileNotFoundError:
                    pass
                else:
                    audit = job.setdefault("policy", {})
                    if "version" in audit:
                        audit.setdefault("version_history", []).append(audit["version"])
                    audit.update(mode=self.policy.mode, version=deepcopy(record))
                    if decision["action"] == "flag":
                        job["attention"] = deepcopy(version["attention"])
                    elif job.get("attention", {}).get("reason") == "critic":
                        job.pop("attention", None)
                    self._save_job(job)
        self._index(asset, variant)
        return verdict

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
                       "staged_views": sorted(p.stem for p in self._checked(
                           self._job_path(job_id).parent / "staged" / "views").glob("*.png")),
                       "replacement_views": job["replacement_views"],
                       "parent_views_inherited": (job.get("inputs", {}).get("parent_views_inherited", [])
                                                  if job["match"].get("report_saved") else
                                                  [v for v in job["canonical_views"]
                                                   if parent and v not in job["replacement_views"]])},
            "metrics": {"part_layers": {}, **metrics}, "critic": {"status": "pending"},
            "created_at": self._now().isoformat(), "accepted": False, "notes": [],
        }
        if job.get("set_id") is not None:
            version["set_id"] = job["set_id"]
        prepared = {}
        for name, value in (artifacts or {}).items():
            target = self.confined(name)  # Validate the relative artifact name itself.
            relative = target.relative_to(self.root)
            if relative in prepared or any(relative in p.parents or p in relative.parents for p in prepared):
                raise ForgeStoreError("Artifact paths must be distinct files.")
            if name.endswith(".json"):
                if isinstance(value, bytes):
                    # Validate JSON without changing harvested source bytes.
                    json.dumps(json.loads(value), allow_nan=False)
                elif isinstance(value, str):
                    value = json.loads(value)
                if not isinstance(value, bytes):
                    value = json.dumps(value, allow_nan=False, indent=2).encode("utf-8")
            elif not isinstance(value, bytes):
                value = value.encode("utf-8") if isinstance(value, str) else json.dumps(value, allow_nan=False).encode("utf-8")
            if len(value) > 32 * 1024 * 1024:
                raise ForgeStoreError("Each completion artifact is limited to 32 MiB.")
            prepared[relative] = value
        version["artifacts"] = sorted(p.as_posix() for p in prepared)
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
        return self.get_version(job["asset"], job["variant"], number)

    @staticmethod
    def _import_source(source: Path, source_roots: list[Path], max_bytes: int) -> Path:
        """Gen Ladder GL1: validate the original path before resolving aliases."""
        source = Path(source)
        if not source.is_absolute() or ".." in source.parts:
            raise ForgeStoreError("Import sources must be absolute, confined paths.")
        if any(p.is_symlink() for p in (source, *source.parents)):
            raise ForgeStoreError("Symlinks are not allowed in import sources.")
        resolved = source.resolve()
        if not any(resolved.is_relative_to(Path(root).resolve()) for root in source_roots):
            raise ForgeStoreError("Import source escapes the allowed source roots.")
        if not resolved.is_file():
            raise ForgeStoreError("Import source must exist and be a regular file.")
        if resolved.stat().st_size > max_bytes:
            raise ForgeStoreError(f"Import artifact exceeds {max_bytes} bytes.")
        return resolved

    @locked
    def import_version(self, asset: str, variant: str, *, origin: str,
                       artifacts: list[tuple[str, Path]], source_roots: list[Path],
                       metrics: dict, inputs: dict, dedupe_key: str,
                       max_bytes_per_artifact: int = 256 * 1024 * 1024) -> dict:
        """Gen Ladder GL1: publish a jobless import; keys dedupe across the library."""
        self._variant_dir(asset, variant)
        if origin != "trellis" or not isinstance(dedupe_key, str) or not dedupe_key:
            raise ForgeStoreError("Imports require trellis origin and a nonempty dedupe key.")
        for summary in self.list_versions():
            if summary["metrics"].get("import", {}).get("dedupe_key") == dedupe_key:
                return summary
        if not isinstance(metrics, dict) or not isinstance(inputs, dict):
            raise ForgeStoreError("Import metrics and inputs must be objects.")
        if type(max_bytes_per_artifact) is not int or max_bytes_per_artifact < 1:
            raise ForgeStoreError("Import size limit must be a positive integer.")
        metrics = {**deepcopy(metrics), "part_layers": {}}
        if not isinstance(metrics.get("import", {}), dict):
            raise ForgeStoreError("Import metrics must be an object.")
        metrics["import"] = {**metrics.get("import", {}), "dedupe_key": dedupe_key}
        json.dumps([metrics, inputs], allow_nan=False)
        prepared = {}
        for name, source in artifacts:
            relative = self.confined(name).relative_to(self.root)
            if relative in prepared or any(relative in p.parents or p in relative.parents for p in prepared):
                raise ForgeStoreError("Artifact paths must be distinct files.")
            prepared[relative] = self._import_source(source, source_roots, max_bytes_per_artifact)
        if not prepared:
            raise ForgeStoreError("Imports require artifacts.")
        versions = self._index(asset, variant)
        number = max((v["number"] for v in versions), default=0) + 1
        directory = self._version_dir(asset, variant, number)
        at = self._now().isoformat()
        version = {
            "number": number, "asset": asset, "variant": variant, "origin": origin,
            "job_id": None, "created_at": at, "accepted": False, "notes": [],
            "lineage": {"parent_version": None, "root_version": number},
            "inputs": deepcopy(inputs), "metrics": metrics, "critic": {"status": "skipped"},
            "artifacts": sorted(p.as_posix() for p in prepared),
        }
        verdict = {"status": "skipped", "score": None,
                   "summary": "Imported TRELLIS generation; critic lane does not review imports.",
                   "model": "none", "issues": [], "at": at}
        pending = self._checked(directory.with_name(f".pending-{uuid4().hex}"))
        try:
            (pending / "artifacts").mkdir(parents=True)
            for name, source in prepared.items():
                source = self._import_source(source, source_roots, max_bytes_per_artifact)
                target = self._checked(pending / "artifacts" / name)
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(source, target)
                except OSError:
                    shutil.copy2(source, target)
                if target.stat().st_size > max_bytes_per_artifact:
                    raise ForgeStoreError("Import artifact grew beyond the size limit.")
            self._write_json(pending / "critic" / "critic.json", verdict)
            self._write_json(pending / "version.json", version)
            # Rename only our pending directory; Capture sources never move.
            os.replace(pending, directory)
        finally:
            if pending.exists():
                shutil.rmtree(pending)
        self._index(asset, variant)
        return self._summary(version)

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
        directory = self._version_dir(asset, variant, number)
        version = self._read(directory / "version.json")
        report_path = self._checked(directory / "artifacts" / "bake_report.json")
        report = self._read(report_path) if report_path.exists() else {}
        version["part_layer_map"] = {part["id"]: part["layer"] for part in report.get("parts", [])
                                     if "id" in part and "layer" in part}
        version.setdefault("artifacts", sorted(p.relative_to(directory / "artifacts").as_posix()
                                               for p in (directory / "artifacts").rglob("*") if p.is_file()))
        return version

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
        version.pop("attention", None)
        self._write_json(self._version_dir(asset, variant, number) / "version.json", version)
        try:
            if not version.get("job_id"):
                raise FileNotFoundError("Imported version has no job.")
            job = self.get_job(version["job_id"])
        except FileNotFoundError:
            pass
        else:
            job["accepted"] = accepted
            job.pop("attention", None)
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
