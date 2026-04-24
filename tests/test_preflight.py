from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from open_sprite_pipeline.cli import build_preflight_report
from open_sprite_pipeline.io_utils import save_yaml
from open_sprite_pipeline.providers import PartCrafterProvider
from open_sprite_pipeline.renderers import BlenderRenderer
from open_sprite_pipeline.settings import load_config


class PreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]

    def test_default_production_preflight_blocks_mock_fallback(self) -> None:
        report = build_preflight_report(self.root / "configs" / "app.example.yaml", mock=False)

        self.assertFalse(report["ok"])
        self.assertEqual(
            report["providers"]["mock"]["reasons"][0]["code"],
            "MOCK_NOT_ALLOWED",
        )
        self.assertEqual(
            report["renderer"]["reasons"][0]["code"],
            "MOCK_NOT_ALLOWED",
        )

    def test_explicit_mock_preflight_is_available(self) -> None:
        report = build_preflight_report(self.root / "configs" / "app.example.yaml", mock=True)

        self.assertTrue(report["ok"])
        self.assertTrue(report["providers"]["mock"]["available"])
        self.assertTrue(report["renderer"]["available"])

    def test_enabled_provider_reports_missing_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
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
                "registry_path": str(self.root / "configs" / "model_registry.yaml"),
                "render_presets_path": str(self.root / "configs" / "render_presets.yaml"),
                "extractor": {
                    "kind": "simple_alpha",
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
                    "trellis2": {
                        "enabled": True,
                        "python_bin": "/missing/trellis2/python",
                    },
                    "trellis": {"enabled": False},
                    "partcrafter": {"enabled": False},
                    "triposr": {"enabled": False},
                    "instantmesh": {"enabled": False},
                    "mock": {"enabled": True},
                },
                "renderer": {
                    "kind": "mock",
                    "mock": {"enabled": True},
                    "blender": {"enabled": False, "blender_bin": "blender", "script_path": "scripts/blender_render.py"},
                },
            }
            config_path = tmp / "config.yaml"
            save_yaml(config_path, config)

            report = build_preflight_report(config_path, mock=False)

            trellis2_reasons = {
                item["code"] for item in report["providers"]["trellis2"]["reasons"]
            }
            self.assertIn("MISSING_EXECUTABLE", trellis2_reasons)

    def test_bare_bin_values_are_not_rewritten_as_paths(self) -> None:
        config = load_config(self.root / "configs" / "app.example.yaml")

        self.assertEqual(config["renderer"]["blender"]["blender_bin"], "blender")

    def test_blender_script_path_resolves_from_config_file(self) -> None:
        config = load_config(self.root / "configs" / "app.example.yaml")

        self.assertEqual(
            config["renderer"]["blender"]["script_path"],
            str(self.root / "scripts" / "blender_render.py"),
        )

    def test_enabled_partcrafter_reports_missing_repo_dir(self) -> None:
        provider = PartCrafterProvider({"enabled": True, "python_bin": sys.executable})

        codes = {item["code"] for item in provider.preflight()["reasons"]}

        self.assertIn("MISSING_REPO_DIR", codes)

    def test_enabled_blender_reports_missing_script_path(self) -> None:
        renderer = BlenderRenderer(
            {"enabled": True, "blender_bin": sys.executable},
            project_root=self.root,
        )

        codes = {item["code"] for item in renderer.preflight()["reasons"]}

        self.assertIn("MISSING_CONFIG_PATH", codes)


if __name__ == "__main__":
    unittest.main()
