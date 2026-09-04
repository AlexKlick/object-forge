from __future__ import annotations

from datetime import timedelta
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from threading import Thread
import time
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw
import uvicorn
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from open_sprite_pipeline.forge_api import forge_router
from open_sprite_pipeline.forge_store import ForgeStore
from open_sprite_pipeline.forge_worker import DEFAULT_SPIKE_ASSETS, ForgeClient, ForgeWorker, png


class ForgeWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="forge-p2-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.assets = self.root / "spike"
        renders = self.assets / "renders/testa/v1"
        renders.mkdir(parents=True)
        (self.assets / "tools").mkdir()
        # Copy the real pure helpers into a synthetic fixture before the read-only boundary.
        (self.assets / "tools/sheet_match.py").write_bytes((DEFAULT_SPIKE_ASSETS / "tools/sheet_match.py").read_bytes())
        self.images = {}
        polygons = {
            "front": [(40, 220), (128, 35), (215, 220)],
            "rear": [(40, 40), (90, 40), (90, 165), (215, 165), (215, 220), (40, 220)],
            "left": [(40, 40), (215, 40), (215, 90), (155, 90), (155, 220), (100, 220), (100, 90), (40, 90)],
            "right": [(40, 40), (215, 40), (215, 220)],
            "roof": [(100, 35), (155, 35), (155, 100), (220, 100), (220, 155), (155, 155), (155, 220), (100, 220), (100, 155), (35, 155), (35, 100), (100, 100)],
        }
        for view, polygon in polygons.items():
            image = Image.new("RGBA", (256, 256), "white")
            ImageDraw.Draw(image).polygon(polygon, fill=(50, 90, 150, 255))
            if view == "rear":
                ImageDraw.Draw(image).rectangle((130, 90, 162, 122), fill=(50, 90, 150, 255))
            image.save(renders / f"{view}.png")
            self.images[view] = image
        self.sheet = Image.new("RGBA", (620, 530), "white")
        for view, size, xy in [("left", 215, (20, 20)), ("front", 240, (320, 20)), ("rear", 185, (30, 300))]:
            self.sheet.paste(self.images[view].resize((size, size)), xy)
        ImageDraw.Draw(self.sheet).ellipse((380, 330, 500, 450), fill=(190, 40, 60, 255))
        self.before = self.snapshot()
        self.addCleanup(self.assert_spike_unchanged)
        env = patch.dict(os.environ, {"FORGE_WORKER_TOKEN": "unit-worker-token"})
        env.start()
        self.addCleanup(env.stop)
        app = FastAPI()
        app.state.forge_store = ForgeStore(self.root / "store")
        app.include_router(forge_router)
        self.store = app.state.forge_store
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        self.thread = Thread(target=self.server.run, kwargs={"sockets": [sock]}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        deadline = time.monotonic() + 10
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                self.fail("test HTTP server did not start")
            time.sleep(0.01)
        self.client = ForgeClient(self.url, "unit-worker-token")

    def stop_server(self):
        self.server.should_exit = True
        self.thread.join(10)
        self.assertFalse(self.thread.is_alive())

    def snapshot(self):
        return {str(p.relative_to(self.assets)): (p.stat().st_mtime_ns,
                hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
                for p in [self.assets, *sorted(self.assets.rglob("*"))]}

    def assert_spike_unchanged(self):
        self.assertEqual(self.before, self.snapshot())

    def upload(self, image=None, params=None):
        boundary = "forge-unit-boundary"
        fields = {"asset": "testa", "variant": "v1", "params": json.dumps(params or {})}
        body = b""
        for name, value in fields.items():
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="sheet.png"\r\nContent-Type: image/png\r\n\r\n'.encode()
                 + png(image or self.sheet) + f'\r\n--{boundary}--\r\n'.encode())
        from urllib.request import Request, urlopen
        with urlopen(Request(self.url + "/v1/forge/jobs", data=body,
                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}), timeout=10) as response:
            self.assertEqual(response.status, 201)
            return json.load(response)

    def run_worker(self, expected=0):
        env = {**os.environ, "FORGE_API": self.url, "FORGE_SPIKE_ASSETS": str(self.assets),
               "FORGE_ONCE": "1", "FORGE_POLL_INTERVAL": "0.01", "PYTHONPATH": str(ROOT / "src")}
        log = self.root / f"worker-{time.monotonic_ns()}.log"
        with log.open("w") as output:
            result = subprocess.run([sys.executable, "-m", "open_sprite_pipeline.forge_worker"],
                                    env=env, stdout=output, stderr=subprocess.STDOUT, timeout=60)
        self.assertEqual(result.returncode, expected, log.read_text())

    def matched(self):
        job = self.upload()
        self.run_worker()
        job = self.client.request("GET", f"/jobs/{job['id']}")
        self.assertEqual(job["state"], "review")
        return job

    def decisions(self, job):
        panels = [{"panel_id": p["panel_id"], "decision": p["decision"], "view": p["auto_view"]}
                  for p in job["match"]["panels"]]
        return {"mode": "submit", "panels": panels, "views_missing": job["match"]["views_missing"]}

    def submit(self, job, body=None):
        result = self.client.request("POST", f"/jobs/{job['id']}/review", body or self.decisions(job))
        self.assertEqual(result["state"], "review")
        self.assertTrue(result["match"]["submitted"])
        return result

    def staged_bytes(self, job):
        directory = self.store._job_path(job["id"]).parent / "staged/views"
        return {p.stem: p.read_bytes() for p in directory.glob("*.png")}

    def test_composite_matches_three_views_rejects_decoy_and_waits_for_review(self):
        job = self.matched()
        self.assertEqual(len(job["canonical_views"]), 5)
        self.assertEqual(len(job["match"]["panels"]), 4)
        self.assertEqual(job["match"]["views_claimed"], ["front", "left", "rear"])
        self.assertEqual(job["match"]["views_missing"], ["right", "roof"])
        accepted = [p for p in job["match"]["panels"] if p["auto_view"]]
        self.assertTrue(all(p["iou"] >= 0.70 for p in accepted))
        self.assertEqual(len(job["match"]["rejects"]), 1)
        for panel in job["match"]["panels"]:
            data = self.client.request("GET", f"/jobs/{job['id']}/panels/{panel['panel_id']}")
            self.assertEqual(Image.open(BytesIO(data)).mode, "RGBA")
            self.assertEqual(len(panel["bbox"]), 4)
        self.run_worker()
        self.assertEqual(self.store.get_job(job["id"]), job)
        lines = (self.store._job_path(job["id"]).parent / "worker.log").read_text()
        self.assertIn("MATCH panels=4", lines)
        self.assertIn("VIEW front iou=", lines)

    def test_repin_materializes_decided_view_and_explicit_missing(self):
        job = self.matched()
        body = self.decisions(job)
        repin = next(p for p in body["panels"] if p["view"] == "front")
        repin.update(decision="repin", view="roof")
        body["views_missing"] = ["front", "right"]
        self.submit(job, body)
        self.run_worker()
        staged = self.store.get_job(job["id"])
        self.assertEqual(staged["state"], "staged")
        self.assertEqual(staged["match"]["views_missing"], ["front", "right"])
        images = self.staged_bytes(job)
        self.assertEqual(set(images), {"left", "rear", "roof"})
        worker = ForgeWorker(self.client, self.assets)
        panel = worker.matcher.load_image(BytesIO(self.client.request("GET", f"/jobs/{job['id']}/panels/{repin['panel_id']}")))
        expected, _ = worker.matcher.align_to_view(panel, worker.render_masks(job)["roof"])
        self.assertEqual(images["roof"], png(expected))

    def test_full_frame_upload_is_one_panel(self):
        job = self.upload(self.images["front"])
        self.run_worker()
        job = self.store.get_job(job["id"])
        self.assertEqual(job["state"], "review")
        self.assertEqual(len(job["match"]["panels"]), 1)
        self.assertEqual(job["match"]["panels"][0]["auto_view"], "front")

    def test_contained_component_is_not_emitted_twice(self):
        worker = ForgeWorker(self.client, self.assets)
        image = self.images["rear"]
        mask = worker.matcher.object_mask(image, worker.matcher.background_color(image))
        self.assertEqual(len(worker.matcher.find_panels(mask)), 2)
        job = self.upload(image)
        self.run_worker()
        job = self.store.get_job(job["id"])
        self.assertEqual(len(job["match"]["panels"]), 1)
        finding = job["match"]["panels"][0]
        self.assertEqual(finding["auto_view"], "rear")
        panel = Image.open(BytesIO(self.client.request("GET", f"/jobs/{job['id']}/panels/{finding['panel_id']}")))
        # The detached detail remains in the retained panel, with its alpha intact.
        self.assertEqual(panel.getpixel((145 - finding["bbox"][0], 105 - finding["bbox"][1]))[3], 255)

    def test_submitted_review_crash_retry_and_staged_noop_are_byte_identical(self):
        job = self.submit(self.matched())
        claimed = self.client.request("POST", "/worker/claim", {"kind": "pipeline"})
        worker = ForgeWorker(self.client, self.assets)
        original = worker.client.request

        def crash(method, path, body=None, **kwargs):
            if path.endswith("/staged"):
                raise OSError("crash before staged state publication")
            return original(method, path, body, **kwargs)

        with patch.object(worker.client, "request", side_effect=crash):
            with self.assertRaises(OSError):
                worker.process(claimed)
        before = self.staged_bytes(job)
        self.assertEqual(len(before), 3)
        self.assertEqual(self.store.get_job(job["id"])["state"], "review")
        with patch.object(self.store, "_now", return_value=self.store._now() + timedelta(seconds=301)):
            self.run_worker()
        self.assertEqual(self.store.get_job(job["id"])["state"], "staged")
        self.assertEqual(before, self.staged_bytes(job))
        record = self.store.get_job(job["id"])
        self.run_worker()
        self.assertEqual(record, self.store.get_job(job["id"]))
        self.assertEqual(before, self.staged_bytes(job))

    def test_matching_expired_lease_redoes_uploads_deterministically(self):
        job = self.upload()
        first = self.client.request("POST", "/worker/claim", {"kind": "pipeline"})
        worker = ForgeWorker(self.client, self.assets)
        report = worker.match(first, worker.render_masks(first))
        before = {p.name: p.read_bytes() for p in (self.store._job_path(job["id"]).parent / "match/panels").glob("*.png")}
        with patch.object(self.store, "_now", return_value=self.store._now() + timedelta(seconds=301)):
            self.run_worker()
        matched = self.store.get_job(job["id"])
        self.assertEqual(matched["match"]["panels"], report["panels"])
        for name, data in before.items():
            self.assertEqual((self.store._job_path(job["id"]).parent / "match/panels" / name).read_bytes(), data)

    def test_review_requires_exact_panel_set_and_complete_view_partition(self):
        from urllib.error import HTTPError
        job = self.matched()
        valid = self.decisions(job)
        missing_decoy = {**valid, "panels": [p for p in valid["panels"] if p["decision"] != "reject"]}
        unknown = {**valid, "panels": valid["panels"] + [{"panel_id": "unknown", "decision": "reject"}]}
        duplicate = {**valid, "panels": valid["panels"] + valid["panels"][:1]}
        for body in (missing_decoy, unknown, duplicate, {**valid, "views_missing": []}):
            with self.assertRaises(HTTPError) as raised:
                self.client.request("POST", f"/jobs/{job['id']}/review", body)
            self.assertEqual(raised.exception.code, 422)
        self.submit(job)
        with self.assertRaises(HTTPError) as raised:
            self.client.request("POST", f"/jobs/{job['id']}/review", valid)
        self.assertEqual(raised.exception.code, 409)

    def test_worker_mutations_require_token_lease_and_staging_files(self):
        from urllib.error import HTTPError
        job = self.upload()
        first = self.client.request("POST", "/worker/claim", {"kind": "pipeline"})
        base = f"/jobs/{job['id']}"
        routes = [("PATCH", base, {"canonical_views": ["front"], "lease_id": "bad"}),
                  ("POST", base + "/match", {"report": {}, "lease_id": "bad"}),
                  ("POST", base + "/panels/p0", png(self.images["front"])),
                  ("POST", base + "/staged/views/front.png", png(self.images["front"])),
                  ("POST", base + "/staged", {"lease_id": "bad"})]
        for method, path, body in routes:
            with self.assertRaises(HTTPError) as raised:
                ForgeClient(self.url, "wrong").request(method, path, body)
            self.assertEqual(raised.exception.code, 403)
            with self.assertRaises(HTTPError) as raised:
                self.client.request(method, path, body)
            self.assertEqual(raised.exception.code, 409)
        ForgeWorker(self.client, self.assets).process(first)
        job = self.submit(self.store.get_job(job["id"]))
        claim = self.client.request("POST", "/worker/claim", {"kind": "pipeline"})
        with self.assertRaises(HTTPError) as raised:
            self.client.request("POST", base + "/staged", {"lease_id": claim["lease"]["lease_id"]})
        self.assertEqual(raised.exception.code, 409)

    def test_missing_views_and_blank_upload_are_reviewable(self):
        job = self.upload(Image.new("RGBA", (256, 256), "white"))
        self.run_worker()
        job = self.store.get_job(job["id"])
        self.assertEqual(job["state"], "review")
        self.assertEqual(job["match"]["panels"], [])
        self.assertEqual(job["match"]["views_missing"], job["canonical_views"])
        self.submit(job)
        self.run_worker()
        self.assertEqual(self.store.get_job(job["id"])["state"], "staged")
        self.assertEqual(self.staged_bytes(job), {})

    def test_allow_extra_summary_and_threshold_parameters(self):
        job = self.upload(params={"iou": 1.0, "margin": 1.0, "allow_extra": True})
        self.run_worker()
        job = self.store.get_job(job["id"])
        self.assertEqual(job["state"], "review")
        self.assertEqual(job["match"]["views_claimed"], [])
        self.assertEqual(len(job["match"]["extras"]), 4)
        self.assertFalse(job["match"]["extras_allowed"])

    def test_allow_extra_with_all_canonical_views_claimed(self):
        sheet = Image.new("RGBA", (800, 540), "white")
        for index, image in enumerate(self.images.values()):
            sheet.paste(image, ((index % 3) * 265, (index // 3) * 270))
        ImageDraw.Draw(sheet).ellipse((590, 330, 710, 450), fill="red")
        job = self.upload(sheet, {"allow_extra": True})
        self.run_worker()
        job = self.store.get_job(job["id"])
        self.assertEqual(len(job["match"]["views_claimed"]), 5)
        self.assertEqual(len(job["match"]["extras"]), 1)
        self.assertTrue(job["match"]["extras_allowed"])
        self.assertEqual(job["match"]["views_missing"], [])

    def test_worker_does_not_claim_bake_or_later_states(self):
        for target in ("staged", "queued_bake", "baking", "ready"):
            job = self.store.create_job("testa", "v1")
            for state in ("matching", "review", "staged", "queued_bake", "baking", "ready"):
                self.store.set_state(job["id"], state)
                if state == target:
                    break
        before = self.store.list_jobs()
        self.run_worker()
        self.assertEqual(before, self.store.list_jobs())

    def test_heartbeat_runs_while_image_work_is_busy(self):
        from threading import Event
        job = self.upload(self.images["front"])
        claimed = self.client.request("POST", "/worker/claim", {"kind": "pipeline"})
        worker = ForgeWorker(self.client, self.assets)
        observed = Event()
        stop = Event()
        intervals = []
        original_progress = worker.progress

        class AcceleratedEvent:
            def wait(self, interval):
                intervals.append(interval)
                return stop.wait(0.01)

            def set(self):
                stop.set()

        def progress(record, *markers, **fields):
            result = original_progress(record, *markers, **fields)
            if not markers:
                observed.set()
            return result

        original_masks = worker.render_masks

        def slow_masks(record):
            self.assertTrue(observed.wait(5), "No heartbeat during busy work")
            return original_masks(record)

        with patch("open_sprite_pipeline.forge_worker.Event", AcceleratedEvent), \
                patch.object(worker, "progress", side_effect=progress), \
                patch.object(worker, "render_masks", side_effect=slow_masks):
            worker.process(claimed)
        self.assertTrue(intervals)
        self.assertEqual(set(intervals), {30})
        self.assertEqual(self.store.get_job(job["id"])["state"], "review")

    def test_loopback_only_and_missing_render_error_marker(self):
        for url in ("http://example.com", "http://192.168.1.2", "http://127.0.0.1@evil.test", "https://127.0.0.1"):
            with self.assertRaises(ValueError):
                ForgeClient(url)
        job = self.store.create_job("absent", "v1")
        self.run_worker(expected=1)
        self.assertEqual(self.store.get_job(job["id"])["state"], "failed")
        self.assertIn("No canonical renders", (self.store._job_path(job["id"]).parent / "worker.log").read_text())


if __name__ == "__main__":
    unittest.main()
