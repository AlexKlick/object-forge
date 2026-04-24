from __future__ import annotations

from typing import Any

from .domain import NormalizedItem, RouteDecision
from .health import first_reason_code
from .policy import classify_model_access
from .registry import ModelRegistry


def _provider_available(provider: Any) -> tuple[bool, str | None]:
    if hasattr(provider, "preflight"):
        payload = provider.preflight()
        if payload.get("available", False):
            return True, None
        return False, first_reason_code(payload)
    if provider.is_enabled():
        return True, None
    return False, "MODEL_DISABLED"


def choose_provider(
    item: NormalizedItem,
    request: dict[str, Any],
    registry: ModelRegistry,
    providers: dict[str, Any],
    environment: str,
) -> RouteDecision:
    del item
    if request.get("provider_override"):
        candidate_lanes = [request["provider_override"]]
    else:
        mode = request.get("mode", "hero")
        if request.get("parts_hint", 0) and int(request["parts_hint"]) > 1:
            mode = "part_aware"
        candidate_lanes = {
            "hero": ["trellis2", "trellis", "partcrafter", "instantmesh", "triposr", "mock"],
            "draft": ["triposr", "instantmesh", "trellis", "trellis2", "mock"],
            "part_aware": ["partcrafter", "trellis2", "trellis", "instantmesh", "triposr", "mock"],
        }[mode]

    blocked: dict[str, str] = {}
    for provider_id in candidate_lanes:
        allowed, reason = classify_model_access(registry, provider_id, environment)
        if not allowed:
            blocked[provider_id] = reason
            continue
        provider = providers.get(provider_id)
        if provider is None:
            blocked[provider_id] = "PROVIDER_NOT_BUILT"
            continue
        available, blocked_reason = _provider_available(provider)
        if not available:
            blocked[provider_id] = blocked_reason or "MODEL_UNAVAILABLE"
            continue
        why = "provider override" if request.get("provider_override") else f"default lane for {request.get('mode', 'hero')}"
        if request.get("parts_hint", 0) and int(request["parts_hint"]) > 1:
            why = f"{why}; parts_hint={request['parts_hint']}"
        return RouteDecision(
            provider_id=provider_id,
            candidates_considered=list(candidate_lanes),
            reason=why,
            blocked_candidates=blocked,
        )
    raise RuntimeError(
        f"No available allowed providers. Candidates={candidate_lanes} blocked={blocked}"
    )
