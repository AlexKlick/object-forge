from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src")) if str(ROOT / "src") not in sys.path else None

from open_sprite_pipeline.forge_store import DEFAULT_PARAMS, ForgeConflict, ForgeStore, ForgeStoreError


class ForgeStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "forge"
        self.store = ForgeStore(self.root)

    def job(self, **kwargs):
        return self.store.create_job("chair", "oak", canonical_views=["front", "back"], **kwargs)

    def baking(self, **kwargs):
        job = self.job(**kwargs)
        for state in ("matching", "review", "staged", "queued_bake", "baking"):
            job = self.store.set_state(job["id"], state)
        return job

    def test_defaults_and_reload(self):
        job = self.job(params={"atlas_tile": 512})
        self.assertEqual(job["params"], {**DEFAULT_PARAMS, "atlas_tile": 512})
        self.assertEqual(ForgeStore(self.root).get_job(job["id"]), job)
        self.assertEqual(job["intent"], "fresh")
        self.assertIsNone(job["parent_job"])
        self.assertFalse(job["accepted"])

    def test_bad_params_and_identifiers(self):
        for params in ({"iou": float("nan")}, {"margin": -1}, {"atlas_tile": True},
                       {"allow_extra": 1}, {"unknown": 3}, {"turntable": -1}, [],
                       {"selfcheck_min": 1.5}, {"selfcheck_min": "0.96"}):
            with self.subTest(params=params), self.assertRaises(ForgeStoreError):
                self.job(params=params)
        for asset in ("..", "../escape", "/absolute", "bad/name"):
            with self.subTest(asset=asset), self.assertRaises(ForgeStoreError):
                self.store.create_job(asset, "oak")
        with self.assertRaises(ForgeStoreError):
            self.store.create_job("chair", "oak", canonical_views={})
        self.assertEqual(self.store.list_jobs(), [])

    def test_atomic_replace_failure_preserves_original_and_removes_tmp(self):
        job = self.job()
        path = self.store._job_path(job["id"])
        original = path.read_bytes()
        with patch("open_sprite_pipeline.forge_store.os.replace", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                self.store.set_state(job["id"], "matching")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.root.rglob("*.tmp")), [])

    def test_serialization_failure_leaves_no_tmp(self):
        path = self.root / "bad.json"
        with self.assertRaises(ValueError):
            self.store._write_json(path, {"bad": float("nan")})
        self.assertFalse(path.exists())
        self.assertEqual(list(self.root.rglob("*.tmp")), [])

    def test_atomic_partial_write_failure_removes_tmp(self):
        job = self.job()
        with patch("open_sprite_pipeline.forge_store.os.fsync", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.store.set_state(job["id"], "matching")
        self.assertEqual(self.store.get_job(job["id"])["state"], "uploaded")
        self.assertEqual(list(self.root.rglob("*.tmp")), [])

    def test_legal_and_illegal_transitions(self):
        job = self.job()
        with self.assertRaises(ForgeConflict):
            self.store.set_state(job["id"], "ready")
        for state in ("matching", "review", "staged", "queued_bake", "baking", "ready"):
            self.assertEqual(self.store.set_state(job["id"], state)["state"], state)
        for state in ("failed", "uploaded", "ready"):
            with self.assertRaises(ForgeConflict):
                self.store.set_state(job["id"], state)

    def test_failure_from_every_active_state_is_terminal(self):
        for stop in ("uploaded", "matching", "review", "staged", "queued_bake", "baking"):
            job = self.job()
            for state in ("matching", "review", "staged", "queued_bake", "baking"):
                if job["state"] == stop:
                    break
                job = self.store.set_state(job["id"], state)
            job = self.store.set_state(job["id"], "failed", error="oops")
            self.assertEqual(job["error"], "oops")
            with self.assertRaises(ForgeConflict):
                self.store.set_state(job["id"], "uploaded")

    def test_claim_heartbeat_expiry_and_stale_worker(self):
        now = self.store._now()
        job = self.job()
        with patch.object(self.store, "_now", return_value=now):
            first = self.store.claim_job()
        lease = first["lease"]["lease_id"]
        self.assertEqual(first["state"], "matching")
        self.assertIsNone(self.store.claim_job())
        self.assertIsNone(self.store.claim_job("critic"))
        with patch.object(self.store, "_now", return_value=now + timedelta(seconds=100)):
            renewed = self.store.heartbeat(job["id"], lease)
        self.assertGreater(renewed["lease"]["lease_expires_at"], first["lease"]["lease_expires_at"])
        with patch.object(self.store, "_now", return_value=now + timedelta(seconds=401)):
            with self.assertRaises(ForgeConflict):
                self.store.heartbeat(job["id"], lease)
            reclaimed = self.store.claim_job()
            self.assertEqual(reclaimed["id"], job["id"])
            self.assertNotEqual(reclaimed["lease"]["lease_id"], lease)
            with self.assertRaises(ForgeConflict):
                self.store.progress(job["id"], state="review", lease_id=lease)
            finished = self.store.progress(job["id"], state="review", lease_id=reclaimed["lease"]["lease_id"])
            self.assertIsNone(finished["lease"])

    def test_expired_bake_claim_is_reaped_in_same_lane(self):
        job = self.job()
        for state in ("matching", "review", "staged", "queued_bake"):
            self.store.set_state(job["id"], state)
        now = self.store._now()
        with patch.object(self.store, "_now", return_value=now):
            first = self.store.claim_job()
        self.assertEqual(first["state"], "baking")
        with patch.object(self.store, "_now", return_value=now + timedelta(seconds=301)):
            second = self.store.claim_job()
        self.assertEqual(second["state"], "baking")
        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(first["lease"], second["lease"])

    def test_concurrent_claim_has_one_winner(self):
        self.job()
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: self.store.claim_job(), range(8)))
        self.assertEqual(sum(r is not None for r in results), 1)

    def test_ring_buffer_counts_embedded_newlines_and_survives_reload(self):
        job = self.job()
        self.store.append_worker_log(job["id"], [f"line {n}" for n in range(240)])
        ForgeStore(self.root).append_worker_log(job["id"], ["last\ntwo"])
        lines = (self.store._job_path(job["id"]).parent / "worker.log").read_text().splitlines()
        self.assertEqual(len(lines), 200)
        self.assertEqual(lines[0], "line 42")
        self.assertEqual(lines[-2:], ["last", "two"])

    def test_version_numbering_index_and_lineage(self):
        parent = self.baking()
        first = self.store.complete(parent["id"], artifacts={"bake_report.json": {"ok": True}})
        child = self.baking(intent="iterate_views", parent_job=parent["id"], parent_version=1,
                            replacement_views=["front"])
        second = self.store.complete(child["id"], metrics={"iou": 0.95})
        self.assertEqual((first["number"], second["number"]), (1, 2))
        self.assertEqual(second["lineage"], {"parent_version": 1, "root_version": 1})
        self.assertEqual(second["inputs"]["parent_views_inherited"], ["back"])
        self.assertEqual(second["metrics"], {"part_layers": {}, "iou": 0.95})
        index = self.root / "assets/chair/variants/oak/versions.json"
        self.assertEqual([v["number"] for v in json.loads(index.read_text())], [1, 2])
        self.assertEqual(self.store.complete(child["id"]), second)
        self.assertEqual(len(self.store.list_versions()), 2)

    def test_iteration_rejects_mismatched_parent(self):
        parent = self.baking()
        self.store.complete(parent["id"])
        other = self.baking()
        for kwargs in ({"parent_job": other["id"], "parent_version": 1}, {"parent_job": parent["id"]}):
            with self.assertRaises(ForgeStoreError):
                self.job(intent="iterate_params", **kwargs)

    def test_concurrent_version_allocation(self):
        jobs = [self.baking() for _ in range(6)]
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda j: self.store.complete(j["id"]), jobs))
        self.assertEqual(sorted(v["number"] for v in results), list(range(1, 7)))
        self.assertEqual(len(self.store.list_versions()), 6)

    def test_failed_version_publication_does_not_consume_number(self):
        job = self.baking()
        # Seed the derived index before injecting a failure in artifact publication.
        self.store.list_versions()
        with patch("open_sprite_pipeline.forge_store.os.replace", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                self.store.complete(job["id"], artifacts={"model.glb": b"glb"})
        self.assertEqual(self.store.list_versions(), [])
        self.assertEqual(list(self.root.rglob("*.tmp")), [])
        self.assertEqual(list(self.root.rglob(".pending-*")), [])
        self.assertEqual(self.store.complete(job["id"])["number"], 1)

    def test_completion_recovers_after_index_or_job_write_failure(self):
        for failure in ("versions.json", "job.json"):
            with self.subTest(failure=failure):
                job = self.baking()
                self.store.list_versions()
                write = self.store._write_json

                def fail(path, payload):
                    if path.name == failure:
                        raise OSError("interrupted publication")
                    write(path, payload)

                with patch.object(self.store, "_write_json", side_effect=fail):
                    with self.assertRaises(OSError):
                        self.store.complete(job["id"])
                version = self.store.complete(job["id"])
                self.assertEqual(self.store.get_job(job["id"])["state"], "ready")
                self.assertEqual(sum(v["job_id"] == job["id"] for v in self.store.list_versions()), 1)
                self.assertEqual(version["job_id"], job["id"])

    def test_path_confinement_and_symlink_write_protection(self):
        job = self.baking()
        self.store.complete(job["id"], artifacts={"nested/model.glb": b"hello"})
        for name in ("../version.json", "/etc/passwd", "nested/../../version.json", "..\\escape", "."):
            with self.subTest(name=name), self.assertRaises(ForgeStoreError):
                self.store.artifact_file("chair", "oak", 1, name)
        outside = Path(self.tmp.name) / "outside"
        outside.write_text("unchanged")
        link = self.root / "assets/chair/variants/oak/versions/v1/artifacts/link"
        link.symlink_to(outside)
        with self.assertRaises(ForgeStoreError):
            self.store.artifact_file("chair", "oak", 1, "link")
        tmp = self.store._job_path(job["id"]).with_name("job.json.tmp")
        tmp.symlink_to(outside)
        with self.assertRaises(ForgeStoreError):
            self.store.append_note(job_id=job["id"], author="owner", text="note")
        self.assertEqual(outside.read_text(), "unchanged")

    def test_accept_notes_filters_and_deleted_origin_job(self):
        job = self.baking()
        self.store.complete(job["id"])
        self.store.accept_version("chair", "oak", 1)
        self.assertTrue(self.store.get_job(job["id"])["accepted"])
        self.store.append_note(asset="chair", variant="oak", number=1, author="me", text="polish legs")
        self.assertEqual(len(self.store.list_versions(accepted=True, q="polish")), 1)
        self.assertEqual(self.store.list_versions(origin="trellis"), [])
        self.store.delete_job(job["id"])
        self.assertEqual(self.store.list_assets(), [{"asset": "chair", "variant": "oak"}])
        self.assertFalse(self.store.accept_version("chair", "oak", 1, False)["accepted"])

    def test_match_panel_staged_upload_and_job_note_helpers(self):
        job = self.job()
        upload = self.store.record_upload(job["id"], "sheet.png", "image/png", b"image")
        path, mime = self.store.upload_file(job["id"], upload["index"])
        self.assertEqual((path.read_bytes(), mime), (b"image", "image/png"))
        self.store.set_state(job["id"], "matching")
        report = {"panels": [{"panel_id": "p1", "iou": 0.95}]}
        self.store.save_match_report(job["id"], report)
        self.assertEqual(self.store.get_job(job["id"])["match"]["panels"], report["panels"])
        self.store.save_panel(job["id"], "p1", b"png")
        self.assertEqual(self.store.panel_file(job["id"], "p1").read_bytes(), b"png")
        self.assertTrue(self.store.save_staged_view(job["id"], "front", b"view").exists())
        with self.assertRaises(ForgeStoreError):
            self.store.save_staged_view(job["id"], "unknown", b"view")
        noted = self.store.append_note(job_id=job["id"], author="owner", text="check")
        self.assertEqual(noted["notes"][0]["text"], "check")

    def test_review_full_coverage_and_explicit_missing(self):
        job = self.job()
        self.store.set_state(job["id"], "matching")
        self.store.set_state(job["id"], "review")
        panels = [{"panel_id": "p1", "decision": "repin", "view": "front", "iou": 0.7}]
        with self.assertRaises(ForgeStoreError):
            self.store.review(job["id"], "submit", panels, [])
        self.assertEqual(self.store.get_job(job["id"])["match"]["decisions"], [])
        self.assertEqual(self.store.review(job["id"], "draft", panels, [])["state"], "review")
        self.assertEqual(self.store.review(job["id"], "submit", panels, ["back"])["state"], "staged")


if __name__ == "__main__":
    unittest.main()
