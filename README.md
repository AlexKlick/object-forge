# Open Sprite Pipeline

A maintainable, commercially-oriented orchestration scaffold for turning 2D images into itemized 3D assets and photoreal sprite renders using **open-source backends only**.

This repository is designed around a practical reality: the best current stack is **not** one monolithic model. The clean production path is:

1. detect or isolate items,
2. normalize them into a stable input contract,
3. route each item to the best open-source image-to-3D backend,
4. render canonical sprite views,
5. score the outputs,
6. capture full provenance in a machine-readable manifest.

## What is in this repo

- A **core orchestrator** with stable interfaces for extraction, routing, generation, rendering, scoring, and manifests.
- A **license gate** so “open-source” does not silently drift into restricted production usage.
- **Provider adapters** for:
  - Grounded-SAM-2 extraction
  - TRELLIS.2
  - TRELLIS
  - PartCrafter
  - TripoSR
  - InstantMesh
- A **Blender headless renderer** for consistent sprite generation.
- A **mock mode** that runs end-to-end without heavyweight models, so the pipeline can be tested in CI and by LLM agents.
- A **Jira-style backlog** with context-rich tickets and acceptance criteria.

## Recommended production stack

- **Segmentation / extraction:** Grounded-SAM-2
- **Hero asset generation:** TRELLIS.2
- **Flexible fallback:** TRELLIS
- **Part-aware decomposition:** PartCrafter
- **Fast draft lane:** TripoSR or InstantMesh
- **Rendering:** Blender headless with fixed camera and light rigs

## Design principles

- Keep all model-specific logic behind provider interfaces.
- Keep the orchestration layer lightweight and dependency-stable.
- Assume heavyweight model repos need **separate virtual environments**.
- Persist every decision that an LLM agent or human operator would need to audit.
- Fail loudly on license ambiguity.

## Repository layout

```text
open_sprite_pipeline/
├── backend_adapters/          # Thin entrypoints executed inside provider-specific envs
├── configs/                   # App config, model registry, render presets
├── docs/                      # Architecture, agent guide, provider setup
├── schemas/                   # Machine-readable output schemas
├── scripts/                   # Blender rendering and bootstrap helpers
├── src/open_sprite_pipeline/  # Core package
└── tests/                     # Registry, routing, manifest, mock E2E tests
```

## Quick start

### 1) Install the core orchestrator

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2) Run the fully local mock path

```bash
python -m open_sprite_pipeline.cli run \
  --config configs/app.example.yaml \
  --image tests/data/mock_input.png \
  --prompt "toy robot." \
  --mode hero \
  --mock
```

This produces:

- extraction artifacts,
- normalized cutouts,
- mock 3D asset files,
- mock sprite frames,
- a manifest describing every step.

### 3) Turn on real providers

Edit `configs/app.example.yaml` and set the relevant provider `enabled: true`, plus the provider-specific:

- `python_bin`
- `repo_dir`
- checkpoints / config paths where needed

Then run:

```bash
python -m open_sprite_pipeline.cli run \
  --config configs/app.example.yaml \
  --image /path/to/input.png \
  --prompt "leather boot. brass buckle." \
  --mode hero
```

## Why the provider-env split matters

TRELLIS, TRELLIS.2, Grounded-SAM-2, PartCrafter, InstantMesh, and TripoSR can all place incompatible demands on CUDA, PyTorch, or auxiliary libraries. This repo keeps the orchestration layer stable by assuming each heavyweight backend can live in its **own virtual environment** and be invoked via subprocess.

That choice makes the codebase cleaner and reduces dependency crossfire.

## Managed TRELLIS.2 container

The maintained real-provider lane is `deploy/docker-compose.trellis2.yml`. It builds a CUDA 12.4 image with TRELLIS.2, CuMesh, O-Voxel, Blender, and the API server wired to `configs/app.container.yaml`.

```bash
docker compose --env-file deploy/open-sprite-trellis2.env -f deploy/docker-compose.trellis2.yml config
docker compose --env-file deploy/open-sprite-trellis2.env -f deploy/docker-compose.trellis2.yml up --build
```

This lane targets GPU 0 / RTX 3090. Do not run it concurrently with the default GPU-0 text inference lane. See [`docs/TRELLIS2_DOCKER.md`](docs/TRELLIS2_DOCKER.md).

## CLI

```bash
python -m open_sprite_pipeline.cli --help
python -m open_sprite_pipeline.cli run --help
python -m open_sprite_pipeline.cli preflight --help
python -m open_sprite_pipeline.cli validate-registry --help
```

Run preflight before enabling real providers:

```bash
python -m open_sprite_pipeline.cli preflight --config configs/app.example.yaml
```

The mock path is only available when explicitly requested with `--mock`. A non-mock
production run must not silently fall back to mock provider or mock renderer outputs.

## API

Optional:

```bash
pip install -e ".[api]"
uvicorn open_sprite_pipeline.api:app --reload
```

Endpoints:

- `GET /healthz`
- `POST /v1/pipeline/run`

## Core contracts

### Input contract

The orchestrator takes:

- one image path,
- an optional text grounding prompt,
- a mode (`hero`, `draft`, or `part_aware`),
- optional hints like `parts_hint` or `provider_override`.

### Output contract

Each run emits:

- `manifest.json`
- extraction artifacts,
- normalized images,
- generated 3D assets,
- sprite frames,
- quality scores,
- routing decisions

See `schemas/manifest.schema.json`.

## Current limitations

- The repo includes **working orchestration code** and real provider integration points, but it does **not** bundle large model weights.
- Blender rendering is implemented as a real headless script, but not exercised in CI.
- Real provider preflight reports missing executables, repos, adapters, and checkpoint/config paths, but it is not a substitute for a real smoke run.
- Quality scoring is heuristic in v1. It is intended for automated routing and review flags, not final art approval.

## Next steps

- Add multi-image conditioning lane for TRELLIS.
- Add render farm / queue support.
- Add automatic human review bundles.
- Add objective benchmark harness for your representative asset set.

## Backlog

See [`TODO_JIRA.md`](TODO_JIRA.md).
