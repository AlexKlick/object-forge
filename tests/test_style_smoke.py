import base64
from contextlib import redirect_stdout, redirect_stderr
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("style_smoke", ROOT / "scripts/style-smoke.py")
SMOKE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SMOKE)


class StyleSmokeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="style-smoke-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.render = self.root / "front.png"
        self.ref = self.root / "ref.png"
        self.depth = self.root / "passes/front.depth.png"
        self.depth.parent.mkdir()
        Image.new("RGBA", (8, 8), "red").save(self.render)
        Image.new("RGB", (8, 8), "blue").save(self.ref)
        Image.new("I;16", (8, 8), 32768).save(self.depth)
        self.out = self.root / "out"
        self.args = ["--render", str(self.render), "--ref", str(self.ref), "--out", str(self.out)]

    def test_default_depth_request_files_and_summary(self):
        raw = self.render.read_bytes()
        result = {"candidates": [
            {"seed": seed, "png": base64.b64encode(raw).decode(), "working_size": [512, 768], "seconds": 1}
            for seed in (11, 22, 33)], "peak_mb": 3000, "prompt_tokens": 12, "truncated": False,
            "box": [0, 0, 8, 8]}
        output = io.StringIO()
        with patch.object(SMOKE.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(result).encode())) as post:
            with redirect_stdout(output):
                self.assertEqual(SMOKE.main(self.args), 0)
        request = post.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8056/v1/style/render")
        body = json.loads(request.data)
        self.assertEqual(base64.b64decode(body["depth_png"]), self.depth.read_bytes())
        self.assertEqual(body["seeds"], [11, 22, 33])
        self.assertEqual(body["view"], "front")
        for seed in (11, 22, 33):
            self.assertEqual((self.out / f"front-{seed}.png").read_bytes(), raw)
        summary = json.loads(output.getvalue())
        self.assertEqual(set(summary), {"seconds", "peak_mb", "prompt_tokens", "truncated", "box", "working_size"})
        self.assertEqual(summary["peak_mb"], 3000)

    def test_gpu_busy_prints_body_and_exits_two(self):
        body = b'{"error":"gpu_busy","free_mb":100,"needed_mb":4500}'
        error = urllib.error.HTTPError("http://test", 503, "busy", {}, io.BytesIO(body))
        output = io.StringIO()
        with patch.object(SMOKE.urllib.request, "urlopen", side_effect=error), redirect_stdout(output):
            self.assertEqual(SMOKE.main(self.args), 2)
        self.assertEqual(json.loads(output.getvalue()), json.loads(body))
        self.assertFalse(self.out.exists())

    def test_explicit_options_and_other_http_error(self):
        output = io.StringIO()
        error = urllib.error.HTTPError("http://test", 422, "bad request", {}, io.BytesIO(b'{"detail":"bad"}'))
        args = self.args + ["--depth", str(self.depth), "--ref", str(self.ref),
                            "--url", "http://127.0.0.1:8999/", "--seeds", "-1,42",
                            "--prompt", "painted", "--negative", "photo", "--long-side", "1024"]
        # argparse treats a comma-containing negative list as an option unless joined with '='.
        args[args.index("--seeds"):args.index("--seeds") + 2] = ["--seeds=-1,42"]
        with patch.object(SMOKE.urllib.request, "urlopen", side_effect=error) as post, redirect_stdout(output):
            self.assertEqual(SMOKE.main(args), 1)
        body = json.loads(post.call_args.args[0].data)
        self.assertEqual(body["seeds"], [-1, 42])
        self.assertEqual(body["long_side"], 1024)
        self.assertEqual(body["prompt"], "painted")
        self.assertEqual(body["negative"], "photo")
        self.assertEqual(len(body["refs"]), 2)
        self.assertEqual(post.call_args.args[0].full_url, "http://127.0.0.1:8999/v1/style/render")

    def test_missing_depth_is_local_failure(self):
        self.depth.unlink()
        with patch.object(SMOKE.urllib.request, "urlopen") as post, redirect_stderr(io.StringIO()):
            self.assertEqual(SMOKE.main(self.args), 1)
        post.assert_not_called()
