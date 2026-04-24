from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .extractors import SimpleAlphaExtractor, build_extractor
from .normalization import ImageNormalizer
from .pipeline import PipelineOrchestrator
from .providers import MockProvider, build_providers
from .registry import ModelRegistry
from .renderers import MockRenderer, build_renderer
from .settings import get_render_preset, load_config
from .policy import validate_registry_for_production


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )


def build_orchestrator(config_path: str | Path, mock: bool = False) -> PipelineOrchestrator:
    project_root = Path(__file__).resolve().parents[2]
    config = load_config(config_path)
    config["_resolved_render_preset"] = get_render_preset(config)
    _setup_logging(config["app"].get("log_level", "INFO"))
    registry = ModelRegistry.load(config["registry_path"])

    if mock:
        extractor = SimpleAlphaExtractor()
        providers = build_providers(config, project_root=project_root, allow_mock=True)
        providers["mock"] = MockProvider({"enabled": True}, allow_mock=True)
        renderer = MockRenderer({"enabled": True}, allow_mock=True)
    else:
        extractor = build_extractor(config, project_root=project_root)
        providers = build_providers(config, project_root=project_root, allow_mock=False)
        renderer = build_renderer(config, project_root=project_root, allow_mock=False)

    normalizer = ImageNormalizer(config["normalization"])
    return PipelineOrchestrator(
        config=config,
        registry=registry,
        extractor=extractor,
        normalizer=normalizer,
        providers=providers,
        renderer=renderer,
        project_root=project_root,
    )


def cmd_run(args: argparse.Namespace) -> int:
    orchestrator = build_orchestrator(args.config, mock=args.mock)
    manifest = orchestrator.run(
        image_path=args.image,
        prompt=args.prompt,
        mode=args.mode,
        parts_hint=args.parts_hint,
        provider_override=args.provider,
    )
    print(json.dumps(manifest, indent=2))
    return 0


def cmd_validate_registry(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    registry = ModelRegistry.load(config["registry_path"])
    problems = validate_registry_for_production(registry)
    if problems:
        print(json.dumps({"ok": False, "problems": problems}, indent=2))
        return 1
    print(json.dumps({"ok": True, "model_count": len(registry.all_models())}, indent=2))
    return 0


def build_preflight_report(config_path: str | Path, mock: bool = False) -> dict:
    project_root = Path(__file__).resolve().parents[2]
    config = load_config(config_path)
    registry = ModelRegistry.load(config["registry_path"])
    registry_problems = validate_registry_for_production(registry)

    if mock:
        extractor = SimpleAlphaExtractor()
        providers = build_providers(config, project_root=project_root, allow_mock=True)
        providers["mock"] = MockProvider({"enabled": True}, allow_mock=True)
        renderer = MockRenderer({"enabled": True}, allow_mock=True)
    else:
        extractor = build_extractor(config, project_root=project_root)
        providers = build_providers(config, project_root=project_root, allow_mock=False)
        renderer = build_renderer(config, project_root=project_root, allow_mock=False)

    extractor_health = extractor.preflight()
    provider_health = {
        provider_id: provider.preflight()
        for provider_id, provider in providers.items()
    }
    renderer_health = renderer.preflight()
    provider_available = any(payload["available"] for payload in provider_health.values())
    ok = (
        not registry_problems
        and extractor_health["available"]
        and provider_available
        and renderer_health["available"]
    )
    return {
        "ok": ok,
        "mode": "mock" if mock else "production",
        "registry": {
            "ok": not registry_problems,
            "model_count": len(registry.all_models()),
            "problems": registry_problems,
        },
        "extractor": extractor_health,
        "providers": provider_health,
        "renderer": renderer_health,
    }


def cmd_preflight(args: argparse.Namespace) -> int:
    report = build_preflight_report(args.config, mock=args.mock)
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="open-sprite-pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run the pipeline on one input image.")
    run_parser.add_argument("--config", required=True, help="Path to app config YAML.")
    run_parser.add_argument("--image", required=True, help="Path to input image.")
    run_parser.add_argument("--prompt", default=None, help="Grounding prompt, e.g. 'boot. buckle.'")
    run_parser.add_argument("--mode", default="hero", choices=["hero", "draft", "part_aware"])
    run_parser.add_argument("--parts-hint", type=int, default=None)
    run_parser.add_argument("--provider", default=None, help="Override provider id.")
    run_parser.add_argument("--mock", action="store_true", help="Force the fully local mock path.")
    run_parser.set_defaults(func=cmd_run)

    val_parser = subparsers.add_parser("validate-registry", help="Validate the model registry.")
    val_parser.add_argument("--config", required=True, help="Path to app config YAML.")
    val_parser.set_defaults(func=cmd_validate_registry)

    preflight_parser = subparsers.add_parser(
        "preflight",
        help="Check configured extractor, provider, and renderer availability.",
    )
    preflight_parser.add_argument("--config", required=True, help="Path to app config YAML.")
    preflight_parser.add_argument("--mock", action="store_true", help="Allow mock provider/renderer.")
    preflight_parser.set_defaults(func=cmd_preflight)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
