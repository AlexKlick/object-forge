"""Host-only MATCH and bake worker. Job/version state uses the Forge HTTP API.

The spike's pure image helpers are imported with bytecode writes disabled;
never call its run/main functions, which write beneath the spike assets tree.
"""
from __future__ import annotations

from contextlib import contextmanager
from collections import Counter
import base64
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
import signal
import subprocess
import sys
from threading import Event, Thread
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from . import forge_glb

DEFAULT_SPIKE_ASSETS = Path("/home/alexk/debt-city-greybox-spike/apps/greybox/assets")
BAKE_MARKERS = re.compile(r"\b(?:PROJECT|SELFCHECK|VIEW-VALIDATE|TURNTABLE|BAKE|VERIFY)\b")


def checked_path(root: Path, relative: str | Path) -> Path:
    """Reject aliases before any transient write or artifact read."""
    path = root / relative
    if not path.is_relative_to(root) or ".." in path.parts:
        raise ValueError("Path escapes worker root.")
    for component in (path, *path.parents):
        if component == root:
            break
        if component.is_symlink():
            raise ValueError(f"Symlink forbidden: {component}")
    if path.is_file() and path.stat().st_nlink != 1:
        raise ValueError(f"Hardlink forbidden: {path}")
    return path


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


def load_matcher(assets: Path):
    # Keep disabled for the host process lifetime, including lazy helper imports.
    sys.dont_write_bytecode = True
    directory = str(assets.resolve() / "tools")
    sys.path.insert(0, directory)
    try:
        # An embedding caller may previously have loaded a different spike tree.
        previous = sys.modules.pop("sheet_match", None)
        try:
            module = importlib.import_module("sheet_match")
            if Path(module.__file__).resolve() != (Path(directory) / "sheet_match.py").resolve():
                raise ValueError("sheet_match must come from FORGE_SPIKE_ASSETS/tools.")
            return module
        finally:
            sys.modules.pop("sheet_match", None)
            if previous is not None:
                sys.modules["sheet_match"] = previous
    finally:
        sys.path.remove(directory)


def png(image) -> bytes:
    stream = BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


class ForgeWorker:
    def __init__(self, client: ForgeClient, assets: Path):
        self.client = client
        self.assets = assets.resolve()
        self._matcher = None
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
    def bake_lock(self, job: dict):
        root = self.store_root()
        path = checked_path(root, f"locks/{job['asset']}__{job['variant']}.lock")
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

    def run_next(self):
        job = self.client.request("POST", "/worker/claim", {
            "kind": "pipeline", "stages": ["matching", "review"]})
        if job is not None:
            return self.process(job)
        for candidate in self.client.request("GET", "/jobs"):
            if candidate["state"] not in {"queued_bake", "baking"}:
                continue
            # Lock BEFORE claiming: a competing process leaves its job queued.
            with self.bake_lock(candidate) as root:
                if root is None:
                    continue
                job = self.client.request("POST", "/worker/claim", {
                    "kind": "pipeline", "stages": ["baking"], "job_id": candidate["id"]})
                if job is not None:
                    return self.process(job, bake_root=root)
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

    def bake_command(self, job: dict) -> list[str]:
        override = os.getenv("FORGE_BAKE_CMD")
        if override:
            return shlex.split(override)
        command = [os.getenv("FORGE_BAKE_PYTHON", "/home/alexk/.venv/bin/python"),
                   str(checked_path(self.assets, "tools/bake.py")),
                   "--asset", job["asset"], "--variant", job["variant"]]
        for key in ("atlas_tile", "turntable", "ownership_min", "view_iou_warn", "view_iou_fail"):
            if job["params"][key]:
                command.extend(["--" + key.replace("_", "-"), str(job["params"][key])])
        return command

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
        output = checked_path(self.assets, Path("bakes") / job["asset"] / job["variant"])
        # The fixed-path tool also writes here; reject aliases before launching it.
        for path in output.rglob("*"):
            checked_path(self.assets, path.relative_to(self.assets))
        log = checked_path(self.assets, output.relative_to(self.assets) / "blender.log")
        previous = {p.relative_to(output).as_posix(): fingerprint(p) for p in output.rglob("*") if p.is_file()}
        seen = ""
        pending = ""

        def tail(final=False):
            nonlocal seen, pending
            if fingerprint(log) == previous.get("blender.log") or not log.exists():
                return
            checked_path(self.assets, log.relative_to(self.assets))
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
        frames = []
        if job["params"]["turntable"]:
            frames = [f"turntable/tt_{i:02d}.png" for i in range(job["params"]["turntable"])]
        artifacts = {}
        raw = {}
        for name in names + frames:
            path = checked_path(self.assets, output.relative_to(self.assets) / name)
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

    def render_masks(self, job: dict) -> dict:
        directory = (self.assets / "renders" / job["asset"] / job["variant"]).resolve()
        if not directory.is_relative_to(self.assets):
            raise ValueError("Render directory escapes spike assets.")
        masks = {}
        for path in sorted(directory.glob("*.png")):
            if not path.resolve().is_relative_to(self.assets):
                raise ValueError("Render image escapes spike assets.")
            image = self.matcher.load_image(path)
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
        for upload in job["uploads"]:
            image = tool.load_image(BytesIO(self.client.request("GET", base + f"/uploads/{upload['index']}")))
            bg = tool.background_color(image)
            detection = tool.object_mask(image, bg)
            mask = tool.match_mask(image, bg)
            rgba = image.copy()
            rgba.putalpha(mask)
            boxes = tool.find_panels(detection)
            # Crops retain all pixels inside their bbox. A contained component
            # (e.g. a detached roof detail) is already in the outer crop; emitting
            # it again would double-count the same pixels as an extra panel.
            boxes = [b for b in boxes if not any(
                b != outer and outer[0] <= b[0] and outer[1] <= b[1]
                and outer[2] >= b[2] and outer[3] >= b[3] for outer in boxes)]
            for index, bbox in enumerate(boxes):
                panels.append({"panel_id": f"u{upload['index']}-p{index}", "upload_index": upload["index"],
                               "bbox": list(bbox), "rgba": rgba.crop(bbox), "mask": mask.crop(bbox)})
        self.progress(job, f"MATCH panels={len(panels)}")
        accepted, rejects = tool.match_panels([p["mask"] for p in panels], masks,
                                               job["params"]["iou"], job["params"]["margin"])
        scores = {entry["panel"]: entry for entry in accepted + rejects}
        findings = []
        for index, panel in enumerate(panels):
            entry = scores[index]
            finding = {k: v for k, v in panel.items() if k not in {"rgba", "mask"}}
            finding.update({k: v for k, v in entry.items() if k != "panel"})
            finding["auto_view"] = entry["view"] if "reason" not in entry else None
            finding["decision"] = "reject" if "reason" in entry else "accept"
            findings.append(finding)
            self.client.request("POST", base + f"/panels/{panel['panel_id']}", png(panel["rgba"]), lease=lease)
            self.progress(job, f"VIEW {entry['view']} iou={entry['iou']:.4f}" +
                          (f" reject={entry['reason']}" if "reason" in entry else ""))
        claimed = sorted(entry["view"] for entry in accepted)
        missing = sorted(set(masks) - set(claimed)
                         - set(job.get("inputs", {}).get("parent_views_inherited", [])))
        extras = [panels[e["panel"]]["panel_id"] for e in rejects]
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
            panel = self.matcher.load_image(BytesIO(self.client.request(
                "GET", base + f"/panels/{decision['panel_id']}")))
            aligned, score = self.matcher.align_to_view(panel, masks[view])
            if aligned is None:
                raise ValueError(f"Cannot align panel {decision['panel_id']} to {view}")
            self.client.request("POST", base + f"/staged/views/{view}.png", png(aligned),
                                lease=job["lease"]["lease_id"])
            self.progress(job, f"VIEW {view} iou={score:.4f} staged decision={decision['decision']}")

    def process(self, job: dict, *, bake_root: Path | None = None):
        if job["state"] == "baking" and bake_root is None:
            with self.bake_lock(job) as root:
                if root is None:
                    return None
                return self.process(job, bake_root=root)
        try:
            if job["state"] != "baking":
                return self.process_match(job)
            with self.heartbeats(job):
                self.progress(job, f"START baking job={job['id']}")
                with self.staged_snapshot(job, bake_root) as backup:
                    artifacts, metrics = self.execute_bake(job, backup)
            version = self.client.request("POST", "/worker/complete", {
                "job_id": job["id"], "lease_id": job["lease"]["lease_id"],
                "artifacts": artifacts, "metrics": metrics})
            print(f"DONE ready job={job['id']} version=v{version['number']}", flush=True)
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
            empty_iteration = job["parent_job"] and not job["uploads"]
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
        # Stop heartbeats before finalization clears the lease.
        body = {"lease_id": job["lease"]["lease_id"]}
        if job["state"] == "matching":
            body["report"] = report
        suffix = "match" if job["state"] == "matching" else "staged"
        result = self.client.request("POST", f"/jobs/{job['id']}/{suffix}", body)
        if job["state"] == "matching" and empty_iteration and not report["views_missing"]:
            self.client.request("POST", f"/jobs/{job['id']}/review", {"mode": "submit", "panels": []})
            claimed = self.client.request("POST", "/worker/claim", {"stages": ["review"], "job_id": job["id"]})
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
                return 0
            if result is None:
                time.sleep(interval)
        except Exception as exc:
            print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            if once:
                return 1
            time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
