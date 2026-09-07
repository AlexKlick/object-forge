"""Lazy model adapter and a CPU-only deterministic styling test double."""
from __future__ import annotations

import gc
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from PIL import Image


@dataclass(frozen=True)
class RenderParams:
    strength: float = 0.62
    guidance: float = 6.5
    control_scale: float = 0.8
    ip_scale: float = 0.6
    steps: int = 28


class StyleBackend(Protocol):
    name: str
    needs_gpu: bool
    loaded: bool

    def load(self) -> None: ...
    def unload(self) -> None: ...
    def render(self, init_rgb: Image.Image, depth_l: Image.Image,
               refs: list[Image.Image], prompt: str, negative: str, seed: int,
               params: RenderParams) -> Image.Image: ...
    def count_tokens(self, prompt: str) -> int | None: ...


class DiffusersBackend:
    name = "diffusers"
    needs_gpu = True

    def __init__(self, base_model: str, controlnet: str, ip_adapter: str,
                 device: str = "cuda:0") -> None:
        self.base_model = base_model
        self.controlnet = controlnet
        self.ip_adapter = ip_adapter
        self.device = device
        self.pipe = None

    @property
    def loaded(self) -> bool:
        return self.pipe is not None

    def load(self) -> None:
        if self.loaded:
            return
        import torch
        from diffusers import ControlNetModel, StableDiffusionControlNetImg2ImgPipeline

        # variant="fp16" on both: the shared cache holds only the fp16 weight
        # files (pulled from the host — the container has no DNS), and without
        # the variant diffusers looks for the full-precision file and fails.
        controlnet = ControlNetModel.from_pretrained(self.controlnet, torch_dtype=torch.float16,
                                                     variant="fp16")
        try:
            self.pipe = StableDiffusionControlNetImg2ImgPipeline.from_pretrained(
                self.base_model, controlnet=controlnet, torch_dtype=torch.float16,
                variant="fp16", safety_checker=None)
            self.pipe.load_ip_adapter(self.ip_adapter, subfolder="models",
                                      weight_name="ip-adapter_sd15.safetensors")
            self.pipe.enable_model_cpu_offload(device=self.device)
            self.pipe.enable_attention_slicing()
            # Bound VAE activation memory at the upper working resolution.
            self.pipe.enable_vae_slicing()
            self.pipe.enable_vae_tiling()
            self.pipe.set_progress_bar_config(disable=True)
        except Exception:
            self.unload()
            raise

    def render(self, init_rgb: Image.Image, depth_l: Image.Image,
               refs: list[Image.Image], prompt: str, negative: str, seed: int,
               params: RenderParams) -> Image.Image:
        import torch

        if self.pipe is None:
            raise RuntimeError("backend is not loaded")
        torch.cuda.reset_peak_memory_stats(self.device)
        self.pipe.set_ip_adapter_scale(params.ip_scale if refs else 0.0)
        # The outer list indexes adapters; the inner list supplies this adapter's
        # reference images. A flat list would require one adapter per reference.
        adapter_images = [refs] if refs else [Image.new("RGB", init_rgb.size, (128, 128, 128))]
        return self.pipe(
            prompt=prompt, negative_prompt=negative, image=init_rgb,
            control_image=depth_l.convert("RGB"), ip_adapter_image=adapter_images,
            generator=torch.Generator(self.device).manual_seed(seed),
            strength=params.strength, guidance_scale=params.guidance,
            controlnet_conditioning_scale=params.control_scale,
            num_inference_steps=params.steps,
        ).images[0].convert("RGB")

    def count_tokens(self, prompt: str) -> int | None:
        if self.pipe is None:
            return None
        return len(self.pipe.tokenizer(prompt, truncation=False, add_special_tokens=True)["input_ids"])

    def unload(self) -> None:
        import torch

        self.pipe = None
        gc.collect()
        if torch.cuda.is_available():
            with torch.cuda.device(self.device):
                torch.cuda.empty_cache()

    def peak_mb(self) -> float | None:
        import torch

        if not torch.cuda.is_available():
            return None
        return torch.cuda.max_memory_allocated(self.device) / (1024 * 1024)

    def free_mb(self) -> float | None:
        """Memory this process could use for a render, in MiB.

        Device-level free memory alone under-reports once a model has run:
        torch's caching allocator keeps its reserved blocks, and the device
        counts them as used even though they are idle and reusable by us. With
        CPU offload the weights leave the GPU between renders, so after the
        first render the device may report only ~2-3 GB free while another
        ~3 GB sits in the cache — and the admission guard would refuse every
        later request as gpu_busy. Count reserved-but-unallocated memory as
        available; memory held by *other* processes (the control lane) is
        still excluded, which is what the guard is for.
        """
        try:
            import torch

            if not torch.cuda.is_available():
                return None
            device_free = torch.cuda.mem_get_info(self.device)[0]
            cached = torch.cuda.memory_reserved(self.device) - torch.cuda.memory_allocated(self.device)
            return (device_free + max(0, cached)) / (1024 * 1024)
        except (ImportError, RuntimeError, AssertionError):
            return None


class FakeBackend:
    name = "fake"
    needs_gpu = False

    def __init__(self) -> None:
        self.loaded = False

    def load(self) -> None:
        self.loaded = True

    def unload(self) -> None:
        self.loaded = False

    def render(self, init_rgb: Image.Image, depth_l: Image.Image,
               refs: list[Image.Image], prompt: str, negative: str, seed: int,
               params: RenderParams) -> Image.Image:
        if not self.loaded:
            raise RuntimeError("backend is not loaded")
        rng = np.random.default_rng(seed % (1 << 64))
        hsv = np.array(init_rgb.convert("HSV"), dtype=np.uint8)
        hsv[:, :, 0] = (hsv[:, :, 0].astype(np.uint16) + int(rng.integers(16, 240))) % 256
        rotated = Image.frombytes("HSV", init_rgb.size, hsv.tobytes()).convert("RGB")
        rgb = np.asarray(rotated).astype(np.int16)
        rgb += rng.integers(-12, 13, size=rgb.shape, dtype=np.int16)
        return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))

    def count_tokens(self, prompt: str) -> int:
        return len(prompt.split())

    def free_mb(self) -> None:
        return None

    def peak_mb(self) -> None:
        return None


def build_backend(name: str | None, settings) -> StyleBackend:
    if name in (None, "diffusers"):
        return DiffusersBackend(settings.base_model, settings.controlnet,
                                settings.ip_adapter, settings.device)
    if name == "fake":
        return FakeBackend()
    raise ValueError(f"unknown STYLE_BACKEND: {name}")
