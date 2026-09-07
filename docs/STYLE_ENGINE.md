# Object Forge style engine (Factory SE1)

This sidecar styles palette-coloured blockout views for the bake track. The SE2
Forge host worker will call it over loopback HTTP. Each seed produces one square
RGBA cutout with the original render's alpha bytes, including antialiased edges.
It does not write the Forge store. Cropping, foreground colour bleed (8 pixels),
working-resolution depth conversion, and silhouette restoration use a vendored
copy of repo B's `apps/greybox/assets/tools/style_compose.py`. Keep everything
below its two-line source header byte-identical; the parity test skips explicitly
when the spike checkout is absent.

## Model and GPU plan

The approved stack is SD 1.5 (`stable-diffusion-v1-5/stable-diffusion-v1-5`, fp16
variant), depth ControlNet (`lllyasviel/control_v11f1p_sd15_depth`), and IP-Adapter
(`h94/IP-Adapter`, `models/ip-adapter_sd15.safetensors`). SD 1.5 is the selected
fit for the remaining GPU 1 budget beside the resident 6.7 GB control lane on a
12 GB RTX 3060; SDXL/Flux are outside this budget. Actual peak VRAM and coexistence
remain operator smoke checks, not a conclusion from unit tests.

The container receives only physical GPU 1, addressed inside it as `cuda:0`.
Models load lazily under one process-local lock, after at least 4500 MiB free is
reported. Missing CUDA/free-memory data also fails closed. The guard is checked
again for subsequent requests, even while loaded; for that recheck the backend
counts torch's reserved-but-unallocated allocator cache as available, because
the device reports it as used although only this process can reuse it.
Inference uses model CPU offload and VAE slicing/tiling — **not** attention
slicing, which would replace the IP-Adapter attention processors with plain
ones and break the first render (`'tuple' object has no attribute 'shape'`).
Each render resets peak CUDA allocation tracking. Idle models unload after 180
seconds, checked every 15 seconds. Unload drops the pipeline, collects Python
objects, and empties the CUDA cache. A failed load or inference unloads the
backend, answers **500 `{"error": "backend_error", "detail": "<Type>: <message>"}`**,
and unloads once more after the exception is discarded so the allocator cache
really returns to the device (re-raising left ~3.8 GB reserved, measured).

Run a single Uvicorn worker: the lock coordinates only this process. GPU memory
admission is a snapshot, not a reservation against another process growing during
inference. The operator must verify peak use and control-lane stability under
load before accepting GPU coexistence; neither the guard nor CPU offload alone
proves that an OOM cannot occur.

Multiple references are grouped under the one loaded IP-Adapter, following the
[upstream reference-list contract](https://huggingface.co/docs/diffusers/using-diffusers/ip_adapter#multiple-ip-adapters).
With zero references, adapter scale becomes zero and the pipeline receives one
neutral grey image. `FakeBackend` uses seeded hue rotation and fine noise; it
requires only Pillow/numpy and is for repository checks, not model-quality proof.
Torch/diffusers imports occur only inside `DiffusersBackend` methods.

## HTTP contract

- `GET /healthz`: `{"ok": true}`; never loads a model.
- `GET /v1/style/status`: `backend`, `loaded`, `busy`, `device`, `free_mb`,
  `models: {base, controlnet, ip_adapter}`, `last_used_s`, `renders`.
  `last_used_s` is seconds since the last attempted render completed (null before
  first use); `renders` counts successfully encoded candidates. Free memory is
  null without CUDA and for the fake backend. Status does not acquire the render
  lock and remains available while inference is busy.
- `POST /v1/style/unload`: waits for the lock, unloads if loaded, returns
  `{"loaded": false}`. Subsequent rendering can load the model again.
- `POST /v1/style/render`: JSON body below. Requests wait their turn; no 409.

```json
{
  "view": "front",
  "init_png": "<base64 PNG>",
  "depth_png": "<base64 PNG>",
  "refs": ["<base64 PNG>"],
  "prompt": "hand-painted wooden game prop",
  "negative": "photograph, blurry",
  "seeds": [11, 22, 33],
  "strength": 0.62,
  "guidance": 6.5,
  "control_scale": 0.8,
  "ip_scale": 0.6,
  "steps": 28,
  "long_side": 768
}
```

`view`, `init_png`, `depth_png`, `prompt`, and `seeds` are required. The other
fields use the shown numeric defaults; `negative` defaults to `""`, `refs` to `[]`.
Use plain base64, without a data-URL prefix. All images must decode to PNG, each
at most 25,000,000 encoded-file bytes. Init must be 2048×2048 RGBA with nonempty
alpha. Depth must have identical dimensions and mode L, I, or I;16; normalized
16-bit linear depth maps 0..65535 to 0..255, near bright, background zero. Reference
PNGs are converted to RGB. Supply 1–6 integer seeds and at most 6 references.
`long_side` must be 512–1024 and a multiple of 64; the working dimensions are
rounded to multiples of 8. Strength is in (0, 1], scales/guidance are nonnegative,
steps is a positive integer, and `int(strength * steps)` must be at least 1.
Seeds must fit torch's integer range [-2**63, 2**64-1]. Invalid input returns
422 with a reason in `detail` before loading models.

Response fields:

```json
{
  "candidates": [{"seed": 11, "png": "<base64 RGBA PNG>", "working_size": [512, 768], "seconds": 1.2}],
  "box": [640, 512, 1152, 1280],
  "scale": 1.0,
  "prompt_tokens": 12,
  "truncated": false,
  "model": "diffusers",
  "peak_mb": 3200.0
}
```

Values above are illustrative, not recorded model evidence. Candidate seconds
cover rendering and output encoding; loading and preprocessing are excluded.
`peak_mb` is the maximum allocated CUDA MiB across seeds (not total device use),
or null for fake. Token counts include tokenizer special tokens for Diffusers;
`truncated` is true when the count exceeds 77, false if the count is unavailable.
A summary log includes view, seeds, working dimensions, total seconds (including
load/preparation), and peak MiB.

GPU admission failure returns **503** with this exact top-level shape:
`{"error": "gpu_busy", "free_mb": 100, "needed_mb": 4500}`. `free_mb` can be null.
The backend is not loaded by a rejected request.

## Environment

| Variable | Default |
| --- | --- |
| `STYLE_BACKEND` | `diffusers` (`fake` for CPU tests) |
| `STYLE_DEVICE` | `cuda:0` |
| `STYLE_BASE_MODEL` | `stable-diffusion-v1-5/stable-diffusion-v1-5` |
| `STYLE_CONTROLNET` | `lllyasviel/control_v11f1p_sd15_depth` |
| `STYLE_IP_ADAPTER` | `h94/IP-Adapter` |
| `STYLE_MIN_FREE_MB` | `4500` |
| `STYLE_IDLE_UNLOAD_S` | `180` |
| `STYLE_MAX_REFS` | `6` |
| `STYLE_MAX_SEEDS` | `6` |
| `STYLE_FRAME_SIZE` | `2048` |
| `STYLE_MAX_IMAGE_BYTES` | `25000000` |

Compose also sets `HF_HOME=/cache/huggingface`,
`HUGGINGFACE_HUB_CACHE=/cache/huggingface/hub`, `HOME=/cache`,
`TRITON_HOME=/cache/triton`, `HF_TOKEN=${HF_TOKEN:-}`, and
`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`. The uid/gid is 1000:1000 so
shared cache files stay host-writable. The only bind mount is
`../.cache/trellis2-container:/cache`; there is no `/data/runs` mount. Compose's
GPU block selects device 1; the sidecar sets neither visibility environment
variable. The main `object-forge` service retains its existing GPUs and mounts.

## Models come from the host, never from inside the container

Containers on this host have **no working DNS**: Docker's embedded resolver
(`127.0.0.11`) cannot reach the external name servers, so `huggingface.co` never
resolves from inside `object-forge` or the sidecar (this is also why SAM2 runs
`LOCAL_ONLY`). The sidecar therefore runs with `HF_HUB_OFFLINE=1` and expects the
three models to already sit in the shared cache. Pull them from the host, which
has DNS, into the bind-mounted cache directory — no host venv ships
`huggingface_hub`, so use `uv` with an ephemeral environment:

```bash
cat > /tmp/pull_style_models.py <<'EOF'
from huggingface_hub import snapshot_download
snapshot_download("stable-diffusion-v1-5/stable-diffusion-v1-5", allow_patterns=[
    "model_index.json", "*/config.json", "*.fp16.safetensors", "tokenizer/*", "scheduler/*", "feature_extractor/*"])
snapshot_download("lllyasviel/control_v11f1p_sd15_depth", allow_patterns=["config.json", "diffusion_pytorch_model.fp16.safetensors"])
snapshot_download("h94/IP-Adapter", allow_patterns=[
    "models/ip-adapter_sd15.safetensors", "models/image_encoder/config.json", "models/image_encoder/model.safetensors"])
EOF
HF_HOME=$PWD/.cache/trellis2-container/huggingface \
  uv run --no-project --with "huggingface_hub>=1.23,<2" python /tmp/pull_style_models.py
```

That is ~5.7 GB (SD 1.5 fp16 2.6 GB, ControlNet 0.7 GB, IP-Adapter + image
encoder 2.4 GB), written as the host user so the uid-1000 container can read it.
First pulled 2026-09-07 (SD 1.5 snapshot `451f4fe`, ControlNet `539f991`,
IP-Adapter `018e402`). A `docker exec … python -` heredoc without `-i` runs an
*empty* script and exits 0 — do not "pull from inside" that way.

## Operator build and smoke

These commands are for the operator after commit. SE1 implementation does not
build/start containers, download models, or smoke a real GPU.

```bash
cd deploy
docker compose --env-file open-sprite-trellis2.env -f docker-compose.interactive-gpu.yml build object-forge-style && docker compose --env-file open-sprite-trellis2.env -f docker-compose.interactive-gpu.yml up -d object-forge-style
```

The image build installs `diffusers` with `pip`, which needs network from the
build container; if the build fails on name resolution, add `network: host`
under the service's `build:` block (build-time only, no runtime change).

From the repository root, using an SE0-B blockout render and style references:

```bash
.venv/bin/python scripts/style-smoke.py \
  --render /path/to/renders/chair/oak/front.png \
  --ref /path/to/style-reference.png \
  --url http://127.0.0.1:8056 --seeds 11,22,33 \
  --prompt "hand-painted wooden game prop" --negative "photo, blurry" \
  --long-side 768 --out /tmp/style-smoke
```

The default depth is `<render dir>/passes/<stem>.depth.png`; override with
`--depth`. Repeat `--ref` to supply multiple references. The CLI writes
`<out>/<stem>-<seed>.png` and prints JSON with request wall seconds, peak MiB,
prompt tokens, truncation, crop box, and working sizes. It exits 2 on 503, printing
the server body; other request/input failures exit 1. The first request has a
30-minute client timeout to allow cold downloads.

## Measured on the first real round (2026-09-07)

`bank_citadel/megabank/front_left` (SE0-B render + depth pass), one mechanical
reference image, three seeds, `long_side=768` → working size 400×768:

| measurement | value |
| --- | --- |
| wall time, cold (model load from cache + 3 renders + encode) | 61 s |
| torch peak allocated (`peak_mb`) | 3 944 MiB |
| GPU 1 device peak (1 s `nvidia-smi` sampling) with the 6.7 GB control lane resident | 10 841 MiB of 12 288 |
| device usage while loaded and idle (weights offloaded to CPU) | +216 MiB over baseline |
| `free_mb` reported while loaded (cache-aware) | 5 073 MiB → the 4 500 guard still admits |
| idle unload | observed at ~200 s; device back to baseline (+76 MiB CUDA context) |
| candidate alpha vs render alpha | byte-identical, all three seeds |
| `style_check.check_png` | pass, all three |
| `style_metrics` (repo B) | palette_drift 4.1–6.7, change 7.3–8.0 (identity 0, +6 tint 2.5) |
| prompt (`style_prompt`, 42 words after the 44-word budget) | 68 CLIP tokens |

Three real-load defects were found and fixed by this round before the first
successful render: ControlNet needed `variant="fp16"` (cache holds fp16 only),
diffusers 0.40 removed the pipeline-level VAE slicing wrappers, and attention
slicing discarded the IP-Adapter attention processors. Each surfaced only under
a real model — the unit suite cannot see them; keep the smoke in every deploy.

## Measured on the first full Forge round (SE2, 2026-09-07)

`from_spec bank_citadel/megabank` with `style.enabled`, one mechanical reference
(the SE0-B `styling_ref` render), default settings (3 seeds per view, 28 steps,
`long_side=768`), seven spec views, host worker → sidecar → review → bake:

| measurement | value |
| --- | --- |
| blockout render, 7 views + depth passes (Blender on the host) | ~50 s |
| style step, 7 views × 3 seeds (sidecar warm after the first view) | ~7 min, ≈60 s per view |
| prompt (pack `short:` + tags + palette roles + view) | 67–68 CLIP tokens, never truncated |
| GPU 1 device peak (1 s sampling) with the 6.7 GB control lane resident | 10 885 MiB of 12 288 |
| review | 21 `source:"style"` panels, all `metrics.pass` and `checks.pass`; palette_drift 3.6–7.0, change 5.3–9.7 |
| chosen candidates vs renders | alpha byte-identical; mean RGB change inside the silhouette ≈16.6 levels |
| bake of the seven staged candidates | ≤30 s; `GLB-CHECK ok` and `GLB-LOD-CHECK ok` (textured 1.0, 1 UV set, OPAQUE) |
| version artifacts | `style/report.json` + one chosen PNG per view, both GLBs, `blockout/{spec.yaml,build_plan.json}` |
| critic (Qwen3.5-4B, loopback) | warn, 85/100 |

The turntable frame of that version shows the painted window grid, facade detail
and brass fixtures; the palette-only bakes before it were flat colour. Visual
quality is still a first pass at a 400×768 working size with a stand-in reference:
a real style board from the operator is the next input, not more parameters.

## Troubleshooting

- `gpu_busy`: inspect status and GPU 1 free memory; wait for other work to release
  memory. Keep the guard at 4500 MiB until operator measurement justifies a change.
  Null `free_mb` means CUDA/free-memory reporting is unavailable. A healthcheck
  passing does not prove CUDA or model loading works.
- First load: weights download only on first render and can take several minutes;
  health remains lightweight, status can report busy. The shared Hugging Face
  cache persists across container replacement.
- No depth pass: regenerate the blockout with the SE0-B tool in repo B. Supply the
  linear depth pass from the same camera, not a colour image or guessed depth.
- Dependency mismatch: `diffusers==0.40.0` requires `huggingface-hub>=1.23,<2` in
  this image. The hub upgrade is confined to `Dockerfile.style`; it changes no
  main-image dependencies. Container dependency compatibility still needs the
  operator build/load check.
- Long prompt: inspect `prompt_tokens`/`truncated`, then shorten the prompt if
  the important description falls beyond the model's 77-token context.

Repository validation uses `make test PYTHON=.venv/bin/python` and pure fake or
stubbed backends. It proves request handling and wiring, not GPU peak budgets,
model download compatibility, inference quality, or control-lane coexistence.
