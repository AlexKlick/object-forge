"""Host-only MATCH worker. All durable writes go through the Forge HTTP API.

The spike's pure image helpers are imported with bytecode writes disabled;
never call its run/main functions, which write beneath the spike assets tree.
"""
from __future__ import annotations

from contextlib import contextmanager
from io import BytesIO
import importlib
import ipaddress
import json
import math
import os
from pathlib import Path
import sys
from threading import Event, Thread
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

DEFAULT_SPIKE_ASSETS = Path("/home/alexk/debt-city-greybox-spike/apps/greybox/assets")


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
        self.matcher = load_matcher(self.assets)

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
        missing = sorted(set(masks) - set(claimed))
        extras = [panels[e["panel"]]["panel_id"] for e in rejects]
        return {"panels": findings, "views_claimed": claimed, "views_missing": missing,
                "extras": extras, "rejects": rejects,
                "extras_allowed": bool(job["params"]["allow_extra"] and not missing)}

    def materialize(self, job: dict, masks: dict):
        if list(masks) != job["canonical_views"]:
            raise ValueError("Canonical render listing changed since MATCH.")
        base = f"/jobs/{job['id']}"
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

    def process(self, job: dict):
        if job["state"] not in {"matching", "review"}:
            raise ValueError("MATCH worker cannot process this state.")
        if job["state"] == "review" and not job["match"].get("submitted"):
            raise ValueError("Review has not been submitted.")
        with self.heartbeats(job):
            self.progress(job, f"START {job['state']} job={job['id']}")
            masks = self.render_masks(job)
            if job["state"] == "matching":
                report = self.match(job, masks)
            else:
                self.materialize(job, masks)
        # Stop heartbeats before finalization clears the lease.
        body = {"lease_id": job["lease"]["lease_id"]}
        if job["state"] == "matching":
            body["report"] = report
        suffix = "match" if job["state"] == "matching" else "staged"
        result = self.client.request("POST", f"/jobs/{job['id']}/{suffix}", body)
        print(f"DONE {result['state']} job={job['id']}", flush=True)
        return result


def main() -> int:
    once = truthy(os.getenv("FORGE_ONCE", ""))
    interval = float(os.getenv("FORGE_POLL_INTERVAL", "2"))
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("FORGE_POLL_INTERVAL must be finite and positive.")
    client = ForgeClient(os.getenv("FORGE_API", "http://127.0.0.1:8070"), os.getenv("FORGE_WORKER_TOKEN", ""))
    worker = ForgeWorker(client, Path(os.getenv("FORGE_SPIKE_ASSETS", str(DEFAULT_SPIKE_ASSETS))))
    while True:
        job = None
        try:
            job = client.request("POST", "/worker/claim", {"kind": "pipeline", "stages": ["matching", "review"]})
            if job is not None:
                worker.process(job)
                if once:
                    return 0
            elif once:
                # A bounded one-shot invocation is also safe against already staged jobs.
                return 0
            else:
                time.sleep(interval)
        except Exception as exc:
            print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            if job is not None:
                try:
                    # Transport/process failures retain state for lease-expiry retry.
                    fields = {} if isinstance(exc, (HTTPError, URLError, OSError)) else {"state": "failed"}
                    worker.progress(job, f"ERROR {type(exc).__name__}: {exc}", error=str(exc), **fields)
                except Exception as report_error:
                    print(f"ERROR reporting failure: {report_error}", file=sys.stderr, flush=True)
            if once:
                return 1
            time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
