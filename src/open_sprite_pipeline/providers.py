from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from .domain import GeneratedAsset, NormalizedItem
from .health import check_dir, check_executable, check_file, health_payload, reason
from .io_utils import ensure_dir
from .runtime import run_command


class BaseProvider(ABC):
    provider_id: str

    @abstractmethod
    def is_enabled(self) -> bool:
        raise NotImplementedError

    def preflight(self) -> dict[str, Any]:
        return health_payload(
            component="provider",
            component_id=self.provider_id,
            enabled=self.is_enabled(),
        )

    @abstractmethod
    def generate(
        self,
        item: NormalizedItem,
        output_dir: str | Path,
        parts_hint: int | None = None,
    ) -> GeneratedAsset:
        raise NotImplementedError


class MockProvider(BaseProvider):
    provider_id = "mock"

    def __init__(self, config: dict[str, Any], allow_mock: bool = False) -> None:
        self.config = config
        self.allow_mock = allow_mock

    def is_enabled(self) -> bool:
        return bool(self.config.get("enabled", True)) and self.allow_mock

    def preflight(self) -> dict[str, Any]:
        enabled = bool(self.config.get("enabled", True))
        reasons = []
        if not enabled:
            reasons.append(reason("MODEL_DISABLED", "Mock provider is disabled."))
        if not self.allow_mock:
            reasons.append(reason("MOCK_NOT_ALLOWED", "Mock provider requires explicit mock mode."))
        return health_payload(
            component="provider",
            component_id=self.provider_id,
            enabled=enabled,
            reasons=reasons,
        )

    def generate(
        self,
        item: NormalizedItem,
        output_dir: str | Path,
        parts_hint: int | None = None,
    ) -> GeneratedAsset:
        del parts_hint
        if not self.is_enabled():
            raise RuntimeError("MOCK_NOT_ALLOWED: mock provider requires explicit mock mode")
        out_dir = ensure_dir(output_dir)
        obj_path = out_dir / f"{item.item_id}.obj"
        obj_path.write_text(
            "\n".join(
                [
                    "o mock_asset",
                    "v 0.0 0.0 0.0",
                    "v 1.0 0.0 0.0",
                    "v 0.0 1.0 0.0",
                    "f 1 2 3",
                ]
            ),
            encoding="utf-8",
        )
        preview = out_dir / f"{item.item_id}_preview.png"
        image = Image.open(item.normalized_rgba_path).convert("RGBA")
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        draw.text((20, 20), "MOCK 3D", fill=(255, 0, 0, 255))
        image.alpha_composite(overlay)
        image.save(preview)
        return GeneratedAsset(
            provider_id=self.provider_id,
            primary_asset_path=obj_path,
            auxiliary_assets=[],
            preview_paths=[preview],
            metadata={"mock": True},
        )


class Trellis2Provider(BaseProvider):
    provider_id = "trellis2"

    def __init__(self, config: dict[str, Any], project_root: str | Path) -> None:
        self.config = config
        self.project_root = Path(project_root)
        self._env = os.environ.copy()
        self._env["PYTHONPATH"] = str(self.project_root / "vendor" / "TRELLIS.2") + os.pathsep + self._env.get("PYTHONPATH", "")

    def is_enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    def preflight(self) -> dict[str, Any]:
        enabled = bool(self.config.get("enabled", False))
        reasons = []
        if not enabled:
            reasons.append(reason("MODEL_DISABLED", "TRELLIS.2 provider is disabled."))
        else:
            for check in [
                check_executable(self.config.get("python_bin"), "python_bin"),
                check_file(
                    self.project_root / "backend_adapters" / "trellis2_generate.py",
                    "adapter",
                    code="MISSING_ADAPTER",
                ),
            ]:
                if check:
                    reasons.append(check)
        return health_payload("provider", self.provider_id, enabled, reasons)

    def generate(
        self,
        item: NormalizedItem,
        output_dir: str | Path,
        parts_hint: int | None = None,
    ) -> GeneratedAsset:
        del parts_hint
        out_dir = ensure_dir(output_dir)
        adapter = self.project_root / "backend_adapters" / "trellis2_generate.py"
        command = [
            self.config["python_bin"],
            str(adapter),
            "--image",
            str(item.normalized_rgba_path),
            "--output-dir",
            str(out_dir),
            "--model-id",
            str(self.config.get("model_id", "microsoft/TRELLIS.2-4B")),
            "--texture-size",
            str(self.config.get("texture_size", 4096)),
            "--decimation-target",
            str(self.config.get("decimation_target", 1000000)),
        ]
        if self.config.get("remesh", True):
            command.append("--remesh")
        run_command(command, workdir=self.project_root, log_path=out_dir / "provider.log", env=self._env)
        glb = out_dir / "model.glb"
        preview = out_dir / "preview.mp4"
        if not glb.exists():
            raise RuntimeError("TRELLIS.2 adapter did not create model.glb")
        previews = [preview] if preview.exists() else []
        return GeneratedAsset(
            provider_id=self.provider_id,
            primary_asset_path=glb,
            preview_paths=previews,
            metadata={"model_id": self.config.get("model_id")},
        )


class TrellisProvider(BaseProvider):
    provider_id = "trellis"

    def __init__(self, config: dict[str, Any], project_root: str | Path) -> None:
        self.config = config
        self.project_root = Path(project_root)
        self._env = os.environ.copy()
        self._env["PYTHONPATH"] = str(self.project_root / "vendor" / "TRELLIS.2") + os.pathsep + self._env.get("PYTHONPATH", "")

    def is_enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    def preflight(self) -> dict[str, Any]:
        enabled = bool(self.config.get("enabled", False))
        reasons = []
        if not enabled:
            reasons.append(reason("MODEL_DISABLED", "TRELLIS provider is disabled."))
        else:
            for check in [
                check_executable(self.config.get("python_bin"), "python_bin"),
                check_file(
                    self.project_root / "backend_adapters" / "trellis_generate.py",
                    "adapter",
                    code="MISSING_ADAPTER",
                ),
            ]:
                if check:
                    reasons.append(check)
        return health_payload("provider", self.provider_id, enabled, reasons)

    def generate(
        self,
        item: NormalizedItem,
        output_dir: str | Path,
        parts_hint: int | None = None,
    ) -> GeneratedAsset:
        del parts_hint
        out_dir = ensure_dir(output_dir)
        adapter = self.project_root / "backend_adapters" / "trellis_generate.py"
        command = [
            self.config["python_bin"],
            str(adapter),
            "--image",
            str(item.normalized_rgba_path),
            "--output-dir",
            str(out_dir),
            "--model-id",
            str(self.config.get("model_id", "microsoft/TRELLIS-image-large")),
            "--texture-size",
            str(self.config.get("texture_size", 1024)),
            "--simplify",
            str(self.config.get("simplify", 0.95)),
        ]
        run_command(command, workdir=self.project_root, log_path=out_dir / "provider.log", env=self._env)
        glb = out_dir / "model.glb"
        ply = out_dir / "gaussian.ply"
        previews = [path for path in [out_dir / "preview_gs.mp4", out_dir / "preview_mesh.mp4"] if path.exists()]
        aux = [ply] if ply.exists() else []
        if not glb.exists():
            raise RuntimeError("TRELLIS adapter did not create model.glb")
        return GeneratedAsset(
            provider_id=self.provider_id,
            primary_asset_path=glb,
            auxiliary_assets=aux,
            preview_paths=previews,
            metadata={"model_id": self.config.get("model_id")},
        )


class PartCrafterProvider(BaseProvider):
    provider_id = "partcrafter"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def is_enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    def preflight(self) -> dict[str, Any]:
        enabled = bool(self.config.get("enabled", False))
        reasons = []
        repo_dir_value = self.config.get("repo_dir")
        repo_dir = Path(repo_dir_value) if repo_dir_value else None
        if not enabled:
            reasons.append(reason("MODEL_DISABLED", "PartCrafter provider is disabled."))
        else:
            for check in [
                check_executable(self.config.get("python_bin"), "python_bin"),
                check_dir(repo_dir_value, "repo_dir"),
                check_file(
                    repo_dir / "scripts" / "inference_partcrafter.py" if repo_dir else None,
                    "entrypoint",
                ),
            ]:
                if check:
                    reasons.append(check)
        return health_payload("provider", self.provider_id, enabled, reasons)

    def generate(
        self,
        item: NormalizedItem,
        output_dir: str | Path,
        parts_hint: int | None = None,
    ) -> GeneratedAsset:
        out_dir = ensure_dir(output_dir)
        repo_dir = Path(self.config["repo_dir"])
        parts = parts_hint or 4
        command = [
            self.config["python_bin"],
            str(repo_dir / "scripts" / "inference_partcrafter.py"),
            "--image_path",
            str(item.normalized_rgb_path or item.normalized_rgba_path),
            "--num_parts",
            str(parts),
            "--output_dir",
            str(out_dir),
            "--tag",
            item.item_id,
            "--guidance_scale",
            str(self.config.get("guidance_scale", 7.0)),
            "--num_inference_steps",
            str(self.config.get("num_inference_steps", 50)),
        ]
        if self.config.get("use_rmbg", False):
            command.append("--rmbg")
        run_command(command, workdir=repo_dir, log_path=out_dir / "provider.log")
        export_dir = out_dir / item.item_id
        glb = export_dir / "object.glb"
        aux = sorted(export_dir.glob("part_*.glb"))
        previews = [p for p in [export_dir / "rendering.gif", export_dir / "rendering.png"] if p.exists()]
        if not glb.exists():
            raise RuntimeError("PartCrafter did not create object.glb")
        return GeneratedAsset(
            provider_id=self.provider_id,
            primary_asset_path=glb,
            auxiliary_assets=aux,
            preview_paths=previews,
            metadata={"parts_hint": parts},
        )


class TripoSRProvider(BaseProvider):
    provider_id = "triposr"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def is_enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    def preflight(self) -> dict[str, Any]:
        enabled = bool(self.config.get("enabled", False))
        reasons = []
        repo_dir_value = self.config.get("repo_dir")
        repo_dir = Path(repo_dir_value) if repo_dir_value else None
        if not enabled:
            reasons.append(reason("MODEL_DISABLED", "TripoSR provider is disabled."))
        else:
            for check in [
                check_executable(self.config.get("python_bin"), "python_bin"),
                check_dir(repo_dir_value, "repo_dir"),
                check_file(repo_dir / "run.py" if repo_dir else None, "entrypoint"),
            ]:
                if check:
                    reasons.append(check)
        return health_payload("provider", self.provider_id, enabled, reasons)

    def generate(
        self,
        item: NormalizedItem,
        output_dir: str | Path,
        parts_hint: int | None = None,
    ) -> GeneratedAsset:
        del parts_hint
        out_dir = ensure_dir(output_dir)
        repo_dir = Path(self.config["repo_dir"])
        command = [
            self.config["python_bin"],
            str(repo_dir / "run.py"),
            str(item.normalized_rgb_path or item.normalized_rgba_path),
            "--output-dir",
            str(out_dir),
            "--model-save-format",
            "glb",
            "--foreground-ratio",
            str(self.config.get("foreground_ratio", 0.85)),
            "--texture-resolution",
            str(self.config.get("texture_resolution", 2048)),
        ]
        if self.config.get("bake_texture", True):
            command.append("--bake-texture")
        if self.config.get("render_video", True):
            command.append("--render")
        run_command(command, workdir=repo_dir, log_path=out_dir / "provider.log")
        glb = out_dir / "0" / "mesh.glb"
        previews = [p for p in [out_dir / "0" / "render.mp4"] if p.exists()]
        aux = [p for p in [out_dir / "0" / "texture.png"] if p.exists()]
        if not glb.exists():
            raise RuntimeError("TripoSR did not create 0/mesh.glb")
        return GeneratedAsset(
            provider_id=self.provider_id,
            primary_asset_path=glb,
            auxiliary_assets=aux,
            preview_paths=previews,
            metadata={},
        )


class InstantMeshProvider(BaseProvider):
    provider_id = "instantmesh"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def is_enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    def preflight(self) -> dict[str, Any]:
        enabled = bool(self.config.get("enabled", False))
        reasons = []
        repo_dir_value = self.config.get("repo_dir")
        repo_dir = Path(repo_dir_value) if repo_dir_value else None
        if not enabled:
            reasons.append(reason("MODEL_DISABLED", "InstantMesh provider is disabled."))
        else:
            for check in [
                check_executable(self.config.get("python_bin"), "python_bin"),
                check_dir(repo_dir_value, "repo_dir"),
                check_file(repo_dir / "run.py" if repo_dir else None, "entrypoint"),
                check_file(self.config.get("config_path"), "config_path"),
            ]:
                if check:
                    reasons.append(check)
        return health_payload("provider", self.provider_id, enabled, reasons)

    def generate(
        self,
        item: NormalizedItem,
        output_dir: str | Path,
        parts_hint: int | None = None,
    ) -> GeneratedAsset:
        del parts_hint
        out_dir = ensure_dir(output_dir)
        repo_dir = Path(self.config["repo_dir"])
        config_path = self.config["config_path"]
        command = [
            self.config["python_bin"],
            str(repo_dir / "run.py"),
            str(config_path),
            str(item.normalized_rgb_path or item.normalized_rgba_path),
            "--output_path",
            str(out_dir),
        ]
        if self.config.get("export_texmap", True):
            command.append("--export_texmap")
        if self.config.get("save_video", True):
            command.append("--save_video")
        run_command(command, workdir=repo_dir, log_path=out_dir / "provider.log")
        config_name = Path(config_path).stem
        mesh = out_dir / config_name / "meshes" / f"{Path(item.normalized_rgb_path or item.normalized_rgba_path).stem}.obj"
        video = out_dir / config_name / "videos" / f"{Path(item.normalized_rgb_path or item.normalized_rgba_path).stem}.mp4"
        if not mesh.exists():
            candidates = list((out_dir / config_name / "meshes").glob("*.obj"))
            if candidates:
                mesh = candidates[0]
        if not mesh.exists():
            raise RuntimeError("InstantMesh did not create an OBJ mesh")
        previews = [video] if video.exists() else []
        return GeneratedAsset(
            provider_id=self.provider_id,
            primary_asset_path=mesh,
            auxiliary_assets=[],
            preview_paths=previews,
            metadata={"config_path": config_path},
        )


def build_providers(
    config: dict[str, Any],
    project_root: str | Path,
    allow_mock: bool = False,
) -> dict[str, BaseProvider]:
    provider_cfg = config["providers"]
    providers: dict[str, BaseProvider] = {
        "trellis2": Trellis2Provider(provider_cfg["trellis2"], project_root=project_root),
        "trellis": TrellisProvider(provider_cfg["trellis"], project_root=project_root),
        "partcrafter": PartCrafterProvider(provider_cfg["partcrafter"]),
        "triposr": TripoSRProvider(provider_cfg["triposr"]),
        "instantmesh": InstantMeshProvider(provider_cfg["instantmesh"]),
        "mock": MockProvider(provider_cfg["mock"], allow_mock=allow_mock),
    }
    return providers
