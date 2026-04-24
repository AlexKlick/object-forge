from __future__ import annotations

from .registry import ModelEntry, ModelRegistry


def classify_model_access(
    registry: ModelRegistry,
    model_id: str,
    environment: str,
) -> tuple[bool, str]:
    try:
        entry = registry.get(model_id)
    except KeyError:
        return False, "UNCLASSIFIED_MODEL"
    if not entry.is_allowed(environment):
        return False, "MODEL_BLOCKED_BY_POLICY"
    return True, "ALLOWED"


def validate_registry_for_production(registry: ModelRegistry) -> list[str]:
    problems: list[str] = []
    for model_id, entry in registry.all_models().items():
        if entry.commercial_status not in {"allowed", "restricted"}:
            problems.append(
                f"{model_id}: unsupported commercial_status={entry.commercial_status}"
            )
        if not entry.license_type:
            problems.append(f"{model_id}: missing license_type")
    return problems
