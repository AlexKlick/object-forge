from __future__ import annotations

from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import unittest
from pathlib import Path

from open_sprite_pipeline.domain import NormalizedItem
from open_sprite_pipeline.registry import ModelRegistry
from open_sprite_pipeline.router import choose_provider


class DummyProvider:
    def __init__(self, enabled: bool) -> None:
        self._enabled = enabled

    def is_enabled(self) -> bool:
        return self._enabled


class RouterTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.registry = ModelRegistry.load(str(root / "configs" / "model_registry.yaml"))
        self.item = NormalizedItem(
            item_id="item_000",
            label="object",
            normalized_rgba_path=root / "tests" / "data" / "mock_input.png",
            normalized_rgb_path=root / "tests" / "data" / "mock_input.png",
            original_cutout_path=root / "tests" / "data" / "mock_input.png",
            metadata={},
        )

    def test_hero_prefers_trellis2(self) -> None:
        providers = {
            "trellis2": DummyProvider(True),
            "trellis": DummyProvider(True),
            "partcrafter": DummyProvider(True),
            "instantmesh": DummyProvider(True),
            "triposr": DummyProvider(True),
            "mock": DummyProvider(True),
        }
        decision = choose_provider(
            item=self.item,
            request={"mode": "hero"},
            registry=self.registry,
            providers=providers,
            environment="production",
        )
        self.assertEqual(decision.provider_id, "trellis2")

    def test_parts_hint_prefers_partcrafter(self) -> None:
        providers = {
            "trellis2": DummyProvider(True),
            "trellis": DummyProvider(True),
            "partcrafter": DummyProvider(True),
            "instantmesh": DummyProvider(True),
            "triposr": DummyProvider(True),
            "mock": DummyProvider(True),
        }
        decision = choose_provider(
            item=self.item,
            request={"mode": "hero", "parts_hint": 4},
            registry=self.registry,
            providers=providers,
            environment="production",
        )
        self.assertEqual(decision.provider_id, "partcrafter")


if __name__ == "__main__":
    unittest.main()
