"""Deterministic Forge decisions. Environment parsing is the only configuration input."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import os


@dataclass(frozen=True)
class Policy:
    mode: str = "off"
    min_iou: float = 0.85
    require_checks: bool = True
    min_critic: int = 70
    accept_on_warn: bool = True
    auto_bake: bool = True

    def __post_init__(self):
        if self.mode not in {"off", "advisory", "enforce"}:
            raise ValueError("FORGE_POLICY must be off, advisory or enforce.")
        if (type(self.min_iou) not in (int, float) or not math.isfinite(self.min_iou)
                or not 0 <= self.min_iou <= 1):
            raise ValueError("FORGE_POLICY_MIN_IOU must be finite and between 0 and 1.")
        if type(self.min_critic) is not int or not 0 <= self.min_critic <= 100:
            raise ValueError("FORGE_POLICY_MIN_CRITIC must be an integer between 0 and 100.")
        for key in ("require_checks", "accept_on_warn", "auto_bake"):
            if type(getattr(self, key)) is not bool:
                raise ValueError(f"FORGE_POLICY_{key.upper()} must be boolean.")

    @classmethod
    def from_env(cls, env=os.environ) -> "Policy":
        def boolean(name, default):
            value = str(env.get(name, default)).strip().lower()
            if value not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
                raise ValueError(f"{name} must be boolean.")
            return value in {"1", "true", "yes", "on"}
        return cls(mode=env.get("FORGE_POLICY", "off"),
                   min_iou=float(env.get("FORGE_POLICY_MIN_IOU", "0.85")),
                   require_checks=boolean("FORGE_POLICY_REQUIRE_CHECKS", "true"),
                   min_critic=int(env.get("FORGE_POLICY_MIN_CRITIC", "70")),
                   accept_on_warn=boolean("FORGE_POLICY_ACCEPT_ON_WARN", "true"),
                   auto_bake=boolean("FORGE_POLICY_AUTO_BAKE", "true"))

    @property
    def thresholds(self) -> dict:
        return asdict(self)


def palette_only(job: dict) -> bool:
    return job.get("intent") == "from_spec" and job.get("generate", {}).get("palette_only") is True


def _number(value, default):
    return value if type(value) in (int, float) and math.isfinite(value) else default


def decide_review(policy: Policy, job: dict, report: dict) -> dict:
    panels = report.get("panels", [])
    chosen, missing, reasons = {}, [], []
    inherited = set(job.get("inputs", {}).get("parent_views_inherited", []))
    for view in job["canonical_views"]:
        styles = [p for p in panels if p.get("source") == "style"
                  and p.get("style_view", p.get("auto_view", p.get("view"))) == view]
        good = [p for p in styles if p.get("metrics", {}).get("pass") is True
                and (not policy.require_checks or p.get("checks", {}).get("pass") is True)]
        candidate = min(good, key=lambda p: (_number(p["metrics"].get("palette_drift"), math.inf),
                                             p["panel_id"]), default=None)
        if candidate is None:
            ordinary = [p for p in panels if p.get("source") != "style"
                        and p.get("auto_view") == view and not p.get("reason")
                        and policy.min_iou <= _number(p.get("iou"), -1) <= 1]
            candidate = min(ordinary, key=lambda p: (-p["iou"], p["panel_id"]), default=None)
        if candidate is not None:
            chosen[candidate["panel_id"]] = view
        elif view not in inherited:
            missing.append(view)
            if styles and all(p.get("metrics", {}).get("pass") is not True for p in styles):
                reason = "all style candidates failed metrics"
            elif styles:
                reason = "all style candidates failed metrics or required checks"
            else:
                reason = f"no candidate ≥ {policy.min_iou:g} IoU"
            reasons.append(f"{view}: {reason}")
    decisions = [{"panel_id": p["panel_id"], "decision": "accept", "view": chosen[p["panel_id"]],
                  "iou": _number(p.get("iou"), 1.0)} if p["panel_id"] in chosen
                 else {"panel_id": p["panel_id"], "decision": "reject"} for p in panels]
    return {"action": "submit" if not missing or palette_only(job) else "escalate",
            "panels": decisions, "views_missing": missing, "reasons": reasons,
            "thresholds": policy.thresholds}


def decide_bake(policy: Policy, job: dict) -> str:
    # Staging already requires a submitted policy or human review.
    # Keep the public string result; the store attaches thresholds to its audit.
    return "approve" if policy.auto_bake else "escalate"


def decide_version(policy: Policy, version: dict) -> dict:
    reasons = []
    for view, metrics in version.get("metrics", {}).get("style", {}).get("views", {}).items():
        if metrics.get("pass") is False:
            reasons.append(f"{view}: style metrics failed")
    critic = version.get("critic", {})
    status = critic.get("status")
    accept = (status == "pass" or (status == "warn" and policy.accept_on_warn
              and _number(critic.get("score"), -1) >= policy.min_critic)
              or (status == "skipped" and version.get("origin") == "trellis"))
    if not accept:
        reasons.append(f"critic {status or 'missing'}: requires pass, eligible warn ≥ {policy.min_critic}, "
                       "or skipped trellis")
    return {"action": "flag" if reasons else "accept", "reasons": reasons,
            "thresholds": policy.thresholds}
