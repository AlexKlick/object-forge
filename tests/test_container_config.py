from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import yaml

from open_sprite_pipeline.settings import load_config


class ComposeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        compose = yaml.safe_load(
            (ROOT / "deploy" / "docker-compose.interactive-gpu.yml").read_text())
        self.service = compose["services"]["object-forge"]

    def test_container_runs_as_the_host_user(self) -> None:
        """The API writes the forge store from inside the container and the host
        worker writes it from outside (Blender cannot run in the container). If
        the container reverts to root it silently creates a root-owned
        jobs/<id>/ per job and every bake fails at views_backup, *after*
        reaching queued_bake — so this is worth pinning."""
        self.assertEqual(str(self.service.get("user", "")), "1000:1000")

    def test_store_bind_mount_is_present(self) -> None:
        self.assertIn("../.runs/trellis2-container:/data/runs", self.service["volumes"])
        self.assertEqual(self.service["environment"]["OPEN_SPRITE_UI_ROOT"], "/data/runs/ui")


class ContainerConfigTests(unittest.TestCase):
    def test_container_config_uses_container_paths(self) -> None:
        config = load_config(ROOT / "configs" / "app.container.yaml")

        self.assertEqual(config["app"]["artifact_root"], "/data/runs")
        self.assertEqual(config["app"]["temp_root"], "/data/tmp")
        self.assertEqual(config["providers"]["trellis2"]["python_bin"], "/opt/conda/bin/python")
        self.assertEqual(config["renderer"]["blender"]["blender_bin"], "/usr/local/bin/blender")
        self.assertEqual(config["renderer"]["blender"]["script_path"], "/app/scripts/blender_render.py")
        self.assertFalse(config["providers"]["mock"]["enabled"])
        self.assertTrue(config["providers"]["trellis2"]["enabled"])


if __name__ == "__main__":
    unittest.main()
