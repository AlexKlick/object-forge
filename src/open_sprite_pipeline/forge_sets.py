"""Pure scene-compatible manifest parsing, pair planning, and coverage arithmetic."""
from __future__ import annotations

import json
import yaml

from .forge_store import ForgeStore, ForgeStoreError


def _strings(name, value, field):
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ForgeStoreError(f"set {name}: {field} must be a list of strings")
    return list(value)


def _files(name, value, field, maximum):
    names = _strings(name, value, field)
    if len(names) > maximum:
        raise ForgeStoreError(f"set {name}: {field} permits at most {maximum} files")
    for filename in names:
        # Original basenames may contain spaces; they never become disk paths.
        if not filename or "/" in filename or "\\" in filename or ".." in filename or "\x00" in filename:
            raise ForgeStoreError("Invalid path identifier.")
    return names


def _factory_pair(name, item):
    style = item.get("style")
    if "style" in item and type(style) is not bool:
        raise ForgeStoreError(f"set {name}: require style must be boolean")
    source = item.get("source", "spec")
    if source not in ("spec", "generate"):
        raise ForgeStoreError(f"set {name}: source must be spec or generate")
    # spec_synth accepts at most seven inputs (forge_worker.process_generate); the
    # manifest refuses what the worker would fail rather than storing it.
    sources = _files(name, item.get("sources", []), "sources", 7)
    if source == "generate" and not sources:
        raise ForgeStoreError(f"set {name}: generate requires one to seven sources")
    result = {"style": style, "source": source, "sources": sources}
    if "style_refs" in item:
        result["style_refs"] = _files(name, item["style_refs"], "style_refs", 6)
    for key, maximum in (("height_hint", 300), ("floor_height", 10)):
        if key in item:
            result[key] = ForgeStore._positive(item[key], key, maximum)
    return result


def load_manifest(text: str) -> dict:
    # A text manifest has the synthetic source name "set" (no filesystem IO).
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ForgeStoreError(f"cannot read set set: {exc}") from exc
    try:
        result = _parse(raw)
        json.dumps(result, allow_nan=False)
        return result
    except (TypeError, ValueError, OverflowError) as exc:
        if isinstance(exc, ForgeStoreError):
            raise
        raise ForgeStoreError(str(exc)) from exc


def _parse(raw):
    if not isinstance(raw, dict):
        raise ForgeStoreError("set set must be a mapping")

    name = str(raw.get("scene") or raw.get("set") or "set")
    # Set-level default for the unattended lane; a requirement may override it.
    ForgeStore._name(name)
    palette_default = bool(raw.get("palette_only", False))
    requires = raw.get("requires", [])
    if not isinstance(requires, list) or not requires:
        raise ForgeStoreError(f"set {name}: 'requires' must be a non-empty list")

    pairs: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(requires):
        if not isinstance(item, dict):
            raise ForgeStoreError(f"set {name}: requires[{index}] must be a mapping")
        asset = str(item.get("asset", "")).strip()
        if not asset:
            raise ForgeStoreError(f"set {name}: requires[{index}] has no asset")
        variants = item.get("variants", item.get("variant"))
        if isinstance(variants, str):
            variants = [variants]
        if not isinstance(variants, list) or not variants:
            raise ForgeStoreError(f"set {name}: {asset} has no variants")
        for variant in variants:
            variant = str(variant).strip()
            if not variant:
                raise ForgeStoreError(f"set {name}: {asset} has an empty variant")
            if (asset, variant) in seen:
                raise ForgeStoreError(f"set {name}: {asset}/{variant} listed twice")
            ForgeStore._name(asset)
            ForgeStore._name(variant)
            seen.add((asset, variant))
            # Unlike the scene lane (turntable default 0) an absent turntable stays
            # None so the launch keeps the Forge default: the critic needs frames.
            turntable = item.get("turntable")
            pairs.append({"asset": asset, "variant": variant,
                          "turntable": int(turntable) if turntable is not None else None,
                          "palette_only": bool(item.get("palette_only", palette_default)),
                          "selfcheck_min": item.get("selfcheck_min"), **_factory_pair(name, item)})
            ForgeStore.validate_params({key: pairs[-1][key] for key in ("turntable", "selfcheck_min")
                                        if pairs[-1][key] is not None})

    bindings = raw.get("bindings", [])
    if not isinstance(bindings, list):
        raise ForgeStoreError(f"set {name}: 'bindings' must be a list")
    for index, binding in enumerate(bindings):
        if not isinstance(binding, dict):
            raise ForgeStoreError(f"set {name}: bindings[{index}] must be a mapping")
        for field in ("kind", "asset"):
            if not str(binding.get(field, "")).strip():
                raise ForgeStoreError(f"set {name}: bindings[{index}] has no {field}")
        if not isinstance(binding.get("variants", {}), dict):
            raise ForgeStoreError(f"set {name}: bindings[{index}].variants must be a mapping")

    props = raw.get("props", [])
    if not isinstance(props, list):
        raise ForgeStoreError(f"set {name}: 'props' must be a list")
    for index, prop in enumerate(props):
        if not isinstance(prop, dict):
            raise ForgeStoreError(f"set {name}: props[{index}] must be a mapping")
        if not str(prop.get("asset", "")).strip():
            raise ForgeStoreError(f"set {name}: props[{index}] has no asset")
        anchor = str(prop.get("anchor", "tile"))
        if anchor != "tile":
            raise ForgeStoreError(f"set {name}: props[{index}] anchor {anchor!r} unsupported "
                             f"(tile) — props are decoration on an already-bound element")
        if int(prop.get("count", 4)) < 1:
            raise ForgeStoreError(f"set {name}: props[{index}] count must be at least 1")

    for binding in bindings:
        for key in ("kind", "asset"):
            ForgeStore._name(str(binding[key]))
        for key, variant in binding.get("variants", {}).items():
            ForgeStore._name(str(key))
            ForgeStore._name(str(variant))
    for prop in props:
        ForgeStore._name(str(prop["asset"]))
        ForgeStore._name(str(prop.get("variant", "default")))
    # The on-disk pair directory uses a double underscore separator.
    if len({p["asset"] + "__" + p["variant"] for p in pairs}) != len(pairs):
        raise ForgeStoreError(f"set {name}: ambiguous pair directory")
    style = raw.get("style", {})
    if not isinstance(style, dict):
        raise ForgeStoreError(f"set {name}: style must be a mapping")
    board = _files(name, style.get("board", []), "style.board", 6)
    heroes = _strings(name, style.get("hero", []), "style.hero")
    required = {p["asset"] + "/" + p["variant"] for p in pairs}
    for hero in heroes:
        if hero not in required:
            raise ForgeStoreError(f"set {name}: hero {hero} is not required")
    settings = ForgeStore.validate_style({k: v for k, v in style.items() if k not in {"board", "hero"}})
    return {"palette_only": palette_default, "style": {**settings, "board": board, "hero": heroes}, "name": name, "requires": pairs, "bindings": bindings, "props": props,
            "description": str(raw.get("description", ""))}


def pair_plan(manifest: dict, pair: dict) -> dict:
    settings = manifest.get("style", {})
    hero = pair["asset"] + "/" + pair["variant"] in settings.get("hero", [])
    styled = pair.get("style") is True or (pair.get("style") is None and hero)
    style = None
    if styled:
        style = ForgeStore.validate_style({k: v for k, v in settings.items() if k not in {"board", "hero"}} | {"enabled": True})
    return {"intent": "generate" if pair.get("source", "spec") == "generate" else "from_spec",
            "palette_only": bool(pair.get("palette_only", manifest.get("palette_only", False))) and not styled,
            "style": style,
            "refs": list(pair.get("style_refs", settings.get("board", []))) if styled else [],
            "sources": list(pair.get("sources", []))}


def coverage(pairs_status: dict) -> dict:
    counts = {}
    baked = accepted = complete = 0
    for pair in pairs_status.values():
        state = pair.get("state", pair.get("status", "planned"))
        counts[state] = counts.get(state, 0) + 1
        has_version = pair.get("version") is not None
        baked += has_version
        accepted += has_version and bool(pair.get("accepted"))
        complete += has_version and bool(pair.get("views_complete", False))
    requested = len(pairs_status)
    return {"coverage": {"requested_pairs": requested, "baked_pairs": baked,
                         "accepted_pairs": accepted, "view_complete_pairs": complete,
                         "percent": round(100.0 * baked / requested, 1) if requested else 0.0},
            "status_counts": dict(sorted(counts.items()))}


def unbound_kinds(manifest: dict, ready_pairs) -> list[dict]:
    ready = {(p.split("/", 1)[0], p.split("/", 1)[1]) if isinstance(p, str) else tuple(p)
             for p in ready_pairs}
    problems = []
    for binding in manifest["bindings"]:
        asset = str(binding["asset"])
        missing = sorted({str(v) for v in binding.get("variants", {}).values()
                          if (asset, str(v)) not in ready})
        if missing:
            problems.append({"kind": str(binding["kind"]), "asset": asset, "missing_variants": missing})
    return problems


def unplaced_assets(manifest: dict) -> list[str]:
    bound = {str(b["asset"]) for b in manifest["bindings"]}
    bound |= {str(p["asset"]) for p in manifest.get("props", [])}
    return sorted({p["asset"] for p in manifest["requires"]} - bound)
