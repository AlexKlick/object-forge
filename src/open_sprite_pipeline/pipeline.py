from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .artifacts import create_run_layout
from .domain import ItemRunResult
from .extractors import BaseExtractor
from .manifest import write_manifest
from .normalization import ImageNormalizer
from .policy import classify_model_access
from .providers import BaseProvider
from .renderers import BaseRenderer
from .router import choose_provider
from .scoring import score_renders


class PipelineOrchestrator:
    def __init__(
        self,
        config: dict[str, Any],
        registry: Any,
        extractor: BaseExtractor,
        normalizer: ImageNormalizer,
        providers: dict[str, BaseProvider],
        renderer: BaseRenderer,
        project_root: str | Path,
    ) -> None:
        self.config = config
        self.registry = registry
        self.extractor = extractor
        self.normalizer = normalizer
        self.providers = providers
        self.renderer = renderer
        self.project_root = Path(project_root)

    def _candidate_block_reason(self, provider_id: str, environment: str) -> str | None:
        allowed, reason = classify_model_access(self.registry, provider_id, environment)
        if not allowed:
            return reason
        provider = self.providers.get(provider_id)
        if provider is None:
            return "PROVIDER_NOT_BUILT"
        if not provider.is_enabled():
            return "MODEL_DISABLED"
        return None

    def run(
        self,
        image_path: str | Path,
        prompt: str | None,
        mode: str = "hero",
        parts_hint: int | None = None,
        provider_override: str | None = None,
    ) -> dict[str, Any]:
        environment = self.config["app"]["environment"]
        layout = create_run_layout(self.config["app"]["artifact_root"], image_path)
        logging.info("Starting run %s", layout.run_id)
        request = {
            "prompt": prompt,
            "mode": mode,
            "parts_hint": parts_hint,
            "provider_override": provider_override,
        }

        detections = self.extractor.extract(
            image_path=image_path,
            prompt=prompt,
            output_dir=layout.extraction_dir,
        )
        items: list[ItemRunResult] = []

        for detection in detections:
            item_errors: list[str] = []
            normalization = self.normalizer.normalize(
                detection=detection,
                output_dir=layout.normalization_dir / detection.item_id,
            )
            decision = choose_provider(
                item=normalization,
                request=request,
                registry=self.registry,
                providers=self.providers,
                environment=environment,
            )

            provider_candidates = [
                decision.provider_id,
                *[
                    candidate
                    for candidate in decision.candidates_considered
                    if candidate != decision.provider_id
                ],
            ]
            generation = None
            rendering = None
            quality = None
            status = "failed"
            route_payload = decision.to_dict()
            blocked_candidates = route_payload.setdefault("blocked_candidates", {})

            for provider_id in provider_candidates:
                block_reason = self._candidate_block_reason(provider_id, environment)
                if block_reason is not None:
                    blocked_candidates[provider_id] = block_reason
                    continue
                provider = self.providers[provider_id]
                try:
                    generation = provider.generate(
                        item=normalization,
                        output_dir=layout.generation_dir / detection.item_id / provider_id,
                        parts_hint=parts_hint,
                    )
                    rendering = self.renderer.render(
                        asset=generation,
                        item=normalization,
                        output_dir=layout.rendering_dir / detection.item_id / provider_id,
                        preset=self.config["_resolved_render_preset"],
                    )
                    quality = score_renders(
                        frame_paths=rendering.frame_paths,
                        thresholds=self.config["app"]["review_thresholds"],
                    )
                    status = "success"
                    route_payload["provider_id"] = provider_id
                    if provider_id != decision.provider_id:
                        route_payload["reason"] = f"{route_payload['reason']}; fallback_to={provider_id}"
                    break
                except Exception as exc:  # noqa: BLE001
                    logging.exception("Provider %s failed for %s", provider_id, detection.item_id)
                    item_errors.append(f"{provider_id}: {exc}")
                    if not self.config["app"].get("enable_fallback_retry", True):
                        break

            items.append(
                ItemRunResult(
                    item_id=detection.item_id,
                    label=detection.label,
                    status=status,
                    extraction=detection.to_dict(),
                    normalization=normalization.to_dict(),
                    route=route_payload,
                    generation=generation.to_dict() if generation else None,
                    rendering=rendering.to_dict() if rendering else None,
                    quality=quality.to_dict() if quality else None,
                    errors=item_errors,
                )
            )

        manifest = write_manifest(
            manifest_path=layout.manifest_path,
            run_id=layout.run_id,
            environment=environment,
            input_image_path=image_path,
            items=items,
            run_dir=layout.run_dir,
            request=request,
            render_preset=self.config["_resolved_render_preset"],
        )
        return manifest
