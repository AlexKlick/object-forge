from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


class FakeVector:
    def __init__(self, value):
        self.value = value


class RenderSettings:
    def __init__(self, fail_cycles: bool = False) -> None:
        self.fail_cycles = fail_cycles
        self._engine = None
        self.resolution_x = None
        self.resolution_y = None
        self.image_settings = types.SimpleNamespace(file_format=None)
        self.film_transparent = None

    @property
    def engine(self):
        return self._engine

    @engine.setter
    def engine(self, value):
        if value == "CYCLES" and self.fail_cycles:
            raise RuntimeError("Cycles engine unavailable")
        self._engine = value


class CyclesSettings:
    def __init__(self) -> None:
        object.__setattr__(self, "samples", None)
        object.__setattr__(self, "use_adaptive_sampling", True)
        object.__setattr__(self, "use_denoising", True)
        object.__setattr__(self, "denoiser", "OPENIMAGEDENOISE")
        object.__setattr__(self, "denoiser_writes", [])

    def __setattr__(self, name, value):
        if name == "denoiser":
            self.denoiser_writes.append(value)
            if value == "NONE":
                raise TypeError("enum item 'NONE' not found")
        object.__setattr__(self, name, value)


def load_blender_render_module(scene):
    root = Path(__file__).resolve().parents[1]
    script_path = root / "scripts" / "blender_render.py"
    module_name = "blender_render_under_test"
    old_bpy = sys.modules.get("bpy")
    old_mathutils = sys.modules.get("mathutils")
    sys.modules["bpy"] = types.SimpleNamespace(
        context=types.SimpleNamespace(scene=scene)
    )
    sys.modules["mathutils"] = types.SimpleNamespace(Vector=FakeVector)
    try:
        spec = importlib.util.spec_from_file_location(module_name, script_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Unable to load {script_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if old_bpy is None:
            sys.modules.pop("bpy", None)
        else:
            sys.modules["bpy"] = old_bpy
        if old_mathutils is None:
            sys.modules.pop("mathutils", None)
        else:
            sys.modules["mathutils"] = old_mathutils


class BlenderRenderTests(unittest.TestCase):
    def test_cycles_render_does_not_force_invalid_denoiser_enum(self) -> None:
        cycles = CyclesSettings()
        scene = types.SimpleNamespace(
            render=RenderSettings(),
            cycles=cycles,
        )
        module = load_blender_render_module(scene)

        with tempfile.TemporaryDirectory() as tmpdir:
            module.configure_render(Path(tmpdir), 256, "CYCLES", True)

        self.assertEqual(scene.render.engine, "CYCLES")
        self.assertEqual(cycles.samples, 64)
        self.assertFalse(cycles.use_adaptive_sampling)
        self.assertFalse(cycles.use_denoising)
        self.assertEqual(cycles.denoiser, "OPENIMAGEDENOISE")
        self.assertEqual(cycles.denoiser_writes, [])

    def test_cycles_engine_assignment_failure_falls_back_to_eevee(self) -> None:
        scene = types.SimpleNamespace(
            render=RenderSettings(fail_cycles=True),
            cycles=CyclesSettings(),
        )
        module = load_blender_render_module(scene)

        with tempfile.TemporaryDirectory() as tmpdir:
            module.configure_render(Path(tmpdir), 256, "CYCLES", True)

        self.assertEqual(scene.render.engine, "BLENDER_EEVEE")


if __name__ == "__main__":
    unittest.main()
