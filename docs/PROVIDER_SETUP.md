# Provider Setup

This repository intentionally separates the orchestration layer from heavyweight provider environments.

## Recommended layout

```text
project-root/
├── open_sprite_pipeline/
├── vendor/
│   ├── Grounded-SAM-2/
│   ├── TRELLIS/
│   ├── TRELLIS.2/
│   ├── PartCrafter/
│   ├── TripoSR/
│   └── InstantMesh/
└── .venvs/
    ├── grounded_sam2/
    ├── trellis/
    ├── trellis2/
    ├── partcrafter/
    ├── triposr/
    └── instantmesh/
```

## Principle

Each provider gets its own Python executable. The orchestrator calls that executable directly.

After wiring paths, run preflight before attempting generation:

```bash
python -m open_sprite_pipeline.cli preflight --config configs/app.example.yaml
```

Preflight checks enabled provider executables, adapter scripts, repo directories,
and required config/checkpoint paths. It does not claim model inference works.
Only a non-mock smoke run with generated assets and render outputs proves a
provider is integrated.

## Example config fragments

### Grounded-SAM-2

```yaml
extractor:
  kind: grounded_sam2
  grounded_sam2:
    enabled: true
    python_bin: /abs/path/.venvs/grounded_sam2/bin/python
    sam2_checkpoint: /abs/path/checkpoints/sam2.1_hiera_large.pt
    sam2_model_config: /abs/path/vendor/Grounded-SAM-2/configs/sam2.1/sam2.1_hiera_l.yaml
    grounding_dino_config: /abs/path/vendor/Grounded-SAM-2/grounding_dino/groundingdino/config/GroundingDINO_SwinT_OGC.py
    grounding_dino_checkpoint: /abs/path/vendor/Grounded-SAM-2/gdino_checkpoints/groundingdino_swint_ogc.pth
```

### TRELLIS.2

```yaml
providers:
  trellis2:
    enabled: true
    python_bin: /abs/path/.venvs/trellis2/bin/python
    model_id: microsoft/TRELLIS.2-4B
```

### TripoSR

```yaml
providers:
  triposr:
    enabled: true
    python_bin: /abs/path/.venvs/triposr/bin/python
    repo_dir: /abs/path/vendor/TripoSR
```

## Bootstrap script

See `scripts/bootstrap_provider_envs.sh` for a starting point.
