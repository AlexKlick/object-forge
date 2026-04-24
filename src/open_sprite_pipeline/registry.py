from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .io_utils import load_yaml


@dataclass
class ModelEntry:
    model_id: str
    provider: str
    task: str
    license_type: str
    commercial_status: str
    hero_asset_eligible: bool
    part_aware: bool
    min_vram_gb: int | None
    input_expectations: list[str]
    output_types: list[str]
    notes: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ModelEntry":
        return cls(**payload)

    def is_allowed(self, environment: str) -> bool:
        if environment == "production":
            return self.commercial_status == "allowed"
        return self.commercial_status != "blocked"

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "provider": self.provider,
            "task": self.task,
            "license_type": self.license_type,
            "commercial_status": self.commercial_status,
            "hero_asset_eligible": self.hero_asset_eligible,
            "part_aware": self.part_aware,
            "min_vram_gb": self.min_vram_gb,
            "input_expectations": list(self.input_expectations),
            "output_types": list(self.output_types),
            "notes": self.notes,
        }


class ModelRegistry:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.schema_version = payload["schema_version"]
        self.environment_defaults = payload["environment_defaults"]
        self._models = {
            model_id: ModelEntry.from_dict(entry)
            for model_id, entry in payload["models"].items()
        }

    @classmethod
    def load(cls, path: str) -> "ModelRegistry":
        return cls(load_yaml(path))

    def get(self, model_id: str) -> ModelEntry:
        if model_id not in self._models:
            raise KeyError(model_id)
        return self._models[model_id]

    def all_models(self) -> dict[str, ModelEntry]:
        return dict(self._models)

    def allowed_models(self, environment: str) -> dict[str, ModelEntry]:
        return {
            key: value
            for key, value in self._models.items()
            if value.is_allowed(environment)
        }
