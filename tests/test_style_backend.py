from contextlib import nullcontext
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from open_sprite_pipeline.style_backend import DiffusersBackend, FakeBackend, RenderParams, build_backend
from open_sprite_pipeline.style_service import StyleSettings


class DiffusersBackendContractTests(unittest.TestCase):
    """API wiring only: no real torch, CUDA, model downloads, or inference."""
    def setUp(self):
        self.torch = MagicMock()
        self.torch.cuda.is_available.return_value = True
        self.torch.cuda.mem_get_info.return_value = (5000 * 1024**2, 12000 * 1024**2)
        self.torch.cuda.max_memory_allocated.return_value = 3200 * 1024**2
        # torch's caching allocator: 3000 MiB reserved by this process, 400 MiB
        # of it actually allocated — the other 2600 MiB is idle cache the device
        # reports as "used" but which a render can reuse.
        self.torch.cuda.memory_reserved.return_value = 3000 * 1024**2
        self.torch.cuda.memory_allocated.return_value = 400 * 1024**2
        self.torch.cuda.device.side_effect = lambda device: nullcontext()
        self.pipe = MagicMock()
        self.diffusers = SimpleNamespace(
            ControlNetModel=MagicMock(), StableDiffusionControlNetImg2ImgPipeline=MagicMock())
        self.diffusers.StableDiffusionControlNetImg2ImgPipeline.from_pretrained.return_value = self.pipe
        mocked_imports = patch.dict(sys.modules, {"torch": self.torch, "diffusers": self.diffusers})
        mocked_imports.start()
        self.addCleanup(mocked_imports.stop)
        self.backend = DiffusersBackend("base", "depth", "adapter", "cuda:0")
        self.init = Image.new("RGB", (512, 768), (100, 20, 40))
        self.depth = Image.new("L", self.init.size, 127)
        self.pipe.return_value = SimpleNamespace(images=[self.init.copy()])

    def test_lazy_load_configuration_and_idempotence(self):
        self.assertFalse(self.backend.loaded)
        self.diffusers.ControlNetModel.from_pretrained.assert_not_called()
        self.backend.load()
        self.backend.load()
        self.diffusers.ControlNetModel.from_pretrained.assert_called_once_with(
            "depth", torch_dtype=self.torch.float16, variant="fp16")
        self.diffusers.StableDiffusionControlNetImg2ImgPipeline.from_pretrained.assert_called_once_with(
            "base", controlnet=self.diffusers.ControlNetModel.from_pretrained.return_value,
            torch_dtype=self.torch.float16, variant="fp16", safety_checker=None)
        self.pipe.load_ip_adapter.assert_called_once_with(
            "adapter", subfolder="models", weight_name="ip-adapter_sd15.safetensors")
        self.pipe.enable_model_cpu_offload.assert_called_once_with(device="cuda:0")
        self.pipe.enable_attention_slicing.assert_called_once_with()
        self.pipe.vae.enable_slicing.assert_called_once_with()
        self.pipe.vae.enable_tiling.assert_called_once_with()
        self.pipe.set_progress_bar_config.assert_called_once_with(disable=True)
        self.assertTrue(self.backend.loaded)

    def test_render_depth_rgb_params_seed_and_multiple_refs(self):
        self.backend.load()
        refs = [Image.new("RGB", (16, 16), color) for color in ("blue", "green")]
        params = RenderParams(0.7, 7, 0.9, 0.4, 30)
        result = self.backend.render(self.init, self.depth, refs, "wood", "plastic", 22, params)
        self.assertEqual(result.size, self.init.size)
        self.assertEqual(result.mode, "RGB")
        kwargs = self.pipe.call_args.kwargs
        self.assertIs(kwargs["image"], self.init)
        self.assertEqual(kwargs["control_image"].mode, "RGB")
        self.assertEqual(kwargs["control_image"].getpixel((0, 0)), (127, 127, 127))
        self.assertEqual(kwargs["ip_adapter_image"], [refs])
        self.assertEqual(kwargs["prompt"], "wood")
        self.assertEqual(kwargs["negative_prompt"], "plastic")
        self.assertEqual(kwargs["strength"], 0.7)
        self.assertEqual(kwargs["guidance_scale"], 7)
        self.assertEqual(kwargs["controlnet_conditioning_scale"], 0.9)
        self.assertEqual(kwargs["num_inference_steps"], 30)
        self.pipe.set_ip_adapter_scale.assert_called_once_with(0.4)
        self.torch.Generator.assert_called_once_with("cuda:0")
        self.torch.Generator.return_value.manual_seed.assert_called_once_with(22)
        self.torch.cuda.reset_peak_memory_stats.assert_called_once_with("cuda:0")

    def test_empty_refs_disable_adapter_without_dropping_image_input(self):
        self.backend.load()
        self.backend.render(self.init, self.depth, [], "", "", 1, RenderParams())
        self.pipe.set_ip_adapter_scale.assert_called_once_with(0.0)
        refs = self.pipe.call_args.kwargs["ip_adapter_image"]
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].mode, "RGB")
        self.assertEqual(refs[0].getpixel((0, 0)), (128, 128, 128))

    def test_count_tokens_untruncated_and_memory_device(self):
        self.assertIsNone(self.backend.count_tokens("test"))
        self.backend.load()
        self.pipe.tokenizer.return_value = {"input_ids": list(range(99))}
        self.assertEqual(self.backend.count_tokens("test"), 99)
        self.pipe.tokenizer.assert_called_once_with("test", truncation=False, add_special_tokens=True)
        self.assertEqual(self.backend.free_mb(), 5000 + 2600)
        self.assertEqual(self.backend.peak_mb(), 3200)
        self.torch.cuda.mem_get_info.assert_called_once_with("cuda:0")
        self.torch.cuda.memory_reserved.assert_called_once_with("cuda:0")
        self.torch.cuda.memory_allocated.assert_called_once_with("cuda:0")
        self.torch.cuda.max_memory_allocated.assert_called_once_with("cuda:0")
        self.torch.cuda.is_available.return_value = False
        self.assertIsNone(self.backend.free_mb())
        self.assertIsNone(self.backend.peak_mb())

    def test_free_mb_counts_own_idle_cache_but_never_negative_cache(self):
        """After the first render the device shows ~2-3 GB free while this
        process holds ~3 GB of idle allocator cache. Without counting that
        cache the loaded-backend recheck would refuse every later request
        as gpu_busy; memory held by other processes must stay excluded."""
        self.torch.cuda.mem_get_info.return_value = (2500 * 1024**2, 12000 * 1024**2)
        self.torch.cuda.memory_reserved.return_value = 3100 * 1024**2
        self.torch.cuda.memory_allocated.return_value = 100 * 1024**2
        self.assertEqual(self.backend.free_mb(), 2500 + 3000)
        # A transient reserved < allocated reading must not subtract.
        self.torch.cuda.memory_reserved.return_value = 50 * 1024**2
        self.assertEqual(self.backend.free_mb(), 2500)

    def test_unload_releases_pipeline_and_cache(self):
        self.backend.load()
        with patch("open_sprite_pipeline.style_backend.gc.collect") as collect:
            self.backend.unload()
        self.assertFalse(self.backend.loaded)
        collect.assert_called_once_with()
        self.torch.cuda.device.assert_called_once_with("cuda:0")
        self.torch.cuda.empty_cache.assert_called_once_with()

    def test_failed_load_releases_partial_pipeline(self):
        self.pipe.load_ip_adapter.side_effect = RuntimeError("adapter load failed")
        with self.assertRaisesRegex(RuntimeError, "adapter load failed"):
            self.backend.load()
        self.assertFalse(self.backend.loaded)
        self.torch.cuda.empty_cache.assert_called_once_with()

    def test_factory_and_defaults(self):
        self.assertEqual(RenderParams(), RenderParams(0.62, 6.5, 0.8, 0.6, 28))
        self.assertIsInstance(build_backend(None, StyleSettings()), DiffusersBackend)
        self.assertIsInstance(build_backend("diffusers", StyleSettings()), DiffusersBackend)
        self.assertIsInstance(build_backend("fake", StyleSettings()), FakeBackend)
        with self.assertRaises(ValueError):
            build_backend("unknown", StyleSettings())
