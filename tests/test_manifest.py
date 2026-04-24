from __future__ import annotations

import json
import tempfile
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import unittest
from pathlib import Path

from open_sprite_pipeline.domain import ItemRunResult
from open_sprite_pipeline.manifest import write_manifest


class ManifestTests(unittest.TestCase):
    def _assert_manifest_matches_required_schema(self, payload: dict) -> None:
        root = Path(__file__).resolve().parents[1]
        schema = json.loads((root / "schemas" / "manifest.schema.json").read_text())
        for field in schema["required"]:
            self.assertIn(field, payload)
        item_schema = schema["properties"]["items"]["items"]
        for field in item_schema["required"]:
            self.assertIn(field, payload["items"][0])

    def test_manifest_contains_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "input.png"
            image_path.write_bytes(b"abc")
            asset_path = Path(tmpdir) / "asset.obj"
            asset_path.write_text("o asset\n", encoding="utf-8")
            manifest_path = Path(tmpdir) / "manifest.json"
            item = ItemRunResult(
                item_id="item_000",
                label="object",
                status="success",
                extraction={"item_id": "item_000"},
                normalization={"item_id": "item_000"},
                route={"provider_id": "mock"},
                generation={"provider_id": "mock", "primary_asset_path": str(asset_path)},
                rendering={"frame_paths": []},
                quality={"needs_review": False},
                errors=[],
            )
            payload = write_manifest(
                manifest_path=manifest_path,
                run_id="run_123",
                environment="production",
                input_image_path=image_path,
                items=[item],
                run_dir=Path(tmpdir),
                request={"mode": "hero", "prompt": "toy robot."},
                render_preset={"name": "sprite_8", "views": 8},
            )
            self.assertEqual(payload["run_id"], "run_123")
            self.assertEqual(payload["run_dir"], str(Path(tmpdir)))
            self.assertEqual(payload["request"]["mode"], "hero")
            self.assertEqual(payload["render_preset"]["name"], "sprite_8")
            self.assertEqual(len(payload["items"]), 1)
            self.assertEqual(len(payload["artifact_hashes"]), 1)
            self.assertEqual(payload["artifact_hashes"][0]["path"], str(asset_path))
            self.assertEqual(len(payload["artifact_hashes"][0]["sha256"]), 64)
            self.assertTrue(manifest_path.exists())
            self._assert_manifest_matches_required_schema(payload)

    def test_schema_matches_emitted_manifest_shape(self) -> None:
        root = Path(__file__).resolve().parents[1]
        schema = json.loads((root / "schemas" / "manifest.schema.json").read_text())
        top_level_required = set(schema["required"])
        item_required = set(schema["properties"]["items"]["items"]["required"])

        self.assertIn("run_dir", top_level_required)
        self.assertIn("request", top_level_required)
        self.assertIn("render_preset", top_level_required)
        self.assertIn("artifact_hashes", top_level_required)
        self.assertNotIn("artifacts", item_required)
        self.assertTrue(
            {
                "extraction",
                "normalization",
                "route",
                "generation",
                "rendering",
                "quality",
                "errors",
            }.issubset(item_required)
        )


if __name__ == "__main__":
    unittest.main()
