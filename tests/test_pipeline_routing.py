from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from PIL import Image, ImageDraw

from open_sprite_pipeline.extractors import SimpleAlphaExtractor
from open_sprite_pipeline.normalization import ImageNormalizer
from open_sprite_pipeline.pipeline import PipelineOrchestrator
from open_sprite_pipeline.providers import MockProvider
from open_sprite_pipeline.registry import ModelRegistry
from open_sprite_pipeline.renderers import MockRenderer


def _model(provider_id: str, commercial_status: str) -> dict:
    return {
        "model_id": provider_id,
        "provider": "test",
        "task": "test",
        "license_type": "MIT",
        "commercial_status": commercial_status,
        "hero_asset_eligible": provider_id != "mock",
        "part_aware": False,
        "min_vram_gb": 0,
        "input_expectations": [],
        "output_types": [],
        "notes": "",
    }


class FailingProvider:
    provider_id = "trellis2"

    def is_enabled(self) -> bool:
        return True

    def generate(self, *args, **kwargs):
        raise RuntimeError("intentional provider failure")


class ForbiddenProvider:
    provider_id = "trellis"

    def __init__(self) -> None:
        self.called = False

    def is_enabled(self) -> bool:
        return True

    def generate(self, *args, **kwargs):
        self.called = True
        raise AssertionError("policy-blocked provider should not be called")


class PipelineRoutingTests(unittest.TestCase):
    def test_fallback_skips_policy_blocked_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            image_path = tmp / "input.png"
            image = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.rectangle((32, 32, 96, 96), fill=(120, 180, 220, 255))
            image.save(image_path)

            registry = ModelRegistry(
                {
                    "schema_version": 1,
                    "environment_defaults": {},
                    "models": {
                        "trellis2": _model("trellis2", "allowed"),
                        "trellis": _model("trellis", "restricted"),
                        "mock": _model("mock", "allowed"),
                    },
                }
            )
            forbidden = ForbiddenProvider()
            config = {
                "app": {
                    "environment": "production",
                    "artifact_root": str(tmp / ".runs"),
                    "enable_fallback_retry": True,
                    "review_thresholds": {
                        "min_frame_count": 1,
                        "min_coverage_mean": 0.001,
                        "min_sharpness_mean": 0.1,
                    },
                },
                "_resolved_render_preset": {
                    "name": "sprite_4",
                    "views": 4,
                    "resolution": 128,
                },
                "normalization": {
                    "canvas_size": 128,
                    "pad_fraction": 0.08,
                    "background_color": [255, 255, 255],
                    "preserve_alpha": True,
                    "save_rgb_version": True,
                },
            }
            orchestrator = PipelineOrchestrator(
                config=config,
                registry=registry,
                extractor=SimpleAlphaExtractor(),
                normalizer=ImageNormalizer(config["normalization"]),
                providers={
                    "trellis2": FailingProvider(),
                    "trellis": forbidden,
                    "mock": MockProvider({"enabled": True}),
                },
                renderer=MockRenderer({"enabled": True}),
                project_root=tmp,
            )

            logging.disable(logging.CRITICAL)
            try:
                manifest = orchestrator.run(
                    image_path=image_path,
                    prompt="toy robot.",
                    mode="hero",
                )
            finally:
                logging.disable(logging.NOTSET)

            item = manifest["items"][0]
            self.assertEqual(item["status"], "success")
            self.assertEqual(item["route"]["provider_id"], "mock")
            self.assertFalse(forbidden.called)
            self.assertEqual(
                item["route"]["blocked_candidates"]["trellis"],
                "MODEL_BLOCKED_BY_POLICY",
            )
            self.assertTrue(
                any("trellis2: intentional provider failure" in error for error in item["errors"])
            )


if __name__ == "__main__":
    unittest.main()
