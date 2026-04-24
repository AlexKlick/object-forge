from __future__ import annotations

import tempfile
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from open_sprite_pipeline.cli import build_orchestrator
from open_sprite_pipeline.io_utils import save_yaml


class MockPipelineTests(unittest.TestCase):
    def test_mock_pipeline_runs_end_to_end(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            image_path = tmp / "input.png"
            image = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.ellipse((96, 96, 416, 416), fill=(80, 160, 240, 255))
            draw.rectangle((220, 180, 300, 340), fill=(220, 80, 80, 255))
            image.save(image_path)

            config = {
                "app": {
                    "environment": "production",
                    "artifact_root": str(tmp / ".runs"),
                    "temp_root": str(tmp / "tmp"),
                    "log_level": "INFO",
                    "render_preset": "sprite_8",
                    "enable_fallback_retry": True,
                    "review_thresholds": {
                        "min_frame_count": 8,
                        "min_coverage_mean": 0.001,
                        "min_sharpness_mean": 0.1,
                    },
                },
                "registry_path": str(repo_root / "configs" / "model_registry.yaml"),
                "render_presets_path": str(repo_root / "configs" / "render_presets.yaml"),
                "extractor": {
                    "kind": "simple_alpha",
                    "allow_fallback": True,
                    "simple_alpha": {"enabled": True},
                    "grounded_sam2": {"enabled": False},
                },
                "normalization": {
                    "canvas_size": 512,
                    "pad_fraction": 0.08,
                    "background_color": [255, 255, 255],
                    "preserve_alpha": True,
                    "save_rgb_version": True,
                },
                "providers": {
                    "trellis2": {"enabled": False},
                    "trellis": {"enabled": False},
                    "partcrafter": {"enabled": False},
                    "triposr": {"enabled": False},
                    "instantmesh": {"enabled": False},
                    "mock": {"enabled": True},
                },
                "renderer": {
                    "kind": "mock",
                    "blender": {"enabled": False, "blender_bin": "blender", "script_path": "scripts/blender_render.py"},
                    "mock": {"enabled": True},
                },
            }
            config_path = tmp / "config.yaml"
            save_yaml(config_path, config)

            orchestrator = build_orchestrator(config_path, mock=True)
            manifest = orchestrator.run(
                image_path=image_path,
                prompt="toy robot.",
                mode="hero",
            )

            self.assertEqual(len(manifest["items"]), 1)
            item = manifest["items"][0]
            self.assertEqual(item["status"], "success")
            self.assertEqual(item["route"]["provider_id"], "mock")
            self.assertEqual(manifest["request"]["mode"], "hero")
            self.assertEqual(manifest["render_preset"]["name"], "sprite_8")
            self.assertTrue(Path(manifest["run_dir"]).exists())
            self.assertTrue(Path(item["rendering"]["sprite_sheet_path"]).exists())
            artifact_paths = {entry["path"] for entry in manifest["artifact_hashes"]}
            self.assertTrue(any(path.endswith(".obj") for path in artifact_paths))
            self.assertTrue(any(path.endswith("sprite_sheet.png") for path in artifact_paths))
            self.assertTrue(all(len(entry["sha256"]) == 64 for entry in manifest["artifact_hashes"]))

            second_manifest = orchestrator.run(
                image_path=image_path,
                prompt="toy robot.",
                mode="hero",
            )
            self.assertNotEqual(manifest["run_id"], second_manifest["run_id"])
            self.assertNotEqual(manifest["run_dir"], second_manifest["run_dir"])
            self.assertTrue(Path(second_manifest["run_dir"]).exists())


if __name__ == "__main__":
    unittest.main()
