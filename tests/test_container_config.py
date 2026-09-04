from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from open_sprite_pipeline.settings import load_config


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
