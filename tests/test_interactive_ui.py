from __future__ import annotations

from io import BytesIO
from pathlib import Path
import sys
import tempfile
from threading import Event, Lock, Thread
import unittest

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from open_sprite_pipeline.interactive_segmentation import (  # noqa: E402
    ColorFloodSegmenter,
    PromptPoint,
)
from open_sprite_pipeline.generation_queue import GpuGenerationQueue  # noqa: E402
from open_sprite_pipeline.silhouette_mesh import create_silhouette_mesh  # noqa: E402
from open_sprite_pipeline.ui_store import UiAssetStore, UiStoreError  # noqa: E402


def sample_image_bytes() -> bytes:
    image = Image.new("RGB", (96, 72), "white")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((18, 12, 70, 62), radius=8, fill=(205, 46, 52))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class InteractiveSegmentationTests(unittest.TestCase):
    def test_color_flood_selects_connected_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "source.png"
            path.write_bytes(sample_image_bytes())
            result = ColorFloodSegmenter(tolerance=45).segment(
                path,
                [PromptPoint(x=40, y=35, label=1)],
            )

            self.assertEqual(result.mask.shape, (72, 96))
            self.assertTrue(result.mask[35, 40])
            self.assertFalse(result.mask[0, 0])
            self.assertEqual(result.engine, "color_flood_fallback")

    def test_store_persists_mask_and_cutout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = UiAssetStore(tmpdir)
            upload = store.create_upload("object.png", sample_image_bytes())
            result = ColorFloodSegmenter(tolerance=45).segment(
                upload.source_path,
                [PromptPoint(x=40, y=35, label=1)],
            )
            segment = store.save_segment(
                upload.image_id,
                result,
                [PromptPoint(x=40, y=35, label=1)],
                None,
            )

            segment_id = segment["segment_id"]
            self.assertTrue(store.segment_file(upload.image_id, segment_id, "mask").is_file())
            with Image.open(store.segment_cutout(upload.image_id, segment_id)) as cutout:
                self.assertIn("A", cutout.getbands())
            self.assertGreater(segment["coverage"], 0.1)
            self.assertLess(segment["coverage"], 0.7)

    def test_store_rejects_non_image_and_invalid_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = UiAssetStore(tmpdir)
            with self.assertRaises(UiStoreError):
                store.create_upload("notes.txt", b"not an image")
            with self.assertRaises(UiStoreError):
                store.upload_file("../../etc/passwd")

    def test_silhouette_mesh_has_geometry_material_and_texture(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cutout = tmp / "cutout.png"
            image = Image.new("RGBA", (80, 80), (0, 0, 0, 0))
            ImageDraw.Draw(image).ellipse((10, 8, 70, 72), fill=(50, 170, 230, 255))
            image.save(cutout)

            result = create_silhouette_mesh(cutout, tmp / "mesh", grid_size=32)

            self.assertEqual(result["provider_id"], "silhouette_extrusion")
            self.assertGreater(result["cell_count"], 100)
            self.assertGreater(result["face_count"], result["cell_count"] * 2)
            self.assertEqual(len(result["sha256"]), 64)
            model = Path(result["primary_asset_path"])
            self.assertTrue(model.is_file())
            self.assertTrue(Path(result["material_path"]).is_file())
            self.assertTrue(Path(result["texture_path"]).is_file())
            self.assertIn("mtllib model.mtl", model.read_text(encoding="utf-8"))


class GenerationQueueTests(unittest.TestCase):
    def test_real_jobs_run_one_at_a_time_in_fifo_order(self) -> None:
        queue = GpuGenerationQueue(max_waiting=3)
        first_started = Event()
        release_first = Event()
        second_attempting = Event()
        second_started = Event()
        state_lock = Lock()
        order: list[str] = []
        results: list[str] = []
        active = 0
        max_active = 0

        def callback(name: str) -> str:
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
                order.append(f"{name}:start")
            if name == "first":
                first_started.set()
                self.assertTrue(release_first.wait(timeout=2))
            else:
                second_started.set()
            with state_lock:
                order.append(f"{name}:end")
                active -= 1
            return name

        first = Thread(target=lambda: results.append(queue.run(lambda: callback("first"))))

        def run_second() -> None:
            second_attempting.set()
            results.append(queue.run(lambda: callback("second")))

        second = Thread(target=run_second)
        first.start()
        self.assertTrue(first_started.wait(timeout=2))
        second.start()
        self.assertTrue(second_attempting.wait(timeout=2))
        self.assertFalse(second_started.wait(timeout=0.1))
        self.assertEqual(queue.snapshot()["waiting"], 1)
        release_first.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(max_active, 1)
        self.assertEqual(order, ["first:start", "first:end", "second:start", "second:end"])
        self.assertCountEqual(results, ["first", "second"])
        self.assertEqual(queue.snapshot()["waiting"], 0)
        self.assertFalse(queue.snapshot()["busy"])


class InteractiveApiTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient
            from open_sprite_pipeline.api import create_app
        except ImportError as exc:  # pragma: no cover - dependency setup failure
            self.skipTest(str(exc))
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        app = create_app(
            config_path=ROOT / "configs" / "app.example.yaml",
            ui_root=Path(self._tempdir.name) / "ui",
            segmenter=ColorFloodSegmenter(tolerance=45),
            allow_real_generation=False,
        )
        self.client = TestClient(app)

    def test_upload_click_segment_and_generate_mesh(self) -> None:
        index = self.client.get("/")
        self.assertEqual(index.status_code, 200)
        self.assertIn("Object Forge", index.text)

        upload_response = self.client.post(
            "/v1/ui/uploads",
            files={"file": ("object.png", sample_image_bytes(), "image/png")},
        )
        self.assertEqual(upload_response.status_code, 200, upload_response.text)
        upload = upload_response.json()
        self.assertEqual(self.client.get(upload["image_url"]).status_code, 200)

        segment_response = self.client.post(
            f"/v1/ui/uploads/{upload['image_id']}/segments",
            json={"points": [{"x": 40, "y": 35, "label": 1}]},
        )
        self.assertEqual(segment_response.status_code, 200, segment_response.text)
        segment = segment_response.json()
        self.assertEqual(segment["engine"], "color_flood_fallback")
        self.assertEqual(self.client.get(segment["mask_url"]).status_code, 200)
        self.assertEqual(self.client.get(segment["cutout_url"]).status_code, 200)

        generate_response = self.client.post(
            (
                f"/v1/ui/uploads/{upload['image_id']}/segments/"
                f"{segment['segment_id']}/generate"
            ),
            json={"generation": "silhouette", "grid_size": 32, "depth": 0.2},
        )
        self.assertEqual(generate_response.status_code, 200, generate_response.text)
        generated = generate_response.json()
        self.assertEqual(generated["status"], "success")
        self.assertEqual(generated["asset_type"], "obj")
        self.assertTrue(self.client.get(generated["primary_asset_url"]).text.startswith("#"))
        self.assertEqual(self.client.get(generated["material_url"]).status_code, 200)
        self.assertEqual(self.client.get(generated["texture_url"]).status_code, 200)

    def test_real_generation_is_explicitly_refused_when_gpu_lane_is_closed(self) -> None:
        upload = self.client.post(
            "/v1/ui/uploads",
            files={"file": ("object.png", sample_image_bytes(), "image/png")},
        ).json()
        segment = self.client.post(
            f"/v1/ui/uploads/{upload['image_id']}/segments",
            json={"points": [{"x": 40, "y": 35, "label": 1}]},
        ).json()

        response = self.client.post(
            (
                f"/v1/ui/uploads/{upload['image_id']}/segments/"
                f"{segment['segment_id']}/generate"
            ),
            json={"generation": "pipeline", "mock": False, "provider": "trellis2"},
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("dedicated GPU lane", response.json()["detail"])

    def test_status_reports_idle_gpu_queue(self) -> None:
        response = self.client.get("/v1/ui/status")

        self.assertEqual(response.status_code, 200)
        generation = response.json()["generation"]
        self.assertFalse(generation["real_pipeline_busy"])
        self.assertEqual(generation["real_pipeline_queue_depth"], 0)
        self.assertEqual(generation["real_pipeline_queue_limit"], 8)

    def test_upload_rejects_non_image(self) -> None:
        response = self.client.post(
            "/v1/ui/uploads",
            files={"file": ("notes.txt", b"not an image", "text/plain")},
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
