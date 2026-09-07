from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import io
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from open_sprite_pipeline.style_backend import FakeBackend
from open_sprite_pipeline.style_service import StyleSettings, create_style_app, encode_png


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def inputs(size=128):
    init = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(init)
    draw.rectangle((size // 4, size // 8, size * 3 // 4, size * 7 // 8),
                   fill=(180, 60, 25, 255))
    draw.line((size // 4 - 1, size // 8, size // 4 - 1, size * 7 // 8),
              fill=(180, 60, 25, 91))
    # Pin every alpha value, including values below the crop threshold.
    for value in range(256):
        init.putpixel((size // 3 + value % 16, size // 3 + value // 16), (180, 60, 25, value))
    depth = Image.fromarray(np.tile(np.linspace(0, 65535, size, dtype=np.uint16), (size, 1)))
    return init, depth


def payload(init, depth, **changes):
    return {"view": "front", "init_png": encode_png(init), "depth_png": encode_png(depth),
            "refs": [encode_png(Image.new("RGB", (16, 16), "blue"))],
            "prompt": "painted wooden prop", "seeds": [11], "long_side": 512, **changes}


class StyleServiceTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "STYLE_BACKEND": "fake", "STYLE_FRAME_SIZE": "128", "STYLE_MAX_REFS": "6",
            "STYLE_MAX_SEEDS": "6", "STYLE_MAX_IMAGE_BYTES": "25000000",
            "STYLE_IDLE_UNLOAD_S": "180", "STYLE_MIN_FREE_MB": "4500", "STYLE_DEVICE": "cuda:0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.clock = Clock()
        self.backend = FakeBackend()
        self.app = create_style_app(self.backend, clock=self.clock)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.init, self.depth = inputs()
        self.body = payload(self.init, self.depth)

    def post(self, **changes):
        return self.client.post("/v1/style/render", json={**self.body, **changes})

    def success(self, **changes):
        response = self.post(**changes)
        self.assertEqual(response.status_code, 200, response.text[:1000])
        return response.json()

    def test_health_and_status_do_not_load(self):
        with patch.object(self.backend, "load", wraps=self.backend.load) as load:
            self.assertEqual(self.client.get("/healthz").json(), {"ok": True})
            status = self.client.get("/v1/style/status").json()
            self.assertEqual(status["backend"], "fake")
            self.assertFalse(status["loaded"])
            self.assertFalse(status["busy"])
            self.assertIsNone(status["free_mb"])
            self.assertIsNone(status["last_used_s"])
            self.assertEqual(status["renders"], 0)
            self.assertEqual(set(status["models"]), {"base", "controlnet", "ip_adapter"})
            load.assert_not_called()

    def test_validation_matrix(self):
        jpeg = io.BytesIO()
        Image.new("RGB", (8, 8)).save(jpeg, "JPEG")
        cases = [
            ({"init_png": "!!!"}, "init_png: invalid base64"),
            ({"depth_png": "é"}, "depth_png: invalid base64"),
            ({"refs": ["not base64"]}, "refs[0]: invalid base64"),
            ({"init_png": base64.b64encode(b"not an image").decode()}, "invalid or unsafe PNG"),
            ({"depth_png": base64.b64encode(jpeg.getvalue()).decode()}, "image must be a PNG"),
            ({"refs": [base64.b64encode(jpeg.getvalue()).decode()]}, "refs[0]: image must be a PNG"),
            ({"init_png": encode_png(self.init.convert("RGB"))}, "square RGBA"),
            ({"init_png": encode_png(Image.new("RGBA", (64, 128)))}, "square RGBA"),
            ({"init_png": encode_png(Image.new("RGBA", (128, 128)))}, "alpha must be non-empty"),
            ({"depth_png": encode_png(Image.new("L", (64, 64)))}, "must match init size"),
            ({"depth_png": encode_png(Image.new("RGB", (128, 128)))}, "mode L/I/I;16"),
            ({"seeds": []}, "seeds must contain 1 to 6"),
            ({"seeds": list(range(7))}, "seeds must contain 1 to 6"),
            ({"seeds": [True]}, "valid integer"),
            ({"seeds": [1.5]}, "valid integer"),
            ({"seeds": [2**64]}, "seeds must be in torch"),
            ({"refs": [self.body["refs"][0]] * 7}, "refs must contain at most 6"),
            ({"long_side": 448}, "greater than or equal to 512"),
            ({"long_side": 1088}, "less than or equal to 1024"),
            ({"long_side": 520}, "multiple of 64"),
            ({"long_side": 768.0}, "valid integer"),
            ({"strength": 0}, "greater than 0"),
            ({"strength": 1.1}, "less than or equal to 1"),
            ({"strength": 0.01}, "at least one denoising step"),
            ({"guidance": -1}, "greater than or equal to 0"),
            ({"control_scale": -1}, "greater than or equal to 0"),
            ({"ip_scale": -1}, "greater than or equal to 0"),
            ({"steps": 0}, "greater than or equal to 1"),
            ({"steps": 1.5}, "valid integer"),
        ]
        for changes, reason in cases:
            with self.subTest(changes=list(changes), reason=reason):
                response = self.post(**changes)
                self.assertEqual(response.status_code, 422, response.text[:1000])
                self.assertIn(reason, response.text)
        self.assertFalse(self.backend.loaded)

    def test_truncated_png_rejected(self):
        raw = base64.b64decode(self.body["init_png"])
        response = self.post(init_png=base64.b64encode(raw[:-20]).decode())
        self.assertEqual(response.status_code, 422)
        self.assertIn("invalid or unsafe PNG", response.text)

    def test_byte_limit_for_each_image(self):
        with patch.dict(os.environ, {"STYLE_MAX_IMAGE_BYTES": "1000"}):
            app = create_style_app(FakeBackend())
        big = encode_png(Image.fromarray(np.random.default_rng(1).integers(
            0, 256, (128, 128, 3), dtype=np.uint8)))
        with TestClient(app) as client:
            for label in ("init_png", "depth_png", "refs"):
                with self.subTest(label=label):
                    changes = {label: [big] if label == "refs" else big}
                    response = client.post("/v1/style/render", json={**self.body, **changes})
                    self.assertEqual(response.status_code, 422)
                    self.assertIn("exceeds STYLE_MAX_IMAGE_BYTES", response.text)

    def test_success_shape_exact_alpha_and_styled_foreground(self):
        result = self.success(seeds=[11, 22])
        self.assertEqual(set(result), {"candidates", "box", "scale", "prompt_tokens",
                                     "truncated", "model", "peak_mb"})
        self.assertEqual(result["model"], "fake")
        self.assertIsNone(result["peak_mb"])
        self.assertEqual(result["prompt_tokens"], 3)
        self.assertFalse(result["truncated"])
        self.assertEqual(len(result["box"]), 4)
        self.assertGreater(result["scale"], 0)
        alpha = np.asarray(self.init.getchannel("A"))
        for item in result["candidates"]:
            with Image.open(io.BytesIO(base64.b64decode(item["png"]))) as candidate:
                self.assertEqual(candidate.mode, "RGBA")
                self.assertEqual(candidate.size, self.init.size)
                self.assertEqual(candidate.getchannel("A").tobytes(), self.init.getchannel("A").tobytes())
                self.assertTrue(np.all(np.asarray(candidate)[:, :, 3][alpha == 0] == 0))
                self.assertTrue(np.any(np.asarray(candidate)[:, :, :3][alpha > 0]
                                       != np.asarray(self.init)[:, :, :3][alpha > 0]))
            self.assertEqual(max(item["working_size"]), 512)
            self.assertTrue(all(dim % 8 == 0 for dim in item["working_size"]))
            self.assertGreaterEqual(item["seconds"], 0)
        self.assertEqual(self.client.get("/v1/style/status").json()["renders"], 2)

    def test_default_2048_frame(self):
        with patch.dict(os.environ, {"STYLE_FRAME_SIZE": "2048"}):
            app = create_style_app(FakeBackend())
        init, depth = inputs(2048)
        # Keep the object small to exercise upscale/restoration without large CPU crops.
        init = Image.new("RGBA", (2048, 2048))
        init.paste(self.init, (900, 900))
        with TestClient(app) as client:
            response = client.post("/v1/style/render", json=payload(init, depth))
        self.assertEqual(response.status_code, 200, response.text[:1000])
        with Image.open(io.BytesIO(base64.b64decode(response.json()["candidates"][0]["png"]))) as candidate:
            self.assertEqual(candidate.size, (2048, 2048))
            self.assertEqual(candidate.getchannel("A").tobytes(), init.getchannel("A").tobytes())

    def test_deterministic_seeds_and_different_seeds(self):
        first = self.success(seeds=[11, 22, 11])["candidates"]
        again = self.success(seeds=[11])["candidates"][0]
        self.assertEqual(first[0]["png"], first[2]["png"])
        self.assertEqual(first[0]["png"], again["png"])
        self.assertNotEqual(first[0]["png"], first[1]["png"])

    def test_depth_is_normalized_to_l_and_refs_to_rgb(self):
        with patch.object(self.backend, "render", wraps=self.backend.render) as render:
            self.success(refs=[encode_png(Image.new("RGBA", (8, 8), (40, 60, 80, 128)))])
        args = render.call_args.args
        self.assertEqual(args[0].mode, "RGB")
        self.assertEqual(args[1].mode, "L")
        self.assertEqual(args[0].size, args[1].size)
        self.assertTrue(90 < args[1].getpixel((args[1].width // 2, args[1].height // 2)) < 170)
        self.assertEqual(args[2][0].mode, "RGB")

    def test_eight_bit_depth_empty_refs_and_limit_seeds(self):
        self.assertEqual(len(self.success(depth_png=encode_png(self.depth.convert("L")),
                                          refs=[], seeds=list(range(6)))["candidates"]), 6)

    def test_six_refs_are_forwarded(self):
        with patch.object(self.backend, "render", wraps=self.backend.render) as render:
            self.success(refs=self.body["refs"] * 6)
        self.assertEqual(len(render.call_args.args[2]), 6)

    def test_faint_nonempty_alpha_accepted(self):
        init = self.init.copy()
        init.putalpha(self.init.getchannel("A").point(lambda p: 1 if p else 0))
        result = self.success(init_png=encode_png(init))
        with Image.open(io.BytesIO(base64.b64decode(result["candidates"][0]["png"]))) as candidate:
            self.assertEqual(candidate.getchannel("A").tobytes(), init.getchannel("A").tobytes())

    def test_truncated_boundary(self):
        self.assertFalse(self.success(prompt="word " * 77)["truncated"])
        self.assertTrue(self.success(prompt="word " * 78)["truncated"])
        with patch.object(self.backend, "count_tokens", return_value=None):
            self.assertFalse(self.success()["truncated"])

    def test_gpu_guard_and_unavailable_memory_never_load(self):
        self.backend.needs_gpu = True
        with patch.object(self.backend, "load", wraps=self.backend.load) as load:
            for free in (100, None):
                with self.subTest(free=free), patch.object(self.backend, "free_mb", return_value=free):
                    response = self.post()
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json(), {"error": "gpu_busy", "free_mb": free, "needed_mb": 4500})
                    self.assertFalse(self.backend.loaded)
            load.assert_not_called()

    def test_gpu_guard_allows_threshold_and_rechecks_loaded_backend(self):
        self.backend.needs_gpu = True
        with patch.object(self.backend, "free_mb", return_value=4500):
            self.success()
        with patch.object(self.backend, "free_mb", return_value=100):
            self.assertEqual(self.post().status_code, 503)
        self.assertEqual(self.app.state.style.renders, 1)

    def test_fake_skips_memory_guard(self):
        with patch.object(self.backend, "free_mb", side_effect=AssertionError("must not query CUDA")):
            self.success()
            self.assertIsNone(self.client.get("/v1/style/status").json()["free_mb"])

    def test_idle_unload_at_boundary_and_last_used_age(self):
        self.success()
        state = self.app.state.style
        self.clock.now += 179
        self.assertEqual(self.client.get("/v1/style/status").json()["last_used_s"], 179)
        self.assertFalse(state.maybe_unload(self.clock()))
        self.clock.now += 1
        self.assertTrue(state.maybe_unload(self.clock()))
        self.assertFalse(self.backend.loaded)
        self.assertFalse(state.maybe_unload(self.clock()))
        self.success()
        self.assertEqual(state.last_used, self.clock())

    def test_unload_idempotent_and_reload(self):
        self.assertEqual(self.client.post("/v1/style/unload").json(), {"loaded": False})
        self.success()
        self.assertEqual(self.client.post("/v1/style/unload").json(), {"loaded": False})
        self.assertFalse(self.backend.loaded)
        self.success()
        self.assertTrue(self.backend.loaded)

    def test_busy_visible_second_request_waits_and_idle_skips(self):
        entered = threading.Event()
        release = threading.Event()
        second_acquiring = threading.Event()
        state = self.app.state.style
        real_lock = state.lock

        class ObservedLock:
            def __enter__(self):
                if real_lock.locked():
                    second_acquiring.set()
                real_lock.acquire()

            def __exit__(self, *args):
                real_lock.release()

            def locked(self):
                return real_lock.locked()

            def acquire(self, **kwargs):
                return real_lock.acquire(**kwargs)

            def release(self):
                real_lock.release()

        state.lock = ObservedLock()
        original = self.backend.render

        def blocking(*args):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test did not release render")
            return original(*args)

        with patch.object(self.backend, "render", side_effect=blocking), ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.post)
            try:
                self.assertTrue(entered.wait(5))
                self.assertTrue(self.client.get("/v1/style/status").json()["busy"])
                self.assertFalse(state.maybe_unload(10000))
                second = pool.submit(self.post, seeds=[22])
                self.assertTrue(second_acquiring.wait(5))
                self.assertFalse(second.done())
                self.clock.now = 500
            finally:
                release.set()
            self.assertEqual(first.result(10).status_code, 200)
            self.assertEqual(second.result(10).status_code, 200)
        self.assertFalse(self.client.get("/v1/style/status").json()["busy"])
        self.assertEqual(state.last_used, 500)
        self.assertEqual(state.renders, 2)

    def test_render_failure_unloads_and_releases_lock(self):
        with patch.object(self.backend, "render", side_effect=RuntimeError("inference failed")):
            with self.assertRaisesRegex(RuntimeError, "inference failed"):
                self.post()
        self.assertFalse(self.backend.loaded)
        self.assertFalse(self.app.state.style.lock.locked())
        self.success()

    def test_render_logs_one_summary(self):
        with self.assertLogs("uvicorn.error.style", level="INFO") as logs:
            self.success(seeds=[11, 22])
        self.assertEqual(len(logs.output), 1)
        for field in ("view='front'", "seeds=[11, 22]", "working_size=", "seconds=", "peak_mb="):
            self.assertIn(field, logs.output[0])

    def test_lifespan_starts_daemon_and_cleans_up(self):
        with TestClient(self.app) as client:
            self.assertTrue(any(t.name == "style-idle-unload" and t.daemon for t in threading.enumerate()))
            self.assertEqual(client.post("/v1/style/render", json=self.body).status_code, 200)
        self.assertFalse(self.backend.loaded)
        self.assertFalse(any(t.name == "style-idle-unload" for t in threading.enumerate()))

    def test_import_and_fake_render_without_torch_or_diffusers(self):
        code = '''
import sys
sys.path.insert(0, "src")
class RejectGPUImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in ("torch", "diffusers"):
            raise AssertionError("unexpected GPU import: " + fullname)
sys.meta_path.insert(0, RejectGPUImports())
from open_sprite_pipeline.style_service import app, create_style_app
from open_sprite_pipeline.style_backend import FakeBackend, RenderParams
from PIL import Image
backend = FakeBackend()
backend.load()
assert backend.render(Image.new("RGB", (16, 16)), Image.new("L", (16, 16)), [], "a", "", 1, RenderParams()).size == (16, 16)
backend.unload()
assert not backend.loaded
'''
        env = {**os.environ, "STYLE_BACKEND": "diffusers"}
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_settings_read_env_at_factory_time(self):
        with patch.dict(os.environ, {"STYLE_IDLE_UNLOAD_S": "2.5", "STYLE_MAX_SEEDS": "2",
                                     "STYLE_BASE_MODEL": "example/base"}):
            app = create_style_app()
        self.assertEqual(app.state.style.settings.idle_unload_s, 2.5)
        self.assertEqual(app.state.style.settings.max_seeds, 2)
        self.assertEqual(app.state.style.settings.base_model, "example/base")
        self.assertEqual(app.state.style.backend.name, "fake")
        self.assertEqual(StyleSettings().frame_size, 2048)
