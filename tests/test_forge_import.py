"""Gen Ladder GL1: jobless, confined TRELLIS imports with retained Capture sources."""
from concurrent.futures import ThreadPoolExecutor
import errno
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

from fastapi.testclient import TestClient

from open_sprite_pipeline.api import create_app
from open_sprite_pipeline.forge_store import ForgeStore, ForgeStoreError
from open_sprite_pipeline.interactive_segmentation import ColorFloodSegmenter


def glb_bytes(*, padding=0, alpha_mode="OPAQUE", uv=True):
    """An embedded triangle plus optional BIN padding (no model services needed)."""
    blob = struct.pack("<15f", 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 1)
    blob += b"\0" * padding
    blob += b"\0" * (-len(blob) % 4)
    doc = {
        "asset": {"version": "2.0"}, "scene": 0,
        "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
        "materials": [{"alphaMode": alpha_mode}],
        "meshes": [{"primitives": [{"material": 0,
                    "attributes": {"POSITION": 0, **({"TEXCOORD_0": 1} if uv else {})}}]}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3",
                       "min": [0, 0, 0], "max": [1, 1, 0]},
                      {"bufferView": 1, "componentType": 5126, "count": 3, "type": "VEC2"}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 36},
                        {"buffer": 0, "byteOffset": 36, "byteLength": 24}],
        "buffers": [{"byteLength": len(blob)}],
    }
    js = json.dumps(doc).encode()
    js += b" " * (-len(js) % 4)
    chunks = struct.pack("<II", len(js), 0x4E4F534A) + js + struct.pack("<II", len(blob), 0x004E4942) + blob
    return b"glTF" + struct.pack("<II", 2, 12 + len(chunks)) + chunks


class ForgeImportStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="genladder-gl1-store-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.sources = self.root / "runs"
        self.sources.mkdir()
        self.source = self.sources / "mesh.glb"
        self.source.write_bytes(glb_bytes())
        self.store = ForgeStore(self.root / "forge")

    def save(self, **kwargs):
        args = dict(origin="trellis", artifacts=[("model.glb", self.source)],
                    source_roots=[self.sources], metrics={}, inputs={}, dedupe_key="ur-one")
        args.update(kwargs)
        return self.store.import_version("chair", "default", **args)

    def test_link_preserves_source_and_round_trips_version_index(self):
        with patch("open_sprite_pipeline.forge_store.os.link", wraps=os.link) as link:
            summary = self.save()
        link.assert_called_once()
        target = self.store.artifact_file("chair", "default", 1, "model.glb")
        self.assertTrue(self.source.samefile(target))
        self.assertEqual(target.read_bytes(), self.source.read_bytes())
        self.assertIsNone(summary["job_id"])
        self.assertEqual(summary["metrics"]["part_layers"], {})
        self.assertEqual(summary["origin"], "trellis")
        self.assertEqual(summary["lineage"], {"parent_version": None, "root_version": 1})
        reopened = ForgeStore(self.store.root)
        self.assertEqual(reopened.list_versions(), [summary])
        full = reopened.get_version("chair", "default", 1)
        self.assertEqual(reopened._summary(full), summary)
        index = self.store.root / "assets/chair/variants/default/versions.json"
        self.assertEqual(json.loads(index.read_text()), [summary])
        self.assertEqual(self.store.list_jobs(), [])

    def test_second_directory_exdev_falls_back_to_copy2(self):
        with tempfile.TemporaryDirectory(prefix="genladder-gl1-second-source-") as second:
            source = Path(second) / "mesh.glb"
            source.write_bytes(self.source.read_bytes())
            calls = []
            def fail_link(src, dst):
                calls.append("link")
                raise OSError(errno.EXDEV, "Cross-device link")
            real_copy = shutil.copy2
            def copy(src, dst):
                calls.append("copy2")
                return real_copy(src, dst)
            with patch("open_sprite_pipeline.forge_store.os.link", side_effect=fail_link), \
                 patch("open_sprite_pipeline.forge_store.shutil.copy2", side_effect=copy):
                self.save(artifacts=[("model.glb", source)], source_roots=[Path(second)])
            target = self.store.artifact_file("chair", "default", 1, "model.glb")
            self.assertEqual(calls, ["link", "copy2"])
            self.assertFalse(source.samefile(target))
            self.assertEqual(source.read_bytes(), target.read_bytes())

    def test_refuses_outside_source_root(self):
        outside = self.root / "outside.glb"
        outside.write_bytes(self.source.read_bytes())
        with self.assertRaises(ForgeStoreError):
            self.save(artifacts=[("model.glb", outside)])
        self.assertEqual(self.store.list_versions(), [])

    def test_refuses_symlink_and_symlinked_parent(self):
        alias = self.sources / "alias.glb"
        alias.symlink_to(self.source)
        directory = self.sources / "alias-dir"
        directory.symlink_to(self.sources, target_is_directory=True)
        for source in (alias, directory / self.source.name):
            with self.subTest(source=source), self.assertRaises(ForgeStoreError):
                self.save(artifacts=[("model.glb", source)])

    def test_refuses_missing_directory_and_relative_source(self):
        for source in (self.sources / "missing.glb", self.sources, Path("relative.glb")):
            with self.subTest(source=source), self.assertRaises(ForgeStoreError):
                self.save(artifacts=[("model.glb", source)])

    def test_refuses_unsafe_and_colliding_artifact_names(self):
        for names in (["../escape"], ["/absolute"], ["bad\\path"], ["."],
                      ["model.glb", "model.glb"], ["nested", "nested/model.glb"]):
            with self.subTest(names=names), self.assertRaises(ForgeStoreError):
                self.save(artifacts=[(name, self.source) for name in names])
        self.assertEqual(self.store.list_versions(), [])

    def test_dedupe_is_global_and_preserves_summary_after_accept(self):
        self.save()
        self.store.accept_version("chair", "default", 1)
        summary = self.store.list_versions()[0]
        self.source.unlink()
        self.assertEqual(self.save(), summary)
        self.assertEqual(self.store.import_version(
            "other", "variant", origin="trellis", artifacts=[], source_roots=[],
            metrics={}, inputs={}, dedupe_key="ur-one"), summary)
        self.assertEqual(len(self.store.list_versions()), 1)

    def test_concurrent_dedupe_allocates_once(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.save(), range(4)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len(self.store.list_versions()), 1)

    def test_small_ceiling_refuses_without_consuming_number(self):
        with self.assertRaises(ForgeStoreError):
            self.save(max_bytes_per_artifact=8)
        self.assertEqual(self.store.list_versions(), [])
        self.assertEqual(self.save()["number"], 1)

    def test_copy_failure_removes_pending_and_does_not_consume_number(self):
        with patch("open_sprite_pipeline.forge_store.os.link", side_effect=OSError(errno.EXDEV, "cross-device")), \
             patch("open_sprite_pipeline.forge_store.shutil.copy2", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.save()
        self.assertEqual(list(self.store.root.rglob(".pending-*")), [])
        self.assertEqual(self.store.list_versions(), [])
        self.assertEqual(self.save()["number"], 1)

    def test_skipped_critic_verdict_and_rerun_stay_worker_free(self):
        summary = self.save()
        self.assertEqual(summary["critic"], {"status": "skipped"})
        verdict = self.store.get_critic("chair", "default", 1)
        self.assertEqual(verdict, {"status": "skipped", "score": None,
                         "summary": "Imported TRELLIS generation; critic lane does not review imports.",
                         "model": "none", "issues": [], "at": summary["created_at"]})
        self.assertIsNone(self.store.claim_critic())
        self.assertIsNone(self.store.claim_job())
        self.assertEqual(self.store.rerun_critic("chair", "default", 1), verdict)
        self.assertIsNone(self.store.claim_critic())

    def test_claim_critic_skips_pending_null_missing_and_deleted_jobs(self):
        self.save()
        version = self.store.get_version("chair", "default", 1)
        version["critic"] = {"status": "pending"}
        for job in (None, "a" * 32, "missing-field"):
            if job == "missing-field":
                version.pop("job_id", None)
                # Index summaries require the field; exercise a missing value in full record.
                with patch.object(self.store, "get_version", return_value=version):
                    self.assertIsNone(self.store.claim_critic())
                continue
            version["job_id"] = job
            self.store._write_json(self.store._version_dir("chair", "default", 1) / "version.json", version)
            self.assertIsNone(self.store.claim_critic())


class ForgeImportApiTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="genladder-gl1-api-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.artifacts = self.root / "artifacts"
        self.run = self.artifacts / "run_one"
        self.run.mkdir(parents=True)
        self.source = self.run / "model with spaces.glb"
        self.source.write_bytes(glb_bytes())
        self.config = self.root / "config.yaml"
        self.config.write_text(json.dumps({"app": {"artifact_root": str(self.artifacts)}}))
        env = patch.dict(os.environ, {"FORGE_ENABLED": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.app = create_app(config_path=self.config, ui_root=self.root / "ui",
                              segmenter=ColorFloodSegmenter(), allow_real_generation=False)
        self.http = TestClient(self.app)
        self.addCleanup(self.http.close)
        self.store = self.app.state.forge_store
        self.record = {
            "record_key": "ur-one", "status": "completed", "provider": "trellis2", "mode": "hero",
            "prompt": "A chair", "image_id": "image-one", "segment_id": "segment-one",
            "response": {"status": "success", "record_key": "ur-one", "run_id": "run_one",
                         "primary_asset_url": self.url(self.source), "preview_urls": []},
        }
        self.write_record()

    def url(self, path):
        return "/v1/artifacts/" + quote(path.relative_to(self.artifacts).as_posix())

    def write_record(self):
        directory = self.root / "ui/generation_runs"
        directory.mkdir(exist_ok=True)
        (directory / "ur-one.json").write_text(json.dumps(self.record))

    def post(self, **kwargs):
        return self.http.post("/v1/ui/runs/ur-one/library", json={"asset": "chair", "variant": "default", **kwargs})

    def test_import_serves_identical_bytes_and_retains_capture_source(self):
        preview = self.run / "preview.mp4"
        preview.write_bytes(b"preview bytes")
        self.record["response"]["preview_urls"] = [self.url(preview)]
        self.write_record()
        response = self.post()
        self.assertEqual(response.status_code, 201, response.text)
        summary = response.json()
        self.assertEqual(summary["number"], 1)
        base = "/v1/forge/assets/chair/variants/default/versions/1"
        for name, source in (("model.glb", self.source), ("preview.mp4", preview)):
            served = self.http.get(base + "/artifacts/" + name)
            self.assertEqual(served.status_code, 200)
            self.assertEqual(served.content, source.read_bytes())
            self.assertEqual(self.http.get(self.url(source)).content, source.read_bytes())
            self.assertTrue(source.is_file())
        full = self.http.get(base).json()
        self.assertEqual(full["inputs"], {"uploads": [], "staged_views": [], "replacement_views": [],
                         "parent_views_inherited": [], "run": {"record_key": "ur-one", "run_id": "run_one",
                         "image_id": "image-one", "segment_id": "segment-one", "provider": "trellis2",
                         "mode": "hero", "prompt": "A chair"}})
        self.assertEqual(summary["metrics"]["import"]["artifact_bytes"],
                         {"model.glb": self.source.stat().st_size, "preview.mp4": preview.stat().st_size})
        self.assertTrue(summary["metrics"]["glb"]["parsed"])
        self.assertIsNone(summary["metrics"]["glb"]["textured"])
        self.assertEqual(self.http.get(base + "/critic").json()["status"], "skipped")
        self.assertEqual(self.store.list_jobs(), [])

    def test_retry_returns_200_same_summary_even_after_source_removed(self):
        first = self.post()
        self.assertEqual(first.status_code, 201)
        self.source.unlink()
        second = self.post(asset="different")
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(len(self.store.list_versions()), 1)

    def test_concurrent_http_retries_return_one_201(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.post(), range(4)))
        self.assertEqual(sorted(r.status_code for r in results), [200, 200, 200, 201])
        self.assertTrue(all(r.json() == results[0].json() for r in results))

    def test_unknown_record_returns_404(self):
        response = self.http.post("/v1/ui/runs/unknown/library", json={"asset": "a", "variant": "v"})
        self.assertEqual(response.status_code, 404)

    def test_noncompleted_or_missing_primary_returns_422(self):
        for status in ("running", "failed", "completed"):
            with self.subTest(status=status):
                self.record["status"] = status
                if status == "completed":
                    self.record["response"].pop("primary_asset_url")
                self.write_record()
                self.assertEqual(self.post().status_code, 422)
        self.assertEqual(self.store.list_versions(), [])

    def test_unusable_glbs_return_422_without_version_allocation(self):
        for payload in (b"not glb", glb_bytes(alpha_mode="BLEND"), glb_bytes(uv=False)):
            with self.subTest(size=len(payload)):
                self.source.write_bytes(payload)
                response = self.post()
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(self.store.list_versions(), [])
        self.source.write_bytes(glb_bytes())
        self.assertEqual(self.post().json()["number"], 1)

    def test_invalid_target_returns_422_without_version_allocation(self):
        self.assertEqual(self.post(asset="../outside").status_code, 422)
        self.assertEqual(self.store.list_versions(), [])

    def test_unset_forge_returns_503(self):
        with patch.dict(os.environ):
            os.environ.pop("FORGE_ENABLED", None)
            app = create_app(config_path=self.config, ui_root=self.root / "disabled",
                             segmenter=ColorFloodSegmenter(), allow_real_generation=False)
        with TestClient(app) as http:
            response = http.post("/v1/ui/runs/ur-one/library", json={"asset": "a", "variant": "v"})
        self.assertEqual(response.status_code, 503)

    def test_glb_above_32_mib_imports_without_base64(self):
        self.source.write_bytes(glb_bytes(padding=32 * 1024 * 1024))
        self.assertGreater(self.source.stat().st_size, 32 * 1024 * 1024)
        response = self.post()
        self.assertEqual(response.status_code, 201, response.text)
        target = self.store.artifact_file("chair", "default", 1, "model.glb")
        self.assertTrue(target.samefile(self.source))
        self.assertEqual(target.read_bytes(), self.source.read_bytes())

    def test_manifest_preview_and_metadata_fallback(self):
        preview = self.run / "turntable.mp4"
        preview.write_bytes(b"manifest preview")
        for key in ("provider", "mode", "prompt", "image_id", "segment_id"):
            self.record.pop(key)
        self.record["response"].update(image_id="response-image", segment_id="response-segment",
            manifest={"request": {"mode": "draft", "prompt": "From manifest"}, "items": [
                {"generation": {"provider_id": "trellis2-local", "preview_paths": [str(preview)]}}]})
        self.write_record()
        response = self.post()
        self.assertEqual(response.status_code, 201, response.text)
        self.assertIn("preview.mp4", response.json()["artifacts"])
        run = self.store.get_version("chair", "default", 1)["inputs"]["run"]
        self.assertEqual(run["provider"], "trellis2-local")
        self.assertEqual(run["mode"], "draft")
        self.assertEqual(run["prompt"], "From manifest")
        self.assertEqual(run["image_id"], "response-image")
        self.assertEqual(run["segment_id"], "response-segment")

    def test_missing_optional_preview_is_omitted(self):
        self.record["response"]["preview_urls"] = [self.url(self.run / "missing.mp4")]
        self.write_record()
        response = self.post()
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["artifacts"], ["model.glb"])

    def test_artifact_url_rejects_traversal_remote_and_symlinks(self):
        alias = self.run / "alias.glb"
        alias.symlink_to(self.source)
        for url in ("/v1/artifacts/%2e%2e/outside.glb", "/v1/artifacts/%2Fetc/passwd",
                    "https://example.com/v1/artifacts/a.glb", self.url(alias),
                    "/v1/artifacts/run_one/missing.glb"):
            with self.subTest(url=url):
                self.record["response"]["primary_asset_url"] = url
                self.write_record()
                self.assertEqual(self.post().status_code, 422)
        self.assertEqual(self.store.list_versions(), [])

    def test_manifest_symlink_preview_rejected(self):
        preview = self.run / "real.mp4"
        preview.write_bytes(b"preview")
        alias = self.run / "alias.mp4"
        alias.symlink_to(preview)
        self.record["response"]["manifest"] = {"items": [{"generation": {"preview_paths": [str(alias)]}}]}
        self.write_record()
        self.assertEqual(self.post().status_code, 422)
        self.assertEqual(self.store.list_versions(), [])


if __name__ == "__main__":
    unittest.main()
