# Interactive Object Forge UI

The API now serves a same-origin browser workspace at `/` for:

1. uploading a PNG, JPEG, or WebP image;
2. placing positive and negative point prompts on the source image;
3. reviewing the returned mask and transparent cutout;
4. creating an immediate textured silhouette OBJ;
5. optionally sending the cutout through the existing image-to-3D pipeline; and
6. inspecting OBJ or GLB results in an orbitable Three.js viewport.

## Start locally

Install the API dependencies and run the app from the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[api]"
.venv/bin/python -m uvicorn open_sprite_pipeline.api:app \
  --host 127.0.0.1 \
  --port 8050
```

The UI is then available at `http://127.0.0.1:8050/`.

## Interactive segmentation

The preferred runtime uses `Sam2Model` and `Sam2Processor` with
`facebook/sam2.1-hiera-tiny`. It supports multiple positive (`label=1`) and
negative (`label=0`) click prompts. Model loading is lazy and serialized so
concurrent requests do not create duplicate model instances.

Relevant environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPEN_SPRITE_SEGMENTER` | `sam2` | Set to `fallback` for deterministic color-region selection. |
| `OPEN_SPRITE_SAM2_MODEL` | `facebook/sam2.1-hiera-tiny` | Transformers-compatible SAM2 model identifier. |
| `OPEN_SPRITE_SAM2_DEVICE` | `cpu` | Inference device for interactive segmentation. |
| `OPEN_SPRITE_SAM2_LOCAL_ONLY` | `false` | Refuse network model fetches when true. |
| `OPEN_SPRITE_SEGMENTATION_ALLOW_FALLBACK` | `true` | Preserve click workflow if SAM2 cannot load. |
| `OPEN_SPRITE_UI_ROOT` | `.runs/ui` | Durable upload, mask, cutout, and preview-mesh root. |
| `OPEN_SPRITE_MAX_UPLOAD_MB` | `20` | Upload byte limit. |
| `OPEN_SPRITE_GPU_QUEUE_LIMIT` | `8` | Maximum real-generation jobs waiting behind the active job. |

Fallback output is explicitly labeled `color_flood_fallback` in the API and
browser. It must not be reported as SAM2 evidence.

## Generation lanes

- `silhouette`: produces a textured 2.5D OBJ immediately without a GPU. It is
  a reviewable geometry preview, not inferred back-side geometry.
- `pipeline` with `mock=true`: exercises the existing mock provider and renderer.
- `pipeline` with `mock=false`: invokes the configured real provider only when
  `OPEN_SPRITE_ALLOW_REAL_GENERATION=true`.

The real-generation flag is an operational barrier. Do not enable it while the
configured provider's GPU conflicts are active.

## Tailscale

Keep Uvicorn loopback-only and expose it to the tailnet with Tailscale Serve:

```bash
tailscale serve --yes --bg --https=8050 http://127.0.0.1:8050
```

This is tailnet-only. Do not use Tailscale Funnel for this unauthenticated
artifact-generation API.

## Dual-GPU runtime

The interactive production compose file assigns the full generation lane and
the click-segmentation lane separately:

- physical GPU 0 / RTX 3090 is logical `cuda:0` and runs TRELLIS.2;
- physical GPU 1 / RTX 3060 is logical `cuda:1` and runs SAM2; and
- SAM2 fallback is disabled so a successful selection is provider/model proof,
  not a color-region substitute.

Real TRELLIS.2 requests from both API generation routes share one bounded FIFO
queue. One job owns the RTX 3090 at a time, with up to eight additional jobs
waiting. The browser keeps its request open and reports the global queue depth.
The queue is process-local, so restarting the container interrupts active or
waiting requests; deploy or restart only after the queue is idle.

Start it only after the GPU-0 text service has been stopped:

```bash
docker compose --env-file deploy/open-sprite-trellis2.env \
  -f deploy/docker-compose.interactive-gpu.yml up -d --build
```
