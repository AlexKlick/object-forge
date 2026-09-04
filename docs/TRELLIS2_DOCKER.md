# TRELLIS.2 Docker Runtime

This is the maintained container lane for the real TRELLIS.2 provider. It exists so the workstation control plane can start and stop image-to-3D generation without depending on the local Conda environment.

## GPU Contract

- Physical GPU: `CUDA:0` / RTX 3090.
- Container-visible device: `CUDA_VISIBLE_DEVICES=0` after Docker exposes only physical GPU 0.
- Do not run this alongside `text-main`, `vllm-nanbeige`, or other GPU-0 inference lanes.
- GPU 1 remains reserved for VibeVoice and voice/TTS work.

TRELLIS.2 requires at least 24GB VRAM, so the RTX 3060 is not a supported target for this lane.

## Static Validation

```bash
docker compose --env-file deploy/open-sprite-trellis2.env -f deploy/docker-compose.trellis2.yml config
```

## Build And Start

```bash
docker compose --env-file deploy/open-sprite-trellis2.env -f deploy/docker-compose.trellis2.yml up --build
```

The service binds to `127.0.0.1:8050` by default and exposes:

- `GET /healthz`
- `POST /v1/pipeline/run`

## Smoke Request

Stop GPU-0 text services first, then run a real request:

```bash
curl -sS http://127.0.0.1:8050/v1/pipeline/run \
  -H 'Content-Type: application/json' \
  -d '{
    "image": "/data/inputs/input.png",
    "prompt": "single stylized rock asset",
    "mode": "hero",
    "provider": "trellis2"
  }'
```

Mount additional input paths in `deploy/docker-compose.trellis2.yml` before using host-local input files outside the repo.
