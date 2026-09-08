"""Host-only MATCH and bake worker. Job/version state uses the Forge HTTP API.

The spike's pure image helpers are imported with bytecode writes disabled;
never call its run/main functions, which write beneath the spike assets tree.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from collections import Counter
from datetime import datetime, timezone
from functools import partial
import base64
import ctypes
import fcntl
import hashlib
from io import BytesIO
import importlib
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
from threading import Event, Thread
import time
from uuid import uuid4
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import yaml

from . import forge_glb
from .style_client import StyleClient, StyleBusy, StyleError

DEFAULT_SPIKE_ASSETS = Path("/home/alexk/debt-city-greybox-spike/apps/greybox/assets")
BAKE_MARKERS = re.compile(r"\b(?:PROJECT|SELFCHECK|VIEW-VALIDATE|TURNTABLE|BAKE|VERIFY)\b")


def checked_path(root: Path, relative: str | Path, *, allow_hardlinks: bool = False) -> Path:
    """Reject aliases before any transient write or artifact read."""
    path = root / relative
    if not path.is_relative_to(root) or ".." in path.parts:
        raise ValueError("Path escapes worker root.")
    for component in (path, *path.parents):
        if component == root:
            break
        if component.is_symlink():
            raise ValueError(f"Symlink forbidden: {component}")
    if not allow_hardlinks and path.is_file() and path.stat().st_nlink != 1:
        raise ValueError(f"Hardlink forbidden: {path}")
    return path


def publish_library_root(pending: Path, destination: Path):
    """Publish a whole directory on the Linux host, including nonempty re-exports.

    rename/replace cannot overwrite a nonempty directory. Linux's atomic exchange
    keeps the previous complete root readable until the new complete root is in
    place; the old tree then occupies pending and can be removed safely.
    """
    if not destination.exists():
        os.replace(pending, destination)
        return
    renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(pending), -100, os.fsencode(destination), 2) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))
    shutil.rmtree(pending)


def fingerprint(path: Path):
    try:
        stat = path.stat()
        return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    except FileNotFoundError:
        return None


def truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Forge API redirects are not allowed.")


class ForgeClient:
    def __init__(self, url: str, token: str = ""):
        parts = urlsplit(url)
        try:
            local = ipaddress.ip_address(parts.hostname or "").is_loopback
        except ValueError:
            local = False
        if (parts.scheme != "http" or not local or parts.username or parts.password
                or parts.query or parts.fragment or parts.path not in {"", "/"}):
            raise ValueError("FORGE_API must be an HTTP loopback IP origin.")
        self.url = url.rstrip("/") + "/v1/forge"
        self.token = token
        # Do not route local jobs/tokens through environment proxies or redirects.
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method: str, path: str, body=None, *, lease: str | None = None):
        binary = isinstance(body, bytes)
        headers = {"X-Forge-Worker": self.token}
        if lease is not None:
            headers["X-Forge-Lease"] = lease
        if body is not None:
            headers["Content-Type"] = "image/png" if binary else "application/json"
        data = body if binary or body is None else json.dumps(body, allow_nan=False).encode()
        request = Request(self.url + path, data=data, headers=headers, method=method)
        with self.opener.open(request, timeout=60) as response:
            payload = response.read()
            if response.status == 204:
                return None
            return json.loads(payload) if "application/json" in response.headers.get("Content-Type", "") else payload


def load_tool(assets: Path, name: str):
    if name not in {"sheet_match", "blockout", "style_prompt", "style_metrics", "style_check", "style_compose", "asset_library", "scene_build"}:
        raise ValueError("Unsupported spike helper.")
    # Keep disabled for the host process lifetime, including lazy helper imports.
    sys.dont_write_bytecode = True
    directory = str(assets.resolve() / "tools")
    original_path = sys.path[:]
    sys.path.insert(0, directory)
    dependency = sys.modules.get("asset_library")
    if name == "scene_build":
        sys.modules["asset_library"] = load_tool(assets, "asset_library")
    try:
        # An embedding caller may previously have loaded a different spike tree.
        previous = sys.modules.pop(name, None)
        try:
            module = importlib.import_module(name)
            if Path(module.__file__).resolve() != (Path(directory) / f"{name}.py").resolve():
                raise ValueError(f"{name} must come from FORGE_SPIKE_ASSETS/tools.")
            return module
        finally:
            sys.modules.pop(name, None)
            if previous is not None:
                sys.modules[name] = previous
    finally:
        sys.path[:] = original_path
        if name == "scene_build":
            sys.modules.pop("asset_library", None)
            if dependency is not None:
                sys.modules["asset_library"] = dependency


def load_matcher(assets: Path):
    return load_tool(assets, "sheet_match")


def png(image) -> bytes:
    stream = BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


class ForgeWorker:
    def __init__(self, client: ForgeClient, assets: Path, *, style=None):
        self.client = client
        self.assets = assets.resolve()
        self._matcher = None
        self._tools = {}
        self.style = style
        if self.style is None and os.getenv("FORGE_STYLE_URL"):
            self.style = StyleClient(os.environ["FORGE_STYLE_URL"], float(os.getenv("FORGE_STYLE_TIMEOUT_S", "600")))
        self.style_wait_s = float(os.getenv("FORGE_STYLE_WAIT_S", "900"))
        if not math.isfinite(self.style_wait_s) or self.style_wait_s < 0:
            raise ValueError("FORGE_STYLE_WAIT_S must be finite and nonnegative.")
        self.critic = None
        override = os.getenv("FORGE_STORE_ROOT")
        self._store_override = None
        self._store_checked = False
        if override is not None:
            if not Path(override).is_absolute():
                raise ValueError("FORGE_STORE_ROOT must be absolute.")
            self._store_override = Path(override).resolve()
            self._check_store_disjoint(self._store_override)

    @property
    def matcher(self):
        if self._matcher is None:
            self._matcher = load_matcher(self.assets)
        return self._matcher

    def tool(self, name):
        if name not in self._tools:
            module = load_tool(self.assets, name)
            if name == "style_metrics":
                # evaluate has no scale option. This private module instance uses
                # half-size blur/erosion to preserve the full-frame spatial scale.
                # Full-frame float Lab blurs cost seconds per candidate at 2048².
                module.palette_drift = partial(module.palette_drift, blur=8.0)
                module.detail_gain = partial(module.detail_gain, blur=2.0, erode=4)
                module.change = partial(module.change, blur=0.0, erode=4)
            self._tools[name] = module
        return self._tools[name]

    @property
    def style_prompt(self):
        return self.tool("style_prompt")

    @property
    def style_metrics(self):
        return self.tool("style_metrics")

    @property
    def style_check(self):
        return self.tool("style_check")

    @property
    def style_compose(self):
        return self.tool("style_compose")

    def store_root(self) -> Path:
        if self._store_override is not None:
            if not self._store_checked:
                self._store_checked = True
                self._check_store_identity(self._store_override)
            return self._store_override
        root = Path(self.client.request("GET", "/status")["store_root"]).resolve()
        self._check_store_disjoint(root)
        return root

    def _check_store_disjoint(self, root: Path):
        if root.is_relative_to(self.assets) or self.assets.is_relative_to(root):
            raise ValueError("Forge store and spike tree must be disjoint.")

    def _check_store_identity(self, root: Path):
        # There is no generic probe-file write endpoint. Compare the API's
        # version index with versions.json, canonicalizing transport whitespace.
        # Never resolve, stat or open the container-reported path on the host.
        reported = "unavailable"
        try:
            reported = self.client.request("GET", "/status")["store_root"]
            pairs = self.client.request("GET", "/assets")
            compared = 0
            for pair in pairs:
                asset, variant = pair["asset"], pair["variant"]
                if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value)
                       or ".." in value for value in (asset, variant)):
                    raise ValueError("Invalid API store identity.")
                remote = self.client.request("GET", f"/assets/{asset}/variants/{variant}/versions")
                local = json.loads(checked_path(
                    root, f"assets/{asset}/variants/{variant}/versions.json").read_bytes())

                def digest(value):
                    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                                     allow_nan=False).encode()).digest()

                if digest(remote) != digest(local):
                    raise ValueError(f"versions.json hash mismatch for {asset}/{variant}")
                if remote:
                    compared += 1
            if not compared:
                raise ValueError("No nonempty version index available for identity comparison")
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "FORGE_STORE_ROOT identity unverified (API=%s, host=%s): %s; continuing",
                reported, root, exc)

    @contextmanager
    def bake_lock(self, job: dict, namespace: str = "spike"):
        if namespace not in {"spike", "workspace"}:
            raise ValueError("Invalid bake lock namespace.")
        suffix = ".workspace" if namespace == "workspace" else ""
        root = self.store_root()
        path = checked_path(root, f"locks/{job['asset']}__{job['variant']}{suffix}.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield None
                return
            try:
                yield root
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def generate_family(job: dict) -> bool:
        return (job.get("intent") in {"generate", "from_spec", "iterate_blockout"}
                or bool(job.get("inputs", {}).get("workspace_parent")))

    def workspace_root(self, job: dict) -> Path:
        root = self.store_root()
        for key in ("asset", "variant"):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", job[key]) or ".." in job[key]:
                raise ValueError("Invalid workspace identifier.")
        ws = checked_path(root, f"assets/{job['asset']}/workspace/{job['variant']}")
        pair = f"{job['asset']}/{job['variant']}"
        for directory in ("in", "specs", "prompts", "style", f"blockouts/{pair}", f"renders/{pair}",
                          f"styled/{pair}/views", f"bakes/{pair}"):
            checked_path(root, ws.relative_to(root) / directory).mkdir(parents=True, exist_ok=True)
        return ws

    def catalog(self) -> dict:
        """Read authored metadata only; the API container cannot see this tree."""
        specs, prompts, errors = [], [], []
        for directory in ("specs", "prompts"):
            for path in sorted((self.assets / directory).glob("*.yaml")):
                try:
                    data = yaml.safe_load(checked_path(self.assets, path.relative_to(self.assets)).read_bytes())
                    if not isinstance(data, dict):
                        raise ValueError("Expected a YAML mapping")
                    if directory == "specs":
                        variants, views = data.get("variants", {}), data["views"]
                        asset = data.get("asset") or path.stem
                        if (not isinstance(asset, str) or not isinstance(variants, dict)
                                or not isinstance(views, (dict, list))
                                or not all(isinstance(name, str) for name in [*variants, *views])):
                            raise ValueError("Invalid spec asset, variants or views")
                        specs.append({"asset": asset, "variants": sorted(variants), "views": sorted(views)})
                    else:
                        prompts.append(path.stem)
                except Exception as exc:
                    errors.append({"file": str(path.relative_to(self.assets))[:200], "error": str(exc)[:200]})
        return {"specs": sorted(specs, key=lambda spec: (spec["asset"], spec["variants"], spec["views"])),
                "prompts": sorted(prompts), "assets_root": str(self.assets),
                "published_at": datetime.now(timezone.utc).isoformat(),
                "errors": sorted(errors, key=lambda error: (error["file"], error["error"]))}

    def publish_catalog(self):
        try:
            catalog = self.catalog()
            result = self.client.request("POST", "/worker/catalog", catalog)
            print(f"CATALOG specs={len(catalog['specs'])} prompts={len(catalog['prompts'])}", flush=True)
            return result
        except Exception as exc:
            print(f"CATALOG publish failed: {type(exc).__name__}: {exc}", flush=True)
            return None

    def run_next(self):
        # Workspace locks span every generating/staging/baking subprocess. Take
        # them before claims so a busy workspace never strands a fresh lease.
        candidates = self.client.request("GET", "/jobs")
        for stages in (("matching", "review"), ("baking",)):
            for candidate in candidates:
                stage = {"uploaded": "matching", "queued_bake": "baking"}.get(candidate["state"], candidate["state"])
                if stage not in stages:
                    continue
                workspace = self.generate_family(candidate)
                lock = (self.bake_lock(candidate, "workspace" if workspace else "spike")
                        if workspace or stage == "baking" else nullcontext(True))
                with lock as root:
                    if root is None:
                        continue
                    job = self.client.request("POST", "/worker/claim", {
                        "kind": "pipeline", "stages": list(stages), "job_id": candidate["id"]})
                    if job is not None:
                        return self.process(job, bake_root=root if root is not True else None)
        return None

    def init_critic(self):
        from .forge_critic import CriticClient

        if self.critic is None:
            try:
                self.critic = CriticClient.from_env()
            except Exception as exc:
                # Client hard-refuses bad URLs; the optional lane cannot stop baking.
                self.critic = CriticClient("", enabled=False)
                self.critic.reason = f"Critic configuration refused: {exc}"
                print("CRITIC disabled for process lifetime: " + self.critic.reason, flush=True)
            self.critic.probe()

    def run_critic_next(self):
        self.init_critic()
        version = self.client.request("POST", "/worker/claim", {"kind": "critic"})
        if version is None:
            return None
        path = f"/assets/{version['asset']}/variants/{version['variant']}/versions/{version['number']}"
        verdict = self.critic.review(version, lambda name: self.client.request("GET", path + "/artifacts/" + name))
        result = self.client.request("POST", path + "/critic", {
            "lease_id": version["critic_lease"]["lease_id"], "verdict": verdict})
        print(f"CRITIC v{version['number']} " + json.dumps(result), flush=True)
        return result

    def run_export_next(self):
        claim = self.client.request("POST", "/worker/claim", {"kind": "export"})
        if claim is None:
            return None
        set_id, lease_id = claim["set_id"], claim["export_lease"]["lease_id"]
        endpoint = f"/worker/export/{set_id}"
        pending = library_root = None
        lock = None
        owned = False

        def check_lease():
            record = self.client.request("GET", f"/sets/{set_id}/export")
            if record["status"] != "running" or (record.get("lease") or {}).get("lease_id") != lease_id:
                raise ValueError("Missing, stale or expired export lease.")

        try:
            # Validate payload identifiers before using them in filesystem paths.
            from .forge_store import ForgeStore
            ForgeStore._name(set_id)
            root = self.store_root()
            directory = checked_path(root, f"sets/{set_id}")
            lock_path = checked_path(root, f"locks/export-{set_id}.lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock = lock_path.open("a+b")
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            check_lease()
            owned = True
            library_root = checked_path(root, directory / "library_root")
            # Recover only export-owned pending directories after a crashed worker.
            for stale in directory.glob(".pending-*"):
                shutil.rmtree(checked_path(root, stale))
            pending = checked_path(root, directory / f".pending-{uuid4().hex}")
            pending.mkdir()
            items, pairs = [], []
            for key, pair in claim["pairs"].items():
                asset, variant = key.split("/")
                ForgeStore._name(asset)
                ForgeStore._name(variant)
                number = pair["version"]
                item = {"asset": asset, "variant": variant, "version": number,
                        "accepted": bool(pair["accepted"]), "status": "exported"}
                items.append(item)
                if number is None:
                    item.update(status="skipped", reason="no version")
                    continue
                if type(number) is not int or number < 1:
                    raise ValueError("Version number must be a positive integer.")
                source_root = checked_path(root, f"assets/{asset}/variants/{variant}/versions/v{number}/artifacts")
                names = {name: f"bakes/{asset}/{variant}/{name}" for name in
                         (f"{asset}_{variant}.glb", f"{asset}_{variant}_lod.glb", "bake_report.json")}
                names["blockout/build_plan.json"] = f"blockouts/{asset}/{variant}/build_plan.json"
                for name, destination in names.items():
                    source = checked_path(source_root, name, allow_hardlinks=True)
                    if not source.is_file():
                        continue  # Repo B owns missing-bake skip decisions.
                    target = checked_path(pending, destination)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.link(source, target)
                    except OSError:
                        shutil.copy2(source, target)
                pairs.append((asset, variant))
            library_tool, scene = self.tool("asset_library"), self.tool("scene_build")
            library = library_tool.build_library(pending, pairs, digest=True)
            skipped = {(s["asset"], s["variant"]): s["reason"] for s in library["skipped"]}
            for item in items:
                reason = skipped.get((item["asset"], item["variant"]))
                if reason is not None:
                    item.update(status="skipped", reason=reason)
            manifest = claim["manifest"]
            report = scene.scene_report(manifest, items, library, scene.unbound_kinds(manifest, library), applied=True)
            library_tool.write_library(library, checked_path(pending, "library/asset_library.json"))
            for name, document in (("city_bindings", scene.bindings_document(manifest)), ("report", report)):
                checked_path(pending, f"library/{name}.json").write_text(
                    json.dumps(document, indent=2, allow_nan=False) + "\n", encoding="utf-8")
            result = {key: report[key] for key in ("items", "coverage", "status_counts")}
            result.update(skipped=library["skipped"], totals=library["totals"])
            check_lease()
            publish_library_root(pending, library_root)
            response = self.client.request("POST", endpoint + "/complete", {
                "lease_id": lease_id, "result": result, "host_store_root": str(root)})
            print(f"EXPORT set={set_id} pairs={len(items)} exported={library['totals']['variants']} "
                  f"skipped={sum(i['status'] == 'skipped' for i in items)}", flush=True)
            return response
        except Exception as exc:
            if pending is not None and pending.exists():
                shutil.rmtree(pending)
            # A reclaimed worker must never remove its successor's published tree.
            if owned:
                try:
                    check_lease()
                except Exception:
                    owned = False
            if owned and library_root is not None and library_root.exists():
                shutil.rmtree(library_root)
            print(f"EXPORT failed set={set_id}: {exc}", flush=True)
            return self.client.request("POST", endpoint + "/fail", {"lease_id": lease_id, "error": str(exc)})
        finally:
            if lock is not None:
                lock.close()

    def critic_loop(self, stopped: Event, interval: float):
        # Independent from run_next/process and their job-failure reporting path.
        self.init_critic()
        while not stopped.is_set():
            try:
                result = self.run_critic_next()
            except Exception as exc:
                print(f"CRITIC storage unavailable: {type(exc).__name__}: {exc}", flush=True)
                result = None
            if result is None:
                stopped.wait(interval)

    @staticmethod
    def command_override(override: str, values: dict) -> list[str]:
        # Tokenize first: spaces or shell metacharacters in paths remain data.
        # No-placeholder overrides keep exactly the historical argv.
        result = []
        for token in shlex.split(override):
            if token == "{inputs}":
                result.extend(str(value) for value in values.get("inputs", []))
                continue
            for key, value in values.items():
                if not isinstance(value, list):
                    token = token.replace("{" + key + "}", str(value))
            result.append(token)
        return result

    def bake_command(self, job: dict) -> list[str]:
        override = os.getenv("FORGE_BAKE_CMD")
        if override and not any(key in override for key in ("{assets_root}", "{spec}")):
            return shlex.split(override)
        ws = self.workspace_root(job) if self.generate_family(job) else self.assets
        spec = checked_path(ws, f"specs/{job['asset']}.yaml")
        if override:
            return self.command_override(override, {"assets_root": ws, "spec": spec})
        command = [os.getenv("FORGE_BAKE_PYTHON", "/home/alexk/.venv/bin/python"),
                   str(checked_path(self.assets, "tools/bake.py")),
                   "--asset", job["asset"], "--variant", job["variant"]]
        # .get(): jobs recorded before a parameter existed carry no key for it.
        for key in ("atlas_tile", "turntable", "ownership_min", "view_iou_warn", "view_iou_fail", "selfcheck_min"):
            if job["params"].get(key):
                command.extend(["--" + key.replace("_", "-"), str(job["params"][key])])
        if self.generate_family(job):
            command.extend(["--assets-root", str(ws), "--spec", str(spec)])
            # Zero staged views is the designed degraded mode for generated
            # assets (all panels rejected / views missing): bake.py's palette
            # fallback, exactly like the scene lane's palette-only sets.
            views = checked_path(ws, f"styled/{job['asset']}/{job['variant']}/views")
            if not list(views.glob("*.png")):
                command.append("--allow-palette-only")
        return command

    def run_workspace_command(self, job: dict, command: list[str], label: str):
        ws = self.workspace_root(job)
        # Both tools traverse their output roots. Check all existing descendants
        # before launch, including hardlinks, and again before harvesting.
        for path in ws.rglob("*"):
            checked_path(ws, path.relative_to(ws))
        log = checked_path(ws, f"{label}.log")
        with log.open("wb") as output:
            process = subprocess.Popen(command, cwd=ws, stdout=output, stderr=subprocess.STDOUT,
                                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, start_new_session=True)
            try:
                code = process.wait()
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
        self.progress(job, f"{label.upper()} exited rc={code}")
        if code:
            raise ValueError(f"{label} failed with rc={code}; log={log}")
        for path in ws.rglob("*"):
            checked_path(ws, path.relative_to(ws))

    def synth_command(self, job: dict, inputs: list[str], out: Path, report: Path):
        settings = job["generate"]
        values = {"inputs": inputs, "asset": job["asset"], "out": out,
                  "height_hint": settings["height_hint"], "floor_height": settings["floor_height"], "report": report}
        # Both the legacy {inputs} expansion and explicit edit placeholders work.
        values.update(edit_in=inputs[inputs.index("--edit-in") + 1] if "--edit-in" in inputs else "",
                      edit=inputs[inputs.index("--edit") + 1] if "--edit" in inputs else "")
        override = os.getenv("FORGE_SYNTH_CMD")
        if override:
            return self.command_override(override, values)
        return [os.getenv("FORGE_BAKE_PYTHON", "/home/alexk/.venv/bin/python"),
                str(checked_path(self.assets, "tools/spec_synth.py")), *inputs,
                "--asset", job["asset"], "--out", str(out), "--height-hint", str(settings["height_hint"]),
                "--floor-height", str(settings["floor_height"]), "--report", str(report)]

    def sources(self, job: dict, *, include_style=True):
        base = f"/jobs/{job['id']}"
        for index in job.get("inputs", {}).get("parent_uploads", []):
            yield f"p{index}", f"/jobs/{job['parent_job']}/uploads/{index}", {"parent_upload_index": index}
        for upload in job["uploads"]:
            yield f"u{upload['index']}", f"{base}/uploads/{upload['index']}", {"upload_index": upload["index"]}
        for index, _ in enumerate(job.get("generate", {}).get("segment_refs", [])):
            yield f"c{index}", f"{base}/cutouts/{index}.png", {"cutout_index": index}

        if include_style:
            for view, entry in (job.get("style") or {}).get("views", {}).items():
                for seed in entry["seeds"]:
                    yield f"s{view}-{seed}", f"{base}/style/{view}/{seed}.png", {"style_view": view, "seed": seed}

    def process_generate(self, job: dict) -> dict:
        ws = self.workspace_root(job)
        base = f"/jobs/{job['id']}"
        settings = job["generate"]
        spec_path = checked_path(ws, f"specs/{job['asset']}.yaml")
        report_path = checked_path(ws, "synth_report.json")
        if job.get("inputs", {}).get("workspace_parent"):
            lease = job["lease"]["lease_id"]
            spec_path.write_bytes(self.client.request("GET",
                f"/assets/{job['asset']}/variants/{job['variant']}/versions/{job['parent_version']}/artifacts/blockout/spec.yaml"))
            directory = checked_path(ws, f"renders/{job['asset']}/{job['variant']}")
            for path in directory.glob("*.png"):
                checked_path(ws, path.relative_to(ws)).unlink()
            self.client.request("PATCH", base, {"canonical_views": job["canonical_views"], "lease_id": lease})
            for view in job["canonical_views"]:
                data = self.client.request("GET", f"/jobs/{job['parent_job']}/renders/{view}.png")
                checked_path(ws, directory.relative_to(ws) / f"{view}.png").write_bytes(data)
                self.client.request("POST", base + f"/renders/{view}.png", data, lease=lease)
            self.client.request("POST", base + "/blockout", {
                "blockout": settings["blockout"], "lease_id": lease,
                "artifact": {"encoding": "base64", "data": base64.b64encode(spec_path.read_bytes()).decode("ascii")}})
            return self.render_masks(job, ws / "renders")
        authored = job["intent"] == "from_spec"
        if (settings.get("style") or {}).get("enabled") and self.style is None:
            raise ValueError("Styling engine not configured (FORGE_STYLE_URL)")
        pack_path = checked_path(ws, f"prompts/{job['asset']}.yaml")
        pack_path.unlink(missing_ok=True)
        if authored:
            source = checked_path(self.assets, f"specs/{settings['spec_asset']}.yaml")
            spec = yaml.safe_load(source.read_bytes())
            spec["asset"] = job["asset"]
            spec_path.write_text(yaml.safe_dump(spec, sort_keys=False))
            pack = checked_path(self.assets, f"prompts/{settings['spec_asset']}.yaml")
            if pack.is_file():
                pack_path.write_bytes(pack.read_bytes())
            report = {"source": "spec", "spec_asset": settings["spec_asset"],
                      "confidence": {}, "assumptions": ["Authored spec; no inference."], "next_view": ""}
        elif job["intent"] == "iterate_blockout":
            parent_spec = checked_path(ws, f"specs/{job['asset']}.parent.yaml")
            parent_spec.write_bytes(self.client.request("GET",
                f"/assets/{job['asset']}/variants/{job['variant']}/versions/{job['parent_version']}/artifacts/blockout/spec.yaml"))
            spec_path.unlink(missing_ok=True)
            report_path.unlink(missing_ok=True)
            inputs = ["--edit-in", str(parent_spec), "--edit", json.dumps(settings["edit"], allow_nan=False)]
            self.run_workspace_command(job, self.synth_command(job, inputs, spec_path, report_path), "synth-edit")
            report = json.loads(report_path.read_bytes())
            report["edit"] = settings["edit"]
            # Pure surgery has no inferred next-view recommendation.
            report["next_view"] = report.get("next_view") or ""
        else:
            inputs = []
            for name, route, metadata in self.sources(job, include_style=False):
                data = self.client.request("GET", route)
                image = self.matcher.load_image(BytesIO(data))
                if "upload_index" in metadata:
                    image.putalpha(self.matcher.match_mask(image, self.matcher.background_color(image)))
                    data = png(image)
                target = checked_path(ws, f"in/{name}.png")
                target.write_bytes(data)  # Always copy; never link into input stores.
                inputs.extend(["--image", str(target)])
            count = len(inputs) // 2
            if not 1 <= count <= 7:
                raise ValueError("spec_synth requires one to seven inputs; none may be silently dropped.")
            if count == 1:
                inputs.append("--force-single")
            if settings.get("tower_override") == "none":
                inputs.extend(["--override", "tower=none"])
            # No stale success on an override that exits without producing output.
            spec_path.unlink(missing_ok=True)
            report_path.unlink(missing_ok=True)
            self.run_workspace_command(job, self.synth_command(job, inputs, spec_path, report_path), "synth")
            report = json.loads(report_path.read_bytes())
        edit = {}
        if isinstance(settings.get("tower_override"), dict):
            edit["tower"] = settings["tower_override"]
        if settings.get("palette_hex"):
            edit["palette"] = {role: color.lstrip("#") for role, color in settings["palette_hex"].items()}
        if edit:
            edited = checked_path(ws, f"specs/{job['asset']}.edited.yaml")
            edit_report = checked_path(ws, "synth_edit_report.json")
            edited.unlink(missing_ok=True)
            edit_report.unlink(missing_ok=True)
            self.run_workspace_command(job, self.synth_command(job,
                ["--edit-in", str(spec_path), "--edit", json.dumps(edit, allow_nan=False)], edited, edit_report), "synth-edit")
            spec_path.write_bytes(edited.read_bytes())
            report["edit"] = json.loads(edit_report.read_bytes())
            report["assumptions"].append("Operator overrides applied after photo synthesis; confidence describes photo inference.")
        spec = yaml.safe_load(spec_path.read_bytes())
        if spec["asset"] != job["asset"] or (job["intent"] == "generate" and len(spec["views"]) != 5):
            raise ValueError("Generated spec must identify this asset and five canonical views.")
        views = sorted(spec["views"])
        if any(not re.fullmatch(r"[A-Za-z0-9_]+", view) for view in views):
            raise ValueError("Unsafe canonical view name.")
        # spec_synth owns the default geometry; an empty alias supports the UI's
        # named variant without changing massing or the companion source tree.
        spec["variants"].setdefault(job["variant"], {})
        spec_path.write_text(yaml.safe_dump(spec, sort_keys=False))
        report_path.write_text(json.dumps(report, allow_nan=False, indent=2))
        render_variant = job["variant"] if authored or job["intent"] == "iterate_blockout" else "default"
        default_renders = checked_path(ws, f"renders/{job['asset']}/{render_variant}")
        default_renders.mkdir(parents=True, exist_ok=True)
        for path in default_renders.rglob("*.png"):
            checked_path(ws, path.relative_to(ws)).unlink()
        checked_path(ws, f"blockouts/{job['asset']}/{render_variant}/build_plan.json").unlink(missing_ok=True)
        if render_variant != job["variant"]:
            checked_path(ws, f"blockouts/{job['asset']}/{job['variant']}/build_plan.json").unlink(missing_ok=True)
        override = os.getenv("FORGE_BLOCKOUT_CMD")
        command = (self.command_override(override, {"spec": spec_path, "out": ws, "variant": render_variant}) if override else
                   [os.getenv("FORGE_BAKE_PYTHON", "/home/alexk/.venv/bin/python"),
                    str(checked_path(self.assets, "tools/blockout.py")), "--spec", str(spec_path),
                    "--variant", render_variant, "--out", str(ws), "--views", "all"])
        self.run_workspace_command(job, command, "blockout")
        renders = {view: checked_path(ws, default_renders.relative_to(ws) / f"{view}.png").read_bytes()
                   for view in views}
        directory = checked_path(ws, f"renders/{job['asset']}/{job['variant']}")
        if directory != default_renders:
            for path in directory.rglob("*.png"):
                checked_path(ws, path.relative_to(ws)).unlink()
            for view, data in renders.items():
                checked_path(ws, directory.relative_to(ws) / f"{view}.png").write_bytes(data)
            for path in default_renders.glob("passes/*.depth.png"):
                target = checked_path(ws, directory.relative_to(ws) / "passes" / path.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(checked_path(ws, path.relative_to(ws)).read_bytes())
            plan = checked_path(ws, f"blockouts/{job['asset']}/{render_variant}/build_plan.json")
            if plan.is_file():
                checked_path(ws, f"blockouts/{job['asset']}/{job['variant']}/build_plan.json").write_bytes(plan.read_bytes())
        lease = job["lease"]["lease_id"]
        self.client.request("PATCH", base, {"canonical_views": views, "lease_id": lease})
        for view, data in renders.items():
            self.client.request("POST", base + f"/renders/{view}.png", data, lease=lease)
        resolved = (self.tool("blockout").apply_variant(spec, spec["variants"][job["variant"]] or {})
                    if authored or job["intent"] == "iterate_blockout" else spec)
        massing = resolved.get("massing", {})
        tower = massing.get("tower")
        if tower and not tower.get("enabled", False):
            tower = None
        plinth = massing.get("plinth")
        payload = {"params": {"footprint": massing.get("footprint", {}),
                    "height": (plinth["floors"] * plinth["floor_height"] if plinth else 0) + (tower["floors"] * tower["floor_height"] if tower else 0),
                    "plinth": {key: plinth[key] for key in ("floors", "floor_height")} if plinth else None,
                    "tower": {key: tower[key] for key in ("width", "floors", "location")} if tower else None},
                   "palette": {role: {"hex": color["hex"]} for role, color in resolved["palette"].items()},
                   "confidence": report["confidence"], "assumptions": report["assumptions"],
                   "next_view": report["next_view"], "synth_report": report, "views": views}
        if authored:
            plan = json.loads(checked_path(ws, f"blockouts/{job['asset']}/{job['variant']}/build_plan.json").read_bytes())
            lo, hi = plan["bbox"]["min"], plan["bbox"]["max"]
            payload["params"].update(footprint={"width": hi[0] - lo[0], "depth": hi[1] - lo[1]}, height=hi[2] - lo[2])
            payload["palette"] = plan["palette"]
        self.client.request("POST", base + "/blockout", {"blockout": payload, "lease_id": lease,
            "artifact": {"encoding": "base64", "data": base64.b64encode(spec_path.read_bytes()).decode("ascii")}})
        if job["intent"] in {"generate", "from_spec"} and (settings.get("style") or {}).get("enabled"):
            self.style_views(job, ws, views)
        return self.render_masks(job, ws / "renders")

    def style_views(self, job: dict, ws: Path, views: list[str]) -> dict:
        from PIL import Image

        if self.style is None:
            raise ValueError("Styling engine not configured (FORGE_STYLE_URL)")
        settings = job["generate"]["style"]
        asset, variant = job["asset"], job["variant"]
        base, lease = f"/jobs/{job['id']}", job["lease"]["lease_id"]
        pack_path = checked_path(ws, f"prompts/{asset}.yaml")
        pack = yaml.safe_load(pack_path.read_bytes()) if pack_path.is_file() else {
            "descriptions": {variant: f"{asset} {variant} building"}}
        if settings["prompt_override"] is not None:
            pack = {"short": {variant: settings["prompt_override"]}}
        if variant not in (pack.get("short") or {}) and variant not in (pack.get("descriptions") or {}):
            # An undeclared variant alias has no authored prompt; a bare KeyError
            # from build_prompt would not tell the operator what to supply.
            raise ValueError(f"No prompt for variant {variant!r} in prompts/{asset}.yaml; "
                             "set style.prompt_override or add a short/descriptions entry")
        palette = json.loads(checked_path(ws, f"blockouts/{asset}/{variant}/build_plan.json").read_bytes())["palette"]
        refs = []
        for ref in job.get("style_refs", []):
            data = self.client.request("GET", base + f"/style/refs/{ref['index']}")
            # Uploads accept JPEG/WebP/GIF as well; the sidecar accepts PNG only.
            with Image.open(BytesIO(data)) as image:
                refs.append(base64.b64encode(png(image.convert("RGB"))).decode("ascii"))
        report = {"views": {}, "prompt_tokens": {}, "model": "", "refs": len(refs), "params": settings}
        result = {"chosen": {}}
        for view_index, view in enumerate(views):
            init_path = checked_path(ws, f"renders/{asset}/{variant}/{view}.png")
            depth_path = checked_path(ws, f"renders/{asset}/{variant}/passes/{view}.depth.png")
            if not depth_path.is_file():
                raise ValueError(f"No depth pass for {view}; regenerate the blockout with the current tools")
            prompt = self.style_prompt.build_prompt(pack, variant, view, palette)
            seeds = [settings["seed_base"] + 100 * i + view_index for i in range(settings["seeds_per_view"])]
            request = {"view": view, "init_png": base64.b64encode(init_path.read_bytes()).decode("ascii"),
                       "depth_png": base64.b64encode(depth_path.read_bytes()).decode("ascii"),
                       "refs": refs, "prompt": prompt["prompt"],
                       "negative": settings["negative_override"] if settings["negative_override"] is not None else prompt["negative"],
                       "seeds": seeds, **{key: settings[key] for key in
                           ("strength", "guidance", "control_scale", "ip_scale", "steps", "long_side")}}
            deadline = time.monotonic() + self.style_wait_s
            while True:
                try:
                    response = self.style.render(request)
                    break
                except StyleBusy as exc:
                    self.progress(job, f"STYLE deferred: gpu busy free={exc.free_mb} needed={exc.needed_mb}")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise StyleError("Styling engine remained GPU busy until FORGE_STYLE_WAIT_S elapsed") from exc
                    time.sleep(min(20, remaining))
                    if time.monotonic() >= deadline:
                        raise StyleError("Styling engine remained GPU busy until FORGE_STYLE_WAIT_S elapsed") from exc
            candidates = response.get("candidates", [])
            returned = [candidate.get("seed") for candidate in candidates]
            if (any(type(seed) is not int for seed in returned) or len(returned) != len(seeds)
                    or set(returned) != set(seeds)):
                raise StyleError("Styling engine returned unexpected candidate seeds")
            init = self.matcher.load_image(init_path)
            # At 2048², use 1024² copies with half-size blur/erosion (see tool()).
            size = (init.width // 2, init.height // 2)
            small_init = init.resize(size, Image.Resampling.LANCZOS)
            small_alpha = init.getchannel("A").resize(size, Image.Resampling.NEAREST)
            # style_check's rules are alpha-only and every candidate keeps the render's
            # alpha byte-for-byte, so a rule the render itself fails (a roof view framed
            # off-centre by the camera) says nothing about the restyle. Those failures
            # are inherited, recorded, and excluded from the candidate's verdict.
            render_checks = self.style_check.check_png(init_path)
            inherited = {name for name, rule in render_checks.get("rules", {}).items() if not rule.get("pass")}
            if inherited:
                self.progress(job, f"STYLE-CHECK {view} inherits render failures: {' '.join(sorted(inherited))}")
            scored = {}
            for candidate in candidates:
                seed = candidate["seed"]
                data = base64.b64decode(candidate["png"], validate=True)
                path = checked_path(ws, f"style/{view}/{seed}.png")
                path.parent.mkdir(parents=True, exist_ok=True)
                with Image.open(BytesIO(data)) as decoded:
                    if (decoded.format != "PNG" or decoded.mode != "RGBA" or decoded.size != init.size
                            or decoded.getchannel("A").tobytes() != init.getchannel("A").tobytes()):
                        raise StyleError("Style candidate must preserve the render's RGBA frame and alpha")
                    small = decoded.resize(size, Image.Resampling.LANCZOS)
                path.write_bytes(data)
                metrics = self.style_metrics.evaluate(small, small_init, small_alpha)
                checks = self.style_check.check_png(path)
                failed = {name for name, rule in checks.get("rules", {}).items() if not rule.get("pass")}
                checks["inherited"] = sorted(failed & inherited)
                checks["render_pass"] = bool(render_checks.get("pass"))
                checks["pass"] = bool(checks.get("pass")) or not (failed - inherited)
                scored[seed] = {"metrics": metrics, "checks": checks}
                self.client.request("POST", base + f"/style/{view}/{seed}.png", data, lease=lease)
            chosen = max(seeds, key=lambda seed: (scored[seed]["metrics"]["pass"],
                scored[seed]["checks"]["pass"], -scored[seed]["metrics"]["palette_drift"]))
            result[view], result["chosen"][view] = scored, chosen
            report["views"][view] = {"seeds": seeds, "chosen": chosen,
                                      "metrics": {str(seed): score for seed, score in scored.items()}}
            report["prompt_tokens"][view] = response.get("prompt_tokens")
            report["model"] = response.get("model", "unknown")
            score = scored[chosen]["metrics"]
            self.progress(job, f"STYLE {view} seeds={len(seeds)} chosen={chosen} drift={score['palette_drift']} "
                               f"change={score['change']} detail={score['detail_gain']} pass={score['pass']} "
                               f"tokens={response.get('prompt_tokens')}")
            if response.get("truncated"):
                self.progress(job, f"STYLE-TRUNCATED {view}")
        path = checked_path(ws, "style/report.json")
        path.write_text(json.dumps(report, allow_nan=False, indent=2))
        saved = self.client.request("POST", base + "/style", {"report": report, "lease_id": lease})
        job["style"] = saved["style"]
        return result

    @staticmethod
    def staged_style_seeds(job: dict) -> dict:
        """View -> seed of the style candidate the submitted review accepted.

        The style report's `chosen` is the worker's metric ranking; a human may
        pick another seed in review (SE3 "Use this seed"). Staging already
        honours the decision, so version custody must follow the decision too,
        never the ranking. Views whose review accepted no style candidate
        (photo panel or missing view) are absent from the result.
        """
        panels = {p["panel_id"]: p for p in job.get("match", {}).get("panels", []) if "style_view" in p}
        seeds = {}
        for decision in job.get("match", {}).get("decisions", []):
            panel = panels.get(decision.get("panel_id"))
            if panel is not None and decision.get("decision") != "reject":
                seeds[decision.get("view") or panel["style_view"]] = panel["seed"]
        return seeds

    def restore_workspace(self, job: dict, *, staged=False):
        # Another job for this pair may have used the shared workspace while the
        # operator reviewed this one. Restore this job's API-owned bytes first.
        ws = self.workspace_root(job)
        base = f"/jobs/{job['id']}"
        checked_path(ws, f"specs/{job['asset']}.yaml").write_bytes(
            self.client.request("GET", base + "/blockout/spec.yaml"))
        directory = checked_path(ws, f"renders/{job['asset']}/{job['variant']}")
        for path in directory.glob("*.png"):
            checked_path(ws, path.relative_to(ws)).unlink()
        for view in job["canonical_views"]:
            checked_path(ws, directory.relative_to(ws) / f"{view}.png").write_bytes(
                self.client.request("GET", base + f"/renders/{view}.png"))
        if job.get("style"):
            report = self.client.request("GET", base + "/style")
            checked_path(ws, "style/report.json").write_text(json.dumps(report, allow_nan=False, indent=2))
            staged_seeds = self.staged_style_seeds(job)
            for view, entry in report["views"].items():
                for seed in {entry["chosen"], staged_seeds.get(view, entry["chosen"])}:
                    path = checked_path(ws, f"style/{view}/{seed}.png")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(self.client.request("GET", base + f"/style/{view}/{seed}.png"))
        if staged:
            self.copy_workspace_views(job)
        return ws

    def copy_workspace_views(self, job: dict):
        ws = self.workspace_root(job)
        directory = checked_path(ws, f"styled/{job['asset']}/{job['variant']}/views")
        views = sorted({p["view"] for p in job["match"]["decisions"] if p["decision"] != "reject"}
                       | set(job.get("inputs", {}).get("parent_views_inherited", [])))
        payloads = {view: self.client.request("GET", f"/jobs/{job['id']}/staged/views/{view}.png") for view in views}
        for path in directory.glob("*.png"):
            checked_path(ws, path.relative_to(ws)).unlink()
        for view, data in payloads.items():
            checked_path(ws, directory.relative_to(ws) / f"{view}.png").write_bytes(data)

    def restore_snapshot(self, backup: Path, views: Path, plan: Path):
        manifest = json.loads(checked_path(backup, "snapshot.json").read_bytes())
        if manifest["spike_assets"] != str(self.assets):
            raise ValueError("Snapshot belongs to a different spike tree.")
        # Verify every backup before changing either spike location.
        copies = {}
        for name, digest in manifest["files"].items():
            data = checked_path(backup, name).read_bytes()
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError(f"Snapshot hash mismatch: {name}")
            copies[name] = data
        for path in views.glob("*.png"):
            checked_path(self.assets, path.relative_to(self.assets)).unlink()
        for name, data in copies.items():
            if name.startswith("views/"):
                checked_path(self.assets, views.relative_to(self.assets) / Path(name).name).write_bytes(data)
        checked_path(self.assets, plan.relative_to(self.assets))
        if manifest["plan_existed"]:
            plan.write_bytes(copies["build_plan.json"])
        else:
            plan.unlink(missing_ok=True)
        if not manifest["views_existed"] and views.exists():
            views.rmdir()
        actual = {f"views/{p.name}": hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in views.glob("*.png")}
        if plan.exists():
            actual["build_plan.json"] = hashlib.sha256(plan.read_bytes()).hexdigest()
        if actual != manifest["files"]:
            raise ValueError("Restored snapshot differs from saved names/hashes.")
        manifest["restored"] = True
        checked_path(backup, "snapshot.json").write_text(json.dumps(manifest, indent=2))

    @contextmanager
    def staged_snapshot(self, job: dict, root: Path):
        pair = Path(job["asset"]) / job["variant"]
        views = checked_path(self.assets, Path("styled") / pair / "views")
        plan = checked_path(self.assets, Path("blockouts") / pair / "build_plan.json")
        jobs = checked_path(root, Path("assets") / job["asset"] / "variants" / job["variant"] / "jobs")
        # Recover a worker crash before a later job can snapshot abandoned views.
        for retained in sorted(jobs.glob("*/views_backup/snapshot.json")):
            retained = checked_path(root, retained.relative_to(root))
            if not json.loads(retained.read_bytes())["restored"]:
                self.restore_snapshot(retained.parent, views, plan)
                self.progress(job, f"RESTORE recovered {retained.parent.parent.name}")
        backup = checked_path(root, jobs.relative_to(root) / job["id"] / "views_backup")
        backup.mkdir(parents=True, exist_ok=True)
        manifest = {"spike_assets": str(self.assets),
                    "views_existed": views.exists(), "plan_existed": plan.exists(),
                    "files": {}, "restored": False}
        paths = sorted(views.glob("*.png")) + ([plan] if plan.exists() else [])
        for path in paths:
            data = checked_path(self.assets, path.relative_to(self.assets)).read_bytes()
            name = "build_plan.json" if path == plan else f"views/{path.name}"
            target = checked_path(root, backup.relative_to(root) / name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            manifest["files"][name] = hashlib.sha256(data).hexdigest()
        checked_path(root, backup.relative_to(root) / "snapshot.json").write_text(json.dumps(manifest, indent=2))
        try:
            views.mkdir(parents=True, exist_ok=True)
            staged = sorted({p["view"] for p in job["match"]["decisions"] if p["decision"] != "reject"}
                            | set(job.get("inputs", {}).get("parent_views_inherited", [])))
            if not staged:
                raise ValueError("Bake requires staged views.")
            # Fetch all input bytes before mutating the shared drop zone.
            payloads = {view: self.client.request("GET", f"/jobs/{job['id']}/staged/views/{view}.png")
                        for view in staged}
            for path in views.glob("*.png"):
                checked_path(self.assets, path.relative_to(self.assets)).unlink()
            for view, data in payloads.items():
                checked_path(self.assets, views.relative_to(self.assets) / f"{view}.png").write_bytes(data)
            yield backup
        finally:
            self.restore_snapshot(backup, views, plan)
            self.progress(job, "RESTORE views and build_plan snapshot verified")

    def execute_bake(self, job: dict, backup: Path):
        target = self.workspace_root(job) if self.generate_family(job) else self.assets
        output = checked_path(target, Path("bakes") / job["asset"] / job["variant"])
        # The fixed-path tool also writes here; reject aliases before launching it.
        for path in output.rglob("*"):
            checked_path(target, path.relative_to(target))
        log = checked_path(target, output.relative_to(target) / "blender.log")
        previous = {p.relative_to(output).as_posix(): fingerprint(p) for p in output.rglob("*") if p.is_file()}
        seen = ""
        pending = ""

        def tail(final=False):
            nonlocal seen, pending
            if fingerprint(log) == previous.get("blender.log") or not log.exists():
                return
            checked_path(target, log.relative_to(target))
            content = log.read_text(errors="replace")
            if not content.startswith(seen):
                seen, pending = "", ""
            pending += content[len(seen):]
            seen = content
            lines = pending.split("\n")
            pending = lines.pop()
            if final and pending:
                lines.append(pending)
                pending = ""
            for line in lines:
                if BAKE_MARKERS.search(line):
                    self.progress(job, line)

        # bake.py owns Blender's PYTHONHOME/PYTHONPATH. Only suppress bytecode.
        if self.generate_family(job):
            # bake.py re-resolves the spec. Do not publish the earlier render
            # plan if a custom bake command fails to produce its own plan.
            checked_path(target, f"blockouts/{job['asset']}/{job['variant']}/build_plan.json").unlink(missing_ok=True)
        with checked_path(backup, "bake-command.log").open("wb") as stdout:
            process = subprocess.Popen(self.bake_command(job), stdout=stdout, stderr=subprocess.STDOUT,
                                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, start_new_session=True)
            try:
                while process.poll() is None:
                    tail()
                    time.sleep(0.05)
                tail(final=True)
                self.progress(job, f"BLENDER exited rc={process.returncode}")
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
        if process.returncode != 0 or "BAKE " not in seen:
            raise ValueError(f"Bake failed: rc={process.returncode}; fresh BAKE marker={'BAKE ' in seen}")
        names = [f"{job['asset']}_{job['variant']}.glb", "atlas.png", "bake_report.json",
                 "harmonize_report.json", "blender.log"]
        lod = f"{job['asset']}_{job['variant']}_lod.glb"
        if checked_path(target, output.relative_to(target) / lod).exists():
            names.append(lod)
        else:
            self.progress(job, f"LOD absent — bake tool emitted no {lod}")
        frames = []
        if job["params"]["turntable"]:
            frames = [f"turntable/tt_{i:02d}.png" for i in range(job["params"]["turntable"])]
        artifacts = {}
        raw = {}
        for name in names + frames:
            path = checked_path(target, output.relative_to(target) / name)
            if fingerprint(path) is None or fingerprint(path) == previous.get(name):
                raise ValueError(f"Missing or stale bake artifact: {name}")
            raw[name] = path.read_bytes()
            artifacts[name] = {"encoding": "base64", "data": base64.b64encode(raw[name]).decode("ascii")}
        report = json.loads(raw["bake_report.json"])
        harmonize = json.loads(raw["harmonize_report.json"])
        if (report["asset"], report["variant"]) != (job["asset"], job["variant"]):
            raise ValueError("Bake report identity differs from job.")
        metrics = {key: report[key] for key in ("coverage", "bleed_avoided", "bleed_faces", "fallback_split")}
        metrics.update(parts_total=len(report["parts"]),
                       part_layers=dict(Counter(part["layer"] for part in report["parts"])),
                       harmonize_drift={view["view"]: view["drift_mean"] for view in harmonize["views"]},
                       turntable_frames=len(frames))
        # The installed tool emits selfcheck only in blender.log, not its JSON.
        if "selfcheck" in report:
            metrics["selfcheck"] = report["selfcheck"]
        else:
            source = checked_path(self.assets, "tools/bake_views.py").read_text()
            threshold = re.search(r"^SELFCHECK_THRESHOLD\s*=\s*([0-9.]+)", source, re.M)
            if threshold is None:
                raise ValueError("Cannot determine bake selfcheck threshold.")
            metrics["selfcheck"] = {"threshold": float(threshold[1]), "views": {
                view: float(iou) for view, iou in re.findall(r"SELFCHECK (\S+) silhouette IoU=([0-9.]+)", seen)}}
        # Everything above this line is a Blender-side measurement, and v1 passed
        # all of it while exporting a mesh that renders as nothing in a real-time
        # engine. Read the exported bytes instead, and refuse to publish a
        # version the client cannot draw — a broken artifact marked ready is
        # worse than a failed job, because the critic then scores it.
        glb_name = f"{job['asset']}_{job['variant']}.glb"
        metrics["glb"] = forge_glb.inspect(raw[glb_name], raw.get("atlas.png"))
        unusable = forge_glb.violations(metrics["glb"])
        if unusable:
            raise ValueError(f"Exported GLB is unusable: {'; '.join(unusable)}")
        self.progress(job, f"GLB-CHECK ok textured={metrics['glb']['textured']} "
                           f"uv_sets={metrics['glb']['uv_sets']} "
                           f"alpha={','.join(metrics['glb']['alpha_modes']) or 'none'}")
        metrics["glb_lod"] = None
        if lod in raw:
            metrics["glb_lod"] = forge_glb.inspect(raw[lod], raw.get("atlas.png"))
            unusable = forge_glb.violations(metrics["glb_lod"])
            if unusable:
                raise ValueError(f"Exported LOD GLB is unusable: {'; '.join(unusable)}")
            self.progress(job, f"GLB-LOD-CHECK ok textured={metrics['glb_lod']['textured']} "
                               f"uv_sets={metrics['glb_lod']['uv_sets']} "
                               f"alpha={','.join(metrics['glb_lod']['alpha_modes']) or 'none'}")
        if self.generate_family(job):
            data = checked_path(target, f"specs/{job['asset']}.yaml").read_bytes()
            artifacts["blockout/spec.yaml"] = {"encoding": "base64", "data": base64.b64encode(data).decode("ascii")}
            plan = checked_path(target, f"blockouts/{job['asset']}/{job['variant']}/build_plan.json")
            if not plan.is_file():
                raise ValueError(f"Missing generate-family build plan: {plan}")
            artifacts["blockout/build_plan.json"] = {"encoding": "base64", "data": base64.b64encode(plan.read_bytes()).decode("ascii")}
            blockout = job["generate"]["blockout"]
            metrics["synth"] = {"confidence": blockout["confidence"], "params": blockout["params"]}
            if job.get("style"):
                report = json.loads(checked_path(target, "style/report.json").read_bytes())
                # Custody follows the submitted review: the retained PNG and the
                # per-view seed are what the bake actually consumed, while
                # auto_seed keeps the worker's ranking for the record.
                staged_seeds = self.staged_style_seeds(job)
                names = ["style/report.json"] + [f"style/{view}/{staged_seeds[view]}.png"
                                                for view in report["views"] if view in staged_seeds]
                for name in names:
                    artifacts[name] = {"encoding": "base64", "data": base64.b64encode(
                        checked_path(target, name).read_bytes()).decode("ascii")}
                views = {}
                for view, entry in report["views"].items():
                    seed = staged_seeds.get(view)
                    scored = entry["metrics"][str(seed if seed is not None else entry["chosen"])]["metrics"]
                    views[view] = {"seed": seed, "auto_seed": entry["chosen"], "staged": seed is not None,
                                   **{key: scored[key] for key in ("pass", "palette_drift", "change", "detail_gain")}}
                metrics["style"] = {"views": views, **{key: report[key] for key in ("model", "refs", "prompt_tokens")}}
        return artifacts, metrics

    def progress(self, job: dict, *markers: str, **fields):
        for marker in markers:
            print(marker, flush=True)
        return self.client.request("POST", f"/jobs/{job['id']}/progress", {
            "lease_id": job["lease"]["lease_id"], "markers": list(markers), **fields})

    @contextmanager
    def heartbeats(self, job: dict):
        stop = Event()
        errors = []

        def beat():
            while not stop.wait(30):
                try:
                    self.progress(job)
                except Exception as exc:
                    errors.append(exc)
                    return

        thread = Thread(target=beat, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()
        if errors:
            raise errors[0]

    def render_masks(self, job: dict, renders_root: Path | None = None) -> dict:
        root = self.assets if renders_root is None else self.workspace_root(job)
        renders = root / "renders" if renders_root is None else renders_root
        directory = checked_path(root, renders.relative_to(root) / job["asset"] / job["variant"])
        masks = {}
        for path in sorted(directory.glob("*.png")):
            image = self.matcher.load_image(checked_path(root, path.relative_to(root)))
            masks[path.stem] = self.matcher.object_mask(image, self.matcher.background_color(image))
        if not masks:
            raise ValueError(f"No canonical renders for {job['asset']}/{job['variant']}")
        return masks

    def match(self, job: dict, masks: dict) -> dict:
        tool = self.matcher
        base = f"/jobs/{job['id']}"
        lease = job["lease"]["lease_id"]
        self.client.request("PATCH", base, {"canonical_views": list(masks), "lease_id": lease})
        panels = []
        for name, route, metadata in self.sources(job):
            image = tool.load_image(BytesIO(self.client.request("GET", route)))
            bg = tool.background_color(image)
            detection = tool.object_mask(image, bg)
            mask = tool.match_mask(image, bg)
            rgba = image.copy()
            rgba.putalpha(mask)
            # A style candidate is one full-frame view, even with detached details.
            boxes = [mask.getbbox()] if "style_view" in metadata else tool.find_panels(detection)
            boxes = [box for box in boxes if box is not None]
            # Crops retain all pixels inside their bbox. A contained component
            # (e.g. a detached roof detail) is already in the outer crop; emitting
            # it again would double-count the same pixels as an extra panel.
            boxes = [b for b in boxes if not any(
                b != outer and outer[0] <= b[0] and outer[1] <= b[1]
                and outer[2] >= b[2] and outer[3] >= b[3] for outer in boxes)]
            for index, bbox in enumerate(boxes):
                panels.append({"panel_id": f"{name}-p{index}", **metadata,
                               "bbox": list(bbox), "rgba": rgba.crop(bbox), "mask": mask.crop(bbox)})
        self.progress(job, f"MATCH panels={len(panels)}")
        # Only photo/cutout panels participate in greedy silhouette assignment.
        ordinary = [i for i, panel in enumerate(panels) if "style_view" not in panel]
        accepted, rejects = tool.match_panels([panels[i]["mask"] for i in ordinary], masks,
                                               job["params"]["iou"], job["params"]["margin"])
        scores = {ordinary[entry["panel"]]: entry for entry in accepted + rejects}
        findings = []
        for index, panel in enumerate(panels):
            entry = scores.get(index, {"view": panel.get("style_view"), "iou": 1.0})
            finding = {k: v for k, v in panel.items() if k not in {"rgba", "mask"}}
            finding.update({k: v for k, v in entry.items() if k != "panel"})
            finding["auto_view"] = entry["view"] if "reason" not in entry else None
            finding["decision"] = "reject" if "reason" in entry else "accept"
            findings.append(finding)
            self.client.request("POST", base + f"/panels/{panel['panel_id']}", png(panel["rgba"]), lease=lease)
            self.progress(job, f"VIEW {entry['view']} iou={entry['iou']:.4f}" +
                          (f" reject={entry['reason']}" if "reason" in entry else ""))
        styled_views = set((job.get("style") or {}).get("views", {}))
        for finding in findings:
            if "style_view" in finding:
                view, seed = finding["style_view"], finding["seed"]
                entry = job["style"]["views"][view]
                finding.update(source="style", **entry["metrics"][str(seed)])
                if seed == entry["chosen"]:
                    finding.update(decision="accept", view=view, auto_view=view)
                    finding.pop("reason", None)
                else:
                    finding.update(decision="reject", reason="alternate", view=view, auto_view=view)
            elif finding["decision"] != "reject" and finding["view"] in styled_views:
                finding.update(decision="reject", reason="style_wins")
        claimed = sorted(f["view"] for f in findings if f["decision"] != "reject")
        missing = sorted(set(masks) - set(claimed)
                         - set(job.get("inputs", {}).get("parent_views_inherited", [])))
        extras = [f["panel_id"] for f in findings if f["decision"] == "reject" and "style_view" not in f]
        rejects = [{"panel": i, "view": f.get("view"), "iou": f.get("iou"), "reason": f["reason"]}
                   for i, f in enumerate(findings) if f["decision"] == "reject"]
        return {"panels": findings, "views_claimed": claimed, "views_missing": missing,
                "extras": extras, "rejects": rejects,
                "extras_allowed": bool(job["params"]["allow_extra"] and not missing)}

    def materialize(self, job: dict, masks: dict):
        if masks is not None and list(masks) != job["canonical_views"]:
            raise ValueError("Canonical render listing changed since MATCH.")
        base = f"/jobs/{job['id']}"
        replaced = {p["view"] for p in job["match"]["decisions"] if p["decision"] != "reject"}
        for view in job.get("inputs", {}).get("parent_views_inherited", []):
            if view in replaced:
                continue
            payload = self.client.request("GET", f"/jobs/{job['parent_job']}/staged/views/{view}.png")
            self.client.request("POST", base + f"/staged/views/{view}.png", payload,
                                lease=job["lease"]["lease_id"])
            self.progress(job, f"VIEW {view} inherited parent={job['parent_job']}")
        for decision in job["match"]["decisions"]:
            if decision["decision"] == "reject":
                continue
            view = decision["view"]
            finding = next((p for p in job["match"]["panels"] if p["panel_id"] == decision["panel_id"]), {})
            if "style_view" in finding:
                if view != finding["style_view"]:
                    raise ValueError("Styled candidates must stage to their original canonical view")
                seed = finding["seed"]
                payload = self.client.request("GET", base + f"/style/{view}/{seed}.png")
                self.client.request("POST", base + f"/staged/views/{view}.png", payload,
                                    lease=job["lease"]["lease_id"])
                self.progress(job, f"VIEW {view} style seed={seed} staged")
                continue
            panel = self.matcher.load_image(BytesIO(self.client.request(
                "GET", base + f"/panels/{decision['panel_id']}")))
            aligned, score = self.matcher.align_to_view(panel, masks[view])
            if aligned is None:
                raise ValueError(f"Cannot align panel {decision['panel_id']} to {view}")
            self.client.request("POST", base + f"/staged/views/{view}.png", png(aligned),
                                lease=job["lease"]["lease_id"])
            self.progress(job, f"VIEW {view} iou={score:.4f} staged decision={decision['decision']}")

    def process(self, job: dict, *, bake_root: Path | None = None):
        if (job["state"] == "baking" or self.generate_family(job)) and bake_root is None:
            with self.bake_lock(job, "workspace" if self.generate_family(job) else "spike") as root:
                if root is None:
                    return None
                return self.process(job, bake_root=root)
        try:
            if job["state"] != "baking":
                return self.process_match(job)
            with self.heartbeats(job):
                self.progress(job, f"START baking job={job['id']}")
                if self.generate_family(job):
                    ws = self.restore_workspace(job, staged=True)
                    for path in ws.rglob("*"):
                        checked_path(ws, path.relative_to(ws))
                    if not list(checked_path(ws, f"styled/{job['asset']}/{job['variant']}/views").glob("*.png")):
                        self.progress(job, "NO staged views — palette-only degraded bake")
                    artifacts, metrics = self.execute_bake(job, ws)
                else:
                    with self.staged_snapshot(job, bake_root) as backup:
                        artifacts, metrics = self.execute_bake(job, backup)
            version = self.client.request("POST", "/worker/complete", {
                "job_id": job["id"], "lease_id": job["lease"]["lease_id"],
                "artifacts": artifacts, "metrics": metrics})
            print(f"DONE ready job={job['id']} version=v{version['number']}", flush=True)
            self.publish_catalog()
            return self.client.request("GET", f"/jobs/{job['id']}")
        except BaseException as exc:
            try:
                fields = ({"state": "failed"} if job["state"] == "baking"
                          or not isinstance(exc, (HTTPError, URLError, OSError)) else {})
                self.progress(job, f"ERROR {type(exc).__name__}: {exc}", error=str(exc), **fields)
            except Exception as report_error:
                print(f"ERROR reporting failure: {report_error}", file=sys.stderr, flush=True)
            raise

    def process_match(self, job: dict):
        if job["state"] not in {"matching", "review"}:
            raise ValueError("MATCH worker cannot process this state.")
        if job["state"] == "review" and not job["match"].get("submitted"):
            raise ValueError("Review has not been submitted.")
        with self.heartbeats(job):
            self.progress(job, f"START {job['state']} job={job['id']}")
            empty_iteration = job["intent"] in {"iterate_params", "iterate_views"} and not job["uploads"]
            if self.generate_family(job):
                if job["state"] == "matching":
                    masks = self.process_generate(job)
                else:
                    ws = self.restore_workspace(job)
                    masks = self.render_masks(job, ws / "renders")
            else:
                masks = None if empty_iteration else self.render_masks(job)
            if job["state"] == "matching":
                if empty_iteration:
                    missing = sorted(set(job["canonical_views"]) - set(job["inputs"]["parent_views_inherited"]))
                    report = {"panels": [], "views_claimed": [], "views_missing": missing}
                    self.progress(job, "MATCH skipped: parameter iteration uses inherited views")
                else:
                    report = self.match(job, masks)
            else:
                self.materialize(job, masks)
                if self.generate_family(job):
                    self.copy_workspace_views(job)
        # Stop heartbeats before finalization clears the lease.
        body = {"lease_id": job["lease"]["lease_id"]}
        if job["state"] == "matching":
            body["report"] = report
        suffix = "match" if job["state"] == "matching" else "staged"
        result = self.client.request("POST", f"/jobs/{job['id']}/{suffix}", body)
        palette_only = job["intent"] == "from_spec" and job["generate"]["palette_only"]
        if job["state"] == "matching" and ((empty_iteration and not report["views_missing"])
                                           or (palette_only and not report["panels"])):
            if not result["match"].get("submitted"):
                self.client.request("POST", f"/jobs/{job['id']}/review", {
                    "mode": "submit", "panels": [], "views_missing": report["views_missing"]})
            claimed = self.client.request("POST", "/worker/claim", {"stages": ["review"], "job_id": job["id"]})
            if claimed and palette_only:
                self.progress(claimed, "MATCH skipped: palette-only authored spec")
            return self.process_match(claimed) if claimed else result
        print(f"DONE {result['state']} job={job['id']}", flush=True)
        return result


def main() -> int:
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"Worker received signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    once = truthy(os.getenv("FORGE_ONCE", ""))
    interval = float(os.getenv("FORGE_POLL_INTERVAL", "2"))
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("FORGE_POLL_INTERVAL must be finite and positive.")
    client = ForgeClient(os.getenv("FORGE_API", "http://127.0.0.1:8070"), os.getenv("FORGE_WORKER_TOKEN", ""))
    worker = ForgeWorker(client, Path(os.getenv("FORGE_SPIKE_ASSETS", str(DEFAULT_SPIKE_ASSETS))))
    worker.init_critic()  # Emits the existing CRITIC probe diagnostic.
    worker.publish_catalog()
    if not once:
        # Model calls never occupy the pipeline loop, even when a bake arrives mid-review.
        Thread(target=worker.critic_loop, args=(Event(), interval), daemon=True).start()
    while True:
        try:
            result = worker.run_next()
            if once:
                try:
                    worker.run_critic_next()
                except Exception as exc:
                    print(f"CRITIC storage unavailable: {type(exc).__name__}: {exc}", flush=True)
                worker.run_export_next()
                return 0
            if result is None:
                result = worker.run_export_next()
            if result is None:
                time.sleep(interval)
        except Exception as exc:
            print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            if once:
                return 1
            time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
