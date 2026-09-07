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


class StyleServiceTests(unittest.TestCase):
    def setUp(self):
        self.services = yaml.safe_load(
            (ROOT / "deploy/docker-compose.interactive-gpu.yml").read_text())["services"]
        self.service = self.services["object-forge-style"]

    def test_user_loopback_and_only_gpu_one(self):
        self.assertEqual(self.service["user"], "1000:1000")
        self.assertEqual(self.service["ports"], ["127.0.0.1:8056:8056"])
        self.assertEqual(self.service["gpus"], [
            {"driver": "nvidia", "device_ids": ["1"], "capabilities": ["gpu"]}])
        env = self.service["environment"]
        self.assertNotIn("CUDA_VISIBLE_DEVICES", env)
        self.assertNotIn("NVIDIA_VISIBLE_DEVICES", env)
        self.assertEqual(env["STYLE_DEVICE"], "cuda:0")

    def test_cache_without_run_store(self):
        self.assertEqual(self.service["volumes"], ["../.cache/trellis2-container:/cache"])
        self.assertFalse(any("/data/runs" in mount for mount in self.service["volumes"]))
        env = self.service["environment"]
        self.assertEqual(env["HOME"], "/cache")
        self.assertEqual(env["HF_HOME"], "/cache/huggingface")
        self.assertEqual(env["HUGGINGFACE_HUB_CACHE"], "/cache/huggingface/hub")
        self.assertEqual(env["TRITON_HOME"], "/cache/triton")
        self.assertEqual(env["STYLE_MIN_FREE_MB"], "4500")
        self.assertEqual(env["STYLE_IDLE_UNLOAD_S"], "180")
        # Containers here have no DNS; models are pulled from the host and the
        # sidecar must fail fast rather than hang on name resolution.
        self.assertEqual(env["HF_HUB_OFFLINE"], "1")

    def test_main_service_keeps_both_gpus_and_store(self):
        main = self.services["object-forge"]
        self.assertEqual(main["gpus"][0]["device_ids"], ["0", "1"])
        self.assertEqual(main["user"], "1000:1000")
        self.assertIn("../.runs/trellis2-container:/data/runs", main["volumes"])
        self.assertEqual(main["environment"]["CUDA_VISIBLE_DEVICES"], "0,1")
        self.assertEqual(main["environment"]["NVIDIA_VISIBLE_DEVICES"], "0,1")

    def test_style_image_and_healthcheck(self):
        self.assertEqual(self.service["build"],
                         {"context": "..", "dockerfile": "deploy/Dockerfile.style"})
        self.assertEqual(self.service["image"], "open-sprite-pipeline:style-cu124")
        self.assertIn("http://127.0.0.1:8056/healthz", self.service["healthcheck"]["test"][-1])
        dockerfile = (ROOT / "deploy/Dockerfile.style").read_text()
        self.assertIn("diffusers==0.40.0", dockerfile)
        self.assertIn("huggingface-hub>=1.23,<2", dockerfile)
        self.assertIn("FROM open-sprite-pipeline:trellis2-cu124", dockerfile)


if __name__ == "__main__":
    unittest.main()
