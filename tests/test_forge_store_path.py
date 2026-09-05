from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from open_sprite_pipeline.forge_worker import ForgeWorker


class ForgeStorePathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="forge-p6-path-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.assets = self.root / "spike"
        self.host = self.root / "host"
        self.reported = self.root / "container-only"
        self.client = Mock()
        self.remote = [{"number": 1, "asset": "a", "variant": "v", "job_id": "unique-bake"}]
        self.client.request.side_effect = self.request
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def request(self, method, path, body=None):
        self.assertEqual(method, "GET")
        if path == "/status":
            return {"store_root": str(self.reported)}
        if path == "/assets":
            return [{"asset": "a", "variant": "v"}]
        if path.endswith("/versions"):
            return self.remote
        if path.endswith("/staged/views/front.png"):
            return b"staged input"
        self.fail(f"Unexpected request: {path}")

    def local_index(self, value=None):
        index = self.host / "assets/a/variants/v/versions.json"
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text(json.dumps(self.remote if value is None else value, indent=4))

    def worker(self):
        os.environ["FORGE_STORE_ROOT"] = str(self.host)
        return ForgeWorker(self.client, self.assets)

    def test_override_locks_use_host_and_canonical_hash_ignores_json_spacing(self):
        self.local_index()
        worker = self.worker()
        with self.assertNoLogs("open_sprite_pipeline.forge_worker", level="WARNING"):
            with worker.bake_lock({"asset": "a", "variant": "v"}) as root:
                self.assertEqual(root, self.host)
                self.assertTrue((self.host / "locks/a__v.lock").is_file())
        self.assertFalse(self.reported.exists())
        self.assertEqual(worker.store_root(), self.host)
        self.assertEqual(sum(call.args[1] == "/status" for call in self.client.request.call_args_list), 1)

    def test_unset_keeps_api_reported_root_and_no_identity_requests(self):
        worker = ForgeWorker(self.client, self.assets)
        with worker.bake_lock({"asset": "a", "variant": "v"}) as root:
            self.assertEqual(root, self.reported)
        self.assertTrue((self.reported / "locks/a__v.lock").is_file())
        self.client.request.assert_called_once_with("GET", "/status")

    def test_relative_and_empty_override_rejected_before_api(self):
        for value in ("relative/store", ""):
            with self.subTest(value=value), patch.dict(os.environ, {"FORGE_STORE_ROOT": value}):
                with self.assertRaisesRegex(ValueError, "absolute"):
                    ForgeWorker(self.client, self.assets)
        self.client.request.assert_not_called()

    def test_overlap_both_directions_and_symlink_alias_rejected(self):
        self.assets.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(self.assets, target_is_directory=True)
        for path in (self.assets, self.assets / "store", self.root, alias / "store"):
            with self.subTest(path=path), patch.dict(os.environ, {"FORGE_STORE_ROOT": str(path)}):
                with self.assertRaisesRegex(ValueError, "disjoint"):
                    ForgeWorker(self.client, self.assets)

    def test_unset_spike_overlap_still_rejected(self):
        self.reported = self.assets / "store"
        with self.assertRaisesRegex(ValueError, "disjoint"):
            ForgeWorker(self.client, self.assets).store_root()

    def test_override_resolved(self):
        self.host.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(self.host, target_is_directory=True)
        self.local_index()
        with patch.dict(os.environ, {"FORGE_STORE_ROOT": str(alias)}):
            self.assertEqual(ForgeWorker(self.client, self.assets).store_root(), self.host)

    def test_mismatch_warns_and_continues_at_host_root(self):
        self.local_index([{"number": 999}])
        with self.assertLogs("open_sprite_pipeline.forge_worker", level="WARNING") as logs:
            self.assertEqual(self.worker().store_root(), self.host)
        self.assertIn("hash mismatch", logs.output[0])
        self.assertIn("continuing", logs.output[0])
        self.assertFalse(self.reported.exists())

    def test_missing_empty_and_unavailable_indexes_warn_without_creating_store(self):
        with self.assertLogs("open_sprite_pipeline.forge_worker", level="WARNING"):
            self.assertEqual(self.worker().store_root(), self.host)
        self.assertFalse(self.host.exists())
        self.remote = []
        self.local_index([])
        with self.assertLogs("open_sprite_pipeline.forge_worker", level="WARNING") as logs:
            self.worker().store_root()
        self.assertIn("No nonempty version index", logs.output[0])
        self.client.request.side_effect = OSError("API down")
        with self.assertLogs("open_sprite_pipeline.forge_worker", level="WARNING"):
            self.assertEqual(self.worker().store_root(), self.host)

    def test_api_reported_spike_path_is_not_used_locally_with_override(self):
        self.reported = self.assets
        self.local_index()
        self.assertEqual(self.worker().store_root(), self.host)
        self.assertFalse(self.assets.exists())

    def test_snapshot_and_recovery_use_override_too(self):
        self.local_index()
        views = self.assets / "styled/a/v/views"
        views.mkdir(parents=True)
        original = b"original image"
        (views / "original.png").write_bytes(original)
        plan = self.assets / "blockouts/a/v/build_plan.json"
        plan.parent.mkdir(parents=True)
        plan.write_bytes(b"{}")
        worker = self.worker()
        worker.progress = Mock()
        job = {"id": "a" * 32, "asset": "a", "variant": "v",
               "match": {"decisions": [{"view": "front", "decision": "accept"}]}}
        with worker.bake_lock(job) as root:
            with worker.staged_snapshot(job, root) as backup:
                self.assertTrue(backup.is_relative_to(self.host))
                self.assertEqual((views / "front.png").read_bytes(), b"staged input")
        self.assertEqual((views / "original.png").read_bytes(), original)
        manifest = json.loads((backup / "snapshot.json").read_bytes())
        self.assertTrue(manifest["restored"])
        self.assertEqual(manifest["files"]["views/original.png"], hashlib.sha256(original).hexdigest())
        self.assertFalse(self.reported.exists())


if __name__ == "__main__":
    unittest.main()
