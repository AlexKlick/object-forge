# Jira Backlog

This file was generated from the earlier planning pass and kept as the governing implementation backlog for this scaffold. The code in this repository covers the orchestration foundation for the highest-priority tickets, but real provider deployment still depends on installing the heavyweight upstream model environments.

---

# Image-to-3D Sprite Pipeline — Jira-Style Implementation Backlog

## Executive Summary

This backlog translates the current open-source image-to-3D landscape into an implementation plan for a **commercially usable, permissive-license, maintainable pipeline** that turns 2D object imagery into photorealistic 3D assets and rendered videogame sprites.

### Working assumptions
- The target is **strict open-source / permissive licensing** for product work. That means **MIT / Apache-2.0 style** assets are preferred.
- The primary deliverable is **photorealistic rendered sprites / turntables**, not only editable meshes.
- The pipeline must support **item-by-item extraction** from a source image or scene.
- The codebase should stay clean by isolating model-specific logic behind provider interfaces.
- No proprietary SaaS or API-only dependency is assumed in the production-critical path.

### Recommended default stack
- **Segmentation / extraction:** Grounded-SAM-2
- **Hero asset generation:** TRELLIS.2
- **Flexible fallback / multi-format generation:** TRELLIS
- **Part-aware decomposition:** PartCrafter
- **Fast draft lane:** TripoSR or InstantMesh
- **Sprite rendering:** Blender or equivalent headless renderer with controlled camera/light rigs

### Key architecture decision
The best solution is **not** a single monolithic model. The most robust path is:
1. detect / segment item,
2. normalize image,
3. choose generation lane,
4. render canonical views,
5. score quality,
6. cache everything,
7. route hero assets to human review.

### Explicit exclusions
The following are intentionally excluded from the strict-open-source production baseline:
- Tencent Hunyuan3D-2.1 (community license restrictions)
- Stability SPAR3D / Stable Fast 3D (community license restrictions)
- Wonder3D weights (AGPL)
- Era3D (AGPL)
- OpenLRM weights (non-commercial)

---

## Backlog Overview

| ID | Title | Type | Priority | Depends On |
|---|---|---|---|---|
| I3D-EPIC-01 | Build permissive open-source image-to-3D sprite pipeline | Epic | P0 | - |
| I3D-001 | Create model registry and license gate | Story | P0 | - |
| I3D-002 | Build item extraction service with Grounded-SAM-2 | Story | P0 | I3D-001 |
| I3D-003 | Implement image normalization and background policy | Story | P0 | I3D-002 |
| I3D-004 | Implement 3D generation provider interface | Story | P0 | I3D-001 |
| I3D-005 | Add TRELLIS.2 hero asset provider | Story | P0 | I3D-004 |
| I3D-006 | Add TRELLIS flexible fallback provider | Story | P0 | I3D-004 |
| I3D-007 | Add PartCrafter part-aware provider | Story | P1 | I3D-004, I3D-003 |
| I3D-008 | Add fast draft lane with TripoSR / InstantMesh | Story | P1 | I3D-004 |
| I3D-009 | Build sprite rendering service and camera rig | Story | P0 | I3D-005 or I3D-006 |
| I3D-010 | Define asset manifest, metadata, and reproducibility schema | Story | P0 | I3D-001 |
| I3D-011 | Add cache, artifact lineage, and deterministic reruns | Story | P0 | I3D-010 |
| I3D-012 | Build automated QA and routing policy | Story | P0 | I3D-009, I3D-010 |
| I3D-013 | Build evaluation benchmark on representative game assets | Story | P0 | I3D-012 |
| I3D-014 | Add human review and override workflow | Story | P1 | I3D-012 |
| I3D-015 | CI, smoke tests, and ops runbook | Story | P1 | I3D-011, I3D-012 |

---

## I3D-EPIC-01 — Build permissive open-source image-to-3D sprite pipeline

**Type:** Epic  
**Priority:** P0

### Context
We need a clean, maintainable system that turns isolated 2D objects or scene items into photorealistic 3D assets and rendered sprite sheets, using only models and weights that are appropriate for a commercially usable open-source stack.

### Objective
Deliver a pipeline that:
- extracts one or more items from input imagery,
- generates 3D assets via open-source providers,
- renders consistent sprite outputs,
- records provenance and quality metrics,
- supports agentic orchestration without model-specific spaghetti code.

### Non-goals
- Full character animation / rigging in v1
- Full scene relighting editor in v1
- API-only or closed-source dependencies in the critical path
- Manual artist cleanup as a hidden required step

### Acceptance Criteria
- [ ] End-to-end pipeline can process at least one single-object input and one multi-object input.
- [ ] All production-path model providers use permissive or otherwise explicitly approved licenses.
- [ ] Every output asset includes a machine-readable manifest linking source image, preprocessing steps, model/provider, prompt/config, render settings, and QA scores.
- [ ] A new engineer can trace the path from raw input to sprite output without reverse-engineering provider-specific code.
- [ ] An LLM agent can invoke each stage through stable, documented interfaces without hidden manual assumptions.

---

## I3D-001 — Create model registry and license gate

**Type:** Story  
**Priority:** P0  
**Depends On:** None

### Problem
Most “open-source” image-to-3D releases are not equally usable for product work. Without a formal registry and license gate, the system will drift into accidental use of restricted models.

### Objective
Create a single source of truth for model eligibility, routing policy, hardware needs, expected output format, and license classification.

### Implementation Notes
- Create `model_registry.yaml` or equivalent.
- Include fields:
  - `model_id`
  - `provider`
  - `task`
  - `license_type`
  - `commercial_status`
  - `weights_source`
  - `min_vram_gb`
  - `input_expectations`
  - `output_types`
  - `hero_asset_eligible`
  - `part_aware`
  - `notes`
- Add hard validation that blocks restricted providers from production routing.
- Support environment-specific overrides (`research`, `prototype`, `production`).

### Recommended baseline entries
- Allow: TRELLIS.2, TRELLIS, PartCrafter, 3DTopia-XL, TripoSR, InstantMesh, Direct3D-S2
- Deny by default: Hunyuan3D-2.1, SPAR3D, Stable Fast 3D, Wonder3D weights, Era3D, OpenLRM weights

### Deliverables
- Model registry file
- License policy checker
- Unit tests for allow/deny behavior
- Documentation page explaining classification rules

### Acceptance Criteria
- [ ] Registry can be loaded without network access.
- [ ] Production mode rejects denied models with a clear error message naming the blocking license rule.
- [ ] Unit tests cover at least one permissive model and one restricted model.
- [ ] A human reviewer can update a model entry without changing routing code.
- [ ] The generation router consumes only registry metadata and does not hardcode license logic elsewhere.

### Agent-Comprehension Notes
- The agent must never infer model eligibility from a string match alone.
- The agent must read the registry first.
- If a requested model is absent from the registry, the system must return `UNCLASSIFIED_MODEL` rather than auto-allow.

---

## I3D-002 — Build item extraction service with Grounded-SAM-2

**Type:** Story  
**Priority:** P0  
**Depends On:** I3D-001

### Problem
Single-image 3D generation works best on isolated objects. Raw scene images with clutter reduce quality and blur model responsibility.

### Objective
Create a deterministic item extraction stage that can isolate one or more candidate objects from a source image and produce RGBA crops plus masks.

### Scope
- Object phrase grounding
- Mask extraction
- Bounding box expansion / padding
- RGBA cutout generation
- Optional multiple-item output for scene decomposition

### Implementation Notes
- Separate interfaces:
  - `detect_items(image, prompts=None)`
  - `segment_item(image, detection)`
  - `export_cutout(image, mask)`
- Retain original image coordinates in metadata.
- Avoid coupling segmentation code to downstream 3D providers.

### Deliverables
- Extraction service module
- JSON schema for detections and masks
- Test fixtures for single-object and multi-object scenes

### Acceptance Criteria
- [ ] Service outputs per-item cutouts with alpha masks and stable IDs.
- [ ] Each cutout retains a reversible mapping to original image coordinates.
- [ ] Multi-object inputs produce a deterministic ordering rule.
- [ ] Low-confidence masks are marked explicitly rather than silently dropped.
- [ ] Downstream providers can consume the cutout without needing scene-level knowledge.

### Agent-Comprehension Notes
- The agent must understand that segmentation is a preprocessing stage, not a model-quality afterthought.
- The agent should treat low-confidence extraction as a routing signal for human review or fallback.

---

## I3D-003 — Implement image normalization and background policy

**Type:** Story  
**Priority:** P0  
**Depends On:** I3D-002

### Problem
Different generators expect different image statistics. Inconsistent normalization leads to inconsistent geometry and materials.

### Objective
Standardize the input contract for all providers.

### Required behaviors
- Normalize object scale and framing
- Pad to a canonical square canvas
- Preserve alpha where available
- Remove background when needed
- Store both raw and normalized versions

### Special handling
- Add an optional “render-like stylization” preprocessor only for models that benefit from it, especially PartCrafter on real photographs.
- Do not make stylization the default for hero photoreal assets unless explicitly routed.

### Deliverables
- Normalization module
- Configurable background policy
- Visual regression examples

### Acceptance Criteria
- [ ] Given the same input image, normalization is deterministic.
- [ ] Provider-specific normalization decisions are configuration-driven.
- [ ] Both original crop and normalized artifact are saved and referenced in metadata.
- [ ] Real-photo stylization is opt-in and never silently applied to all providers.
- [ ] The output image contract is documented in one place.

### Agent-Comprehension Notes
- The agent must know that normalization changes model behavior materially.
- The agent must record whether stylization occurred.

---

## I3D-004 — Implement 3D generation provider interface

**Type:** Story  
**Priority:** P0  
**Depends On:** I3D-001

### Problem
Without a provider abstraction, the codebase will become a collection of special cases tied to individual repos.

### Objective
Create a stable interface for all image-to-3D backends.

### Required interface
```text
prepare(input_asset, provider_config) -> prepared_input
run(prepared_input, run_config) -> raw_generation_output
postprocess(raw_generation_output) -> canonical_asset_bundle
healthcheck() -> provider_status
```

### Canonical asset bundle
Must support the following optional fields:
- `mesh_obj`
- `mesh_glb`
- `mesh_ply`
- `gaussian_ply`
- `radiance_field`
- `pbr_maps`
- `preview_renders`
- `provider_metrics`

### Deliverables
- Base provider interface
- Shared error taxonomy
- Example mock provider for tests

### Acceptance Criteria
- [ ] Adding a new provider requires implementing only the documented interface.
- [ ] Provider-specific temp files never leak beyond the provider boundary.
- [ ] The router can invoke any provider without knowing repo-specific command syntax.
- [ ] Failures return structured error codes rather than stack traces only.
- [ ] At least one integration test uses a mock provider to verify orchestration logic independent of GPU inference.

### Agent-Comprehension Notes
- The agent must interact with providers through the interface, not shell scripts directly.
- Provider outputs must be normalized into the canonical bundle before downstream rendering.

---

## I3D-005 — Add TRELLIS.2 hero asset provider

**Type:** Story  
**Priority:** P0  
**Depends On:** I3D-004

### Why this matters
TRELLIS.2 is the strongest permissive open-source default for high-fidelity image-to-3D with full PBR materials and arbitrary topology support.

### Objective
Integrate TRELLIS.2 as the default high-quality generation lane for hero assets.

### Implementation Notes
- Expose at least:
  - resolution mode (`512`, `1024`, `1536`)
  - material generation toggle
  - export targets (`GLB`, `OBJ`, `PLY`)
- Require explicit VRAM check before launch.
- Capture generation timing and export stats.

### Deliverables
- TRELLIS.2 provider
- Config preset for `hero`, `standard`, and `draft`
- Smoke test with at least one known-good example image

### Acceptance Criteria
- [ ] Provider refuses to start when hardware requirements are not met and returns a structured `INSUFFICIENT_VRAM` error.
- [ ] Provider exports a canonical bundle including geometry and material outputs when available.
- [ ] A `hero` preset exists and is documented.
- [ ] Output previews are rendered automatically for QA.
- [ ] A reproducible example run can be executed from a single command.

### Agent-Comprehension Notes
- Route assets here when quality matters more than throughput.
- Prefer this lane for photorealistic sprites, reflective surfaces, and hero props.

---

## I3D-006 — Add TRELLIS flexible fallback provider

**Type:** Story  
**Priority:** P0  
**Depends On:** I3D-004

### Why this matters
TRELLIS remains unusually useful because it can output radiance fields, 3D Gaussians, and meshes from the same structured latent framework.

### Objective
Integrate TRELLIS as the flexible fallback and research lane.

### Implementation Notes
- Support output mode selection:
  - `mesh`
  - `gaussian`
  - `radiance_field`
- Allow multi-image conditioning where multiple views exist.
- Use this provider when the downstream target is rendered views rather than only editable meshes.

### Deliverables
- TRELLIS provider
- Multi-view input adapter
- Documentation for choosing output formats

### Acceptance Criteria
- [ ] Provider supports single-image and multi-image input modes.
- [ ] Output mode is selectable without code changes.
- [ ] Render path can consume Gaussian or radiance-field outputs where configured.
- [ ] Provider can be chosen automatically when TRELLIS.2 is unavailable.
- [ ] The router records why TRELLIS was chosen instead of TRELLIS.2.

### Agent-Comprehension Notes
- Prefer this lane when rendered turntables matter more than mesh editability.
- Use multi-image conditioning whenever additional views are available.

---

## I3D-007 — Add PartCrafter part-aware provider

**Type:** Story  
**Priority:** P1  
**Depends On:** I3D-004, I3D-003

### Why this matters
PartCrafter is the closest open-source match to “generate 3D item by item from a 2D image,” because it can jointly produce multiple semantically distinct parts from a single RGB image.

### Objective
Use PartCrafter as the decomposition / part-aware lane, not as the default photoreal hero renderer.

### Implementation Notes
- Support explicit `num_parts`
- Support optional part-count suggestion only if the provider remains replaceable
- Treat stylization as optional preprocessing for real photos
- Output part meshes and part metadata separately

### Risks
- Real-photo domain gap
- Part count ambiguity
- Part semantics may differ from gameplay semantics

### Deliverables
- PartCrafter provider
- Part manifest format
- Part assembly preview renderer

### Acceptance Criteria
- [ ] Provider outputs individually addressable part assets with stable IDs.
- [ ] Every part includes parent-object linkage metadata.
- [ ] Real-photo inputs can optionally pass through a stylization lane, and that decision is recorded.
- [ ] The router does not select PartCrafter as the default hero path for all objects.
- [ ] A benchmark example shows improved decomposition over monolithic generation on at least one multi-part object category.

### Agent-Comprehension Notes
- Use when the downstream consumer needs separable sub-assets or interactive components.
- Do not assume the best visual photorealism comes from this lane.

---

## I3D-008 — Add fast draft lane with TripoSR / InstantMesh

**Type:** Story  
**Priority:** P1  
**Depends On:** I3D-004

### Problem
Hero-quality models are too expensive for bulk ideation, dataset bootstrapping, or quick iteration.

### Objective
Provide a low-latency path for draft assets.

### Routing policy
- Use TripoSR for fastest reconstruction-style drafts.
- Use InstantMesh when a mesh-first draft with better structure is preferred.
- Promote candidates to TRELLIS.2 only after draft QA or human selection.

### Deliverables
- TripoSR provider
- InstantMesh provider
- Promotion policy from draft lane to hero lane

### Acceptance Criteria
- [ ] Draft lane is measurably faster than hero lane on the same hardware class.
- [ ] Draft outputs are flagged `draft_only=true` in metadata.
- [ ] Promotion to hero lane preserves lineage to the draft artifact.
- [ ] Router can choose draft lane based on explicit throughput policy.
- [ ] Operators can disable the draft lane globally without code edits.

### Agent-Comprehension Notes
- Use this for breadth, not final quality.
- The agent must never present draft assets as final-approved hero outputs.

---

## I3D-009 — Build sprite rendering service and camera rig

**Type:** Story  
**Priority:** P0  
**Depends On:** I3D-005 or I3D-006

### Problem
3D generation is only half the task. For videogame sprites, render consistency is the product.

### Objective
Create a deterministic renderer that converts 3D assets into sprite sheets, turntables, and angle-locked renders.

### Required capabilities
- Canonical camera rigs (front, 3/4, side, rear, top-down if needed)
- Configurable light rigs
- Alpha output
- Shadow toggle
- Material-consistent rendering
- Batch render mode

### Deliverables
- Headless render pipeline
- Render preset definitions
- Sprite sheet packer

### Acceptance Criteria
- [ ] Given the same asset and preset, rendering is deterministic.
- [ ] Camera and light presets are versioned artifacts.
- [ ] Renderer supports transparent background exports.
- [ ] Sprite sheets include metadata describing frame order and camera angles.
- [ ] At least one render preset is optimized for photorealistic prop sprites.

### Agent-Comprehension Notes
- The renderer is a first-class subsystem, not a post-hoc screenshot tool.
- The agent must specify which preset produced which sprite sheet.

---

## I3D-010 — Define asset manifest, metadata, and reproducibility schema

**Type:** Story  
**Priority:** P0  
**Depends On:** I3D-001

### Problem
Without artifact lineage, the team will lose track of what image, model, config, and render preset produced a given sprite.

### Objective
Define a canonical manifest format for every generated asset bundle.

### Required fields
- input image hash
- extraction stage outputs
- normalization config
- provider name and version
- model checkpoint reference
- run config
- hardware summary
- generated files
- QA metrics
- promotion / approval state

### Deliverables
- JSON schema or Pydantic model
- Manifest validator
- Example manifests

### Acceptance Criteria
- [ ] Every output bundle contains a valid manifest.
- [ ] The manifest is sufficient to rerun generation from source input.
- [ ] File hashes are recorded for all major artifacts.
- [ ] Invalid manifests fail validation before publication.
- [ ] The schema is documented for both humans and agents.

### Agent-Comprehension Notes
- The manifest is the authoritative memory of the pipeline.
- Agents must read manifests instead of inferring provenance from filenames.

---

## I3D-011 — Add cache, artifact lineage, and deterministic reruns

**Type:** Story  
**Priority:** P0  
**Depends On:** I3D-010

### Problem
Image-to-3D inference is expensive. Without caching and stable identifiers, the system will waste GPU time and duplicate outputs.

### Objective
Implement content-addressed caching and rerun support.

### Implementation Notes
- Hash normalized input + provider + config + checkpoint ID
- Reuse prior artifacts when hashes match
- Store failed-run state separately from successful cache hits

### Deliverables
- Cache key library
- Artifact store layout
- Cache invalidation rules

### Acceptance Criteria
- [ ] Re-running the same request with the same config hits cache rather than recomputing.
- [ ] Changing any generation-affecting parameter changes the cache key.
- [ ] Cache metadata links draft and hero versions of the same source asset.
- [ ] Failed runs do not poison successful cache entries.
- [ ] A rerun command can reconstruct the output from manifest data.

### Agent-Comprehension Notes
- Agents must prefer cache hits when reproducibility is acceptable.
- Agents must state whether a result was cached or newly generated.

---

## I3D-012 — Build automated QA and routing policy

**Type:** Story  
**Priority:** P0  
**Depends On:** I3D-009, I3D-010

### Problem
Single-image 3D outputs can look plausible from the front and fail from the back or under relighting. A pipeline without QA will silently ship brittle assets.

### Objective
Score generated assets and route them to acceptance, retry, alternate provider, or human review.

### Candidate QA metrics
- silhouette consistency across views
- front-view identity preservation
- backside plausibility
- material consistency across camera orbit
- alpha cleanliness
- mesh watertightness or non-manifold warnings
- part count agreement (where applicable)

### Deliverables
- QA scorer module
- Routing policy engine
- Retry / fallback rules

### Acceptance Criteria
- [ ] Each asset receives a structured QA report.
- [ ] Failing metrics produce explicit routing actions.
- [ ] The system can retry a failed asset with a different provider according to policy.
- [ ] Human-review thresholds are configurable.
- [ ] QA results are stored in the asset manifest.

### Agent-Comprehension Notes
- Agents must not equate “model succeeded” with “asset is good.”
- Routing decisions must be traceable to explicit QA metrics.

---

## I3D-013 — Build evaluation benchmark on representative game assets

**Type:** Story  
**Priority:** P0  
**Depends On:** I3D-012

### Problem
Model choice discussions stay vague unless measured on your actual asset mix.

### Objective
Create a benchmark set that reflects expected production use.

### Benchmark buckets
- metallic props
- soft goods / clothing-like objects
- glossy consumer objects
- thin structures / antennas / handles
- articulated / multi-part objects
- cluttered real-world photos

### Deliverables
- Curated benchmark dataset
- Baseline results table
- Repeatable evaluation script

### Acceptance Criteria
- [ ] Benchmark contains at least 5 asset categories relevant to the target game style.
- [ ] Each category includes both “easy” and “hard” examples.
- [ ] At least three providers are benchmarked on the same subset.
- [ ] Results include both automatic metrics and human preference scores.
- [ ] The chosen default router policy is justified by benchmark results, not preference alone.

### Agent-Comprehension Notes
- Agents must use benchmark evidence when recommending provider defaults.
- New providers must be benchmarked before becoming production defaults.

---

## I3D-014 — Add human review and override workflow

**Type:** Story  
**Priority:** P1  
**Depends On:** I3D-012

### Problem
Hero assets and ambiguous decompositions still need review.

### Objective
Allow operators to approve, reject, reroute, or annotate assets without breaking artifact lineage.

### Deliverables
- Review UI or lightweight review manifest editor
- Approval state transitions
- Override reasons

### Acceptance Criteria
- [ ] Human approval state is recorded in metadata.
- [ ] Overrides preserve the original automated QA report.
- [ ] Reviewers can request reroute to another provider without manual file wrangling.
- [ ] Rejected assets are never silently reused as approved cache hits.
- [ ] Review comments can be consumed by downstream agents.

### Agent-Comprehension Notes
- Agents must treat human override as higher priority than automatic routing.
- Agents must surface previous review history when reprocessing the same source asset.

---

## I3D-015 — CI, smoke tests, and ops runbook

**Type:** Story  
**Priority:** P1  
**Depends On:** I3D-011, I3D-012

### Problem
This stack is multi-model, GPU-heavy, and failure-prone. Without operational discipline it will decay quickly.

### Objective
Establish minimum reliability and onboarding standards.

### Deliverables
- CI for non-GPU logic
- Smoke tests for provider boot / registry validation
- Ops runbook for common failures
- Environment matrix

### Acceptance Criteria
- [ ] CI validates schemas, routing rules, and registry integrity on every change.
- [ ] Smoke tests cover at least one mock run for every provider integration.
- [ ] The runbook documents GPU memory failures, missing weights, broken checkpoints, and license-gate failures.
- [ ] New engineers can bring up the orchestration layer without reading provider source code.
- [ ] A fallback behavior is documented for each provider outage mode.

### Agent-Comprehension Notes
- Agents should emit actionable failure categories that map to the runbook.
- The runbook should be safe to quote directly into automated repair prompts.

---

## Definition of Done for v1

The v1 milestone is complete when all of the following are true:
- A cluttered input image can be decomposed into one or more cutouts.
- A selected object can be routed to a permissive open-source 3D generator.
- The generator produces a canonical asset bundle with manifest data.
- The renderer produces a deterministic sprite sheet.
- QA either approves the result or routes it to retry / review.
- The entire run is reproducible from manifest data.

---

## Recommended Implementation Order

1. I3D-001 — model registry and license gate  
2. I3D-004 — provider interface  
3. I3D-002 — item extraction  
4. I3D-003 — normalization  
5. I3D-005 — TRELLIS.2 provider  
6. I3D-006 — TRELLIS provider  
7. I3D-009 — sprite rendering  
8. I3D-010 — manifest schema  
9. I3D-011 — caching  
10. I3D-012 — QA and routing  
11. I3D-008 — draft lane  
12. I3D-007 — PartCrafter lane  
13. I3D-013 — benchmark  
14. I3D-014 — review workflow  
15. I3D-015 — CI and ops

---

## Final Recommendation

For a clean and commercially safer implementation:
- **Default hero lane:** Grounded-SAM-2 → normalization → TRELLIS.2 → sprite renderer → QA
- **Flexible fallback lane:** Grounded-SAM-2 → normalization → TRELLIS → sprite renderer → QA
- **Part-aware lane:** Grounded-SAM-2 or isolated object crop → optional stylization → PartCrafter → per-part render / export
- **Fast draft lane:** normalization → TripoSR or InstantMesh → quick sprite previews → optional promotion to TRELLIS.2

This architecture keeps the system maintainable because the selection logic lives in the router and registry, not inside scattered provider-specific scripts.
