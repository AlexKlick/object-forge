# Architecture

## Goal

Build a clean, inspectable pipeline that turns one input image into:

- isolated item cutouts,
- normalized per-item images,
- generated 3D assets,
- canonical sprite renders,
- machine-readable manifests with provenance and quality signals.

## Pipeline stages

```text
Input Image
  └─> Extraction
       └─> Normalization
            └─> Routing
                 └─> 3D Generation
                      └─> Rendering
                           └─> QA / Scoring
                                └─> Manifest + Review Decision
```

## Separation of concerns

### Core orchestrator

The code under `src/open_sprite_pipeline/` is deliberately lightweight:

- config loading,
- routing,
- policy,
- artifact layout,
- manifests,
- subprocess invocation,
- quality scoring.

It should remain stable even as upstream model repos change.

### Provider environments

Each real backend should be isolated in its own environment. The orchestrator only needs:

- a Python executable,
- a repo directory,
- model/checkpoint paths,
- a deterministic command contract.

## Why subprocess over direct import

Direct imports across every model in one Python environment would quickly become brittle. Heavy 3D repos often pin different CUDA or PyTorch combinations.

The subprocess boundary gives:

- better failure isolation,
- cleaner upgrades,
- easier ops ownership,
- easier rollback when one model stack breaks.

## Routing policy

The default router favors:

- **hero** → `trellis2`, then `trellis`, then `partcrafter`, then fast fallbacks
- **draft** → `triposr`, then `instantmesh`, then higher-cost options
- **part_aware** → `partcrafter`, then `trellis2`, then `trellis`

The router consumes:

- registry metadata,
- runtime config,
- request mode,
- part hints,
- production license policy.

The router must not hardcode license logic outside the registry/policy layer.

## Manifest strategy

Every item result records:

- source image hash,
- extraction metadata,
- normalization settings,
- route decision,
- provider and provider config subset,
- output asset paths,
- render paths,
- QA metrics,
- review flags.

The manifest is the audit surface for both humans and LLM agents.

## Failure policy

A generation provider failure should not crash the whole run unless no allowed fallback exists.

Per-item behavior:

1. try preferred provider,
2. if it fails, record the failure,
3. try the next allowed provider,
4. if all fail, mark the item as failed and continue.

## Quality gate

The v1 scorer is heuristic and measures:

- frame count completeness,
- mean visible coverage,
- coverage stability,
- edge/sharpness estimate,
- non-empty outputs.

This is enough to route obvious failures or low-confidence outputs into human review.
