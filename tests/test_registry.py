from __future__ import annotations

from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import unittest
from pathlib import Path

from open_sprite_pipeline.policy import classify_model_access
from open_sprite_pipeline.registry import ModelRegistry


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.registry = ModelRegistry.load(str(self.root / "configs" / "model_registry.yaml"))

    def test_allows_trellis2_in_production(self) -> None:
        allowed, reason = classify_model_access(self.registry, "trellis2", "production")
        self.assertTrue(allowed)
        self.assertEqual(reason, "ALLOWED")

    def test_blocks_hunyuan_in_production(self) -> None:
        allowed, reason = classify_model_access(self.registry, "hunyuan3d_2_1", "production")
        self.assertFalse(allowed)
        self.assertEqual(reason, "MODEL_BLOCKED_BY_POLICY")

    def test_unclassified_model(self) -> None:
        allowed, reason = classify_model_access(self.registry, "missing-model", "production")
        self.assertFalse(allowed)
        self.assertEqual(reason, "UNCLASSIFIED_MODEL")


if __name__ == "__main__":
    unittest.main()
