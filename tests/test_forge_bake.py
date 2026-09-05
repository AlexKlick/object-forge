from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import fcntl
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from open_sprite_pipeline.forge_api import forge_router
from open_sprite_pipeline.forge_store import ForgeStore
from open_sprite_pipeline.forge_worker import ForgeWorker


FAKE = r"""
import json
from pathlib import Path
import sys
import time
root, mode = Path(sys.argv[1]), sys.argv[2]
views = root / "styled/a/v/views"
assert sorted(p.name for p in views.glob("*.png")) == ["front.png"]
assert (views / "front.png").read_bytes() == b"staged front"
plan = root / "blockouts/a/v/build_plan.json"
plan.write_text('{"regenerated": true}')
out = root / "bakes/a/v"
if mode == "stale_log":
    sys.exit(0)
with (out / "blender.log").open("w") as log:
    log.write("PROJECT front faces=3\n")
    log.flush()
    time.sleep(0.2)
    log.write("SELFCHECK front silhouette IoU=0.9900\nSELFCHECK PASS\n")
    if mode != "no_marker":
        log.write("BAKE a_v.glb\n")
report = {"asset": "a", "variant": "v", "parts": [{"layer": "core"}, {"layer": "core"}, {"layer": "social"}],
          "coverage": {"front": 0.8, "palette": 0.2}, "bleed_avoided": 2, "bleed_faces": 0,
          "fallback_split": {"underside": 0.1, "gap": 0.1}}
(out / "bake_report.json").write_text(json.dumps(report, separators=(",", ":")))
(out / "harmonize_report.json").write_text(json.dumps({"views": [{"view": "front", "drift_mean": 0.03}]}))
(out / "a_v.glb").write_bytes(b"glTF fake model")
if mode != "stale_atlas":
    (out / "atlas.png").write_bytes(b"atlas fake bytes")
(out / "turntable/tt_00.png").write_bytes(b"turntable fake bytes")
sys.exit(7 if mode == "fail" else 0)
"""


class ApiClient:
    """Exercise the actual P1 HTTP routes without Blender or host services."""
    def __init__(self, http):
        self.http = http

    def request(self, method, path, body=None, *, lease=None):
        headers = {"X-Forge-Worker": "test-bake"}
        if lease:
            headers["X-Forge-Lease"] = lease
        kwargs = {"content": body} if isinstance(body, bytes) else {"json": body}
        response = self.http.request(method, "/v1/forge" + path, headers=headers, **kwargs)
        response.raise_for_status()
        if response.status_code == 204:
            return None
        return response.json() if "application/json" in response.headers.get("content-type", "") else response.content


class ForgeBakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="forge-p3-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.assets = self.root / "spike"
        self.views = self.assets / "styled/a/v/views"
        self.plan = self.assets / "blockouts/a/v/build_plan.json"
        self.output = self.assets / "bakes/a/v"
        for directory in (self.views, self.plan.parent, self.output / "turntable", self.assets / "tools"):
            directory.mkdir(parents=True)
        (self.assets / "tools/bake_views.py").write_text("SELFCHECK_THRESHOLD = 0.97\n")
        (self.views / "legacy.png").write_bytes(b"legacy original")
        (self.views / "note.txt").write_bytes(b"leave alone")
        self.plan.write_bytes(b'{ "original": true }\n')
        self.original = self.snapshot()
        self.script = self.root / "fake_bake.py"
        self.script.write_text(FAKE)
        self.env = patch.dict(os.environ, {"FORGE_WORKER_TOKEN": "test-bake", "FORGE_BAKE_CMD": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.mode("ok")
        self.store = ForgeStore(self.root / "store")
        app = FastAPI()
        app.state.forge_store = self.store
        app.include_router(forge_router)
        self.http = TestClient(app)
        self.addCleanup(self.http.close)
        self.client = ApiClient(self.http)
        self.worker = ForgeWorker(self.client, self.assets)

    def mode(self, mode):
        os.environ["FORGE_BAKE_CMD"] = shlex.join([sys.executable, str(self.script), str(self.assets), mode])

    def snapshot(self):
        return ({p.name: p.read_bytes() for p in self.views.iterdir()},
                self.plan.read_bytes() if self.plan.exists() else None)

    def staged(self, turntable=1):
        data = BytesIO()
        Image.new("RGB", (2, 2)).save(data, format="PNG")
        response = self.http.post("/v1/forge/jobs", data={"asset": "a", "variant": "v",
            "params": json.dumps({"turntable": turntable}), "canonical_views": '["front"]'},
            files={"files": ("front.png", data.getvalue(), "image/png")})
        response.raise_for_status()
        job = response.json()
        base = f"/jobs/{job['id']}"
        claim = self.client.request("POST", "/worker/claim", {"stages": ["matching"]})
        lease = claim["lease"]["lease_id"]
        self.client.request("POST", base + "/panels/p0", b"panel", lease=lease)
        self.client.request("POST", base + "/match", {"lease_id": lease, "report": {"panels": [{"panel_id": "p0"}]}})
        self.client.request("POST", base + "/review", {"mode": "submit", "panels": [
            {"panel_id": "p0", "decision": "accept", "view": "front"}]})
        claim = self.client.request("POST", "/worker/claim", {"stages": ["review"]})
        lease = claim["lease"]["lease_id"]
        self.client.request("POST", base + "/staged/views/front.png", b"staged front", lease=lease)
        result = self.client.request("POST", base + "/staged", {"lease_id": lease})
        self.assertEqual(result["state"], "staged")
        return result

    def approve(self, job):
        result = self.client.request("POST", f"/jobs/{job['id']}/approve")
        self.assertEqual(result["state"], "queued_bake")
        return result

    def assert_restored(self, job):
        self.assertEqual(self.snapshot(), self.original)
        backup = self.store._job_path(job["id"]).parent / "views_backup"
        manifest = json.loads((backup / "snapshot.json").read_bytes())
        self.assertTrue(manifest["restored"])
        for name, digest in manifest["files"].items():
            self.assertEqual(hashlib.sha256((backup / name).read_bytes()).hexdigest(), digest)
        with (self.store.root / "locks/a__v.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)

    def test_happy_path_version_metrics_artifact_bytes_and_snapshot(self):
        job = self.staged()
        self.assertIsNone(self.worker.run_next())
        self.approve(job)
        self.assertEqual(self.worker.run_next()["state"], "ready")
        version = self.store.get_version("a", "v", 1)
        self.assertEqual(version["inputs"]["staged_views"], ["front"])
        self.assertEqual(version["metrics"], {
            "part_layers": {"core": 2, "social": 1}, "parts_total": 3,
            "coverage": {"front": 0.8, "palette": 0.2}, "bleed_avoided": 2, "bleed_faces": 0,
            "fallback_split": {"underside": 0.1, "gap": 0.1}, "harmonize_drift": {"front": 0.03},
            "turntable_frames": 1, "selfcheck": {"threshold": 0.97, "views": {"front": 0.99}}})
        for path in self.output.rglob("*"):
            if path.is_file():
                stored = self.store.artifact_file("a", "v", 1, path.relative_to(self.output).as_posix())
                self.assertEqual(hashlib.md5(stored.read_bytes()).hexdigest(), hashlib.md5(path.read_bytes()).hexdigest())
        log = (self.store._job_path(job["id"]).parent / "worker.log").read_text()
        for marker in ("PROJECT front", "SELFCHECK front", "BAKE a_v.glb", "BLENDER exited rc=0"):
            self.assertIn(marker, log)
        self.assert_restored(job)
        next_job = self.approve(self.staged())
        self.worker.run_next()
        self.assertEqual(self.store.get_job(next_job["id"])["version_number"], 2)

    def test_failure_rc_or_missing_marker_restores_and_releases(self):
        for mode in ("fail", "no_marker"):
            with self.subTest(mode=mode):
                self.mode(mode)
                job = self.approve(self.staged())
                with self.assertRaisesRegex(ValueError, "Bake failed"):
                    self.worker.run_next()
                self.assertEqual(self.store.get_job(job["id"])["state"], "failed")
                self.assertEqual(self.store.list_versions(), [])
                self.assert_restored(job)

    def test_stale_log_and_artifact_cannot_complete(self):
        (self.output / "blender.log").write_text("BAKE old.glb\n")
        (self.output / "atlas.png").write_bytes(b"old atlas")
        for mode in ("stale_log", "stale_atlas"):
            with self.subTest(mode=mode):
                self.mode(mode)
                job = self.approve(self.staged())
                with self.assertRaises(ValueError):
                    self.worker.run_next()
                self.assertEqual(self.store.get_job(job["id"])["state"], "failed")
                self.assert_restored(job)

    def test_turntable_zero_omits_harvest_even_when_frames_exist(self):
        job = self.approve(self.staged(turntable=0))
        self.worker.run_next()
        version = self.store.get_version("a", "v", 1)
        self.assertEqual(version["metrics"]["turntable_frames"], 0)
        self.assertFalse((self.store._version_dir("a", "v", 1) / "artifacts/turntable").exists())
        self.assert_restored(job)

    def test_single_flight_blocks_second_claim_and_expired_lease(self):
        first = self.approve(self.staged())
        second = self.approve(self.staged())
        started, proceed = Event(), Event()
        execute = self.worker.execute_bake
        def paused(job, backup):
            started.set()
            if not proceed.wait(10):
                raise RuntimeError("test synchronization timeout")
            return execute(job, backup)
        with patch.object(self.worker, "execute_bake", side_effect=paused), ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.worker.run_next)
            try:
                self.assertTrue(started.wait(10))
                self.assertIsNone(self.client.request("POST", "/worker/claim", {"stages": ["baking"]}))
                self.assertIsNone(ForgeWorker(self.client, self.assets).run_next())
                self.assertEqual(self.store.get_job(second["id"])["state"], "queued_bake")
                active = self.store.get_job(first["id"])
                self.assertEqual(active["state"], "baking")
                saved_lease = dict(active["lease"])
                active["lease"]["lease_expires_at"] = (self.store._now() - timedelta(seconds=1)).isoformat()
                self.store._save_job(active)
                self.assertIsNone(ForgeWorker(self.client, self.assets).run_next())
                active["lease"] = saved_lease
                self.store._save_job(active)
            finally:
                proceed.set()
            self.assertEqual(future.result(timeout=10)["state"], "ready")
        self.assertEqual(self.worker.run_next()["state"], "ready")
        self.assert_restored(second)

    def test_markers_are_posted_before_process_exit(self):
        job = self.approve(self.staged())
        progress = self.worker.progress
        observed = []
        def inspect(record, *markers, **fields):
            if any("PROJECT front" in line for line in markers):
                observed.append(not (self.output / "a_v.glb").exists())
            return progress(record, *markers, **fields)
        with patch.object(self.worker, "progress", side_effect=inspect):
            self.worker.run_next()
        self.assertEqual(observed, [True])
        self.assert_restored(job)

    def test_missing_original_plan_is_removed_after_bake(self):
        self.plan.unlink()
        self.original = self.snapshot()
        job = self.approve(self.staged())
        self.worker.run_next()
        self.assert_restored(job)

    def test_completion_error_restores_and_fails(self):
        job = self.approve(self.staged())
        request = self.client.request
        def fail_complete(method, path, *args, **kwargs):
            if path == "/worker/complete":
                raise OSError("completion unavailable")
            return request(method, path, *args, **kwargs)
        with patch.object(self.client, "request", side_effect=fail_complete), self.assertRaises(OSError):
            self.worker.run_next()
        self.assertEqual(self.store.get_job(job["id"])["state"], "failed")
        self.assert_restored(job)

    def test_symlink_views_rejected_before_mutation(self):
        target = self.root / "protected.png"
        target.write_bytes(b"protected")
        (self.views / "alias.png").symlink_to(target)
        job = self.approve(self.staged())
        with self.assertRaisesRegex(ValueError, "Symlink forbidden"):
            self.worker.run_next()
        self.assertEqual(target.read_bytes(), b"protected")
        self.assertEqual(self.store.get_job(job["id"])["state"], "failed")

    def test_bake_command_defaults_params_and_override(self):
        job = self.staged(turntable=0)
        with patch.dict(os.environ, {"FORGE_BAKE_CMD": "", "FORGE_BAKE_PYTHON": "/custom/python"}):
            command = self.worker.bake_command(job)
        self.assertEqual(command[:2], ["/custom/python", str(self.assets / "tools/bake.py")])
        self.assertNotIn("--turntable", command)
        self.assertNotIn("--view-iou-fail", command)
        self.assertIn("--atlas-tile", command)
        self.assertEqual(self.worker.bake_command(job), shlex.split(os.environ["FORGE_BAKE_CMD"]))

    def test_interrupt_reaps_bake_before_restoration(self):
        job = self.approve(self.staged())
        progress = self.worker.progress
        processes = []
        popen = subprocess.Popen
        def launch(*args, **kwargs):
            process = popen(*args, **kwargs)
            processes.append(process)
            return process
        def interrupt(record, *markers, **fields):
            if any("PROJECT front" in line for line in markers):
                raise KeyboardInterrupt("test interruption")
            return progress(record, *markers, **fields)
        with patch.object(self.worker, "progress", side_effect=interrupt), \
                patch("open_sprite_pipeline.forge_worker.subprocess.Popen", side_effect=launch), \
                self.assertRaises(KeyboardInterrupt):
            self.worker.run_next()
        self.assertIsNotNone(processes[0].poll())
        self.assertEqual(self.store.get_job(job["id"])["state"], "failed")
        self.assert_restored(job)

    def test_abandoned_snapshot_recovered_before_next_job(self):
        old = self.approve(self.staged())
        self.worker.run_next()
        backup = self.store._job_path(old["id"]).parent / "views_backup"
        manifest = json.loads((backup / "snapshot.json").read_bytes())
        manifest["restored"] = False
        (backup / "snapshot.json").write_text(json.dumps(manifest))
        (self.views / "legacy.png").unlink()
        (self.views / "abandoned.png").write_bytes(b"interrupted bake")
        self.plan.write_bytes(b"interrupted plan")
        job = self.approve(self.staged())
        self.worker.run_next()
        self.assert_restored(job)
        self.assertTrue(json.loads((backup / "snapshot.json").read_bytes())["restored"])


if __name__ == "__main__":
    unittest.main()
