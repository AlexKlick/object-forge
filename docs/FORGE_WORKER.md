# Bake Forge P2 host worker

Run one API writer and any host workers against its loopback origin. The API
owns the store; the worker uses only stdlib urllib for HTTP and PIL/spike helpers
for image work. No additional dependencies are required.

```bash
FORGE_ENABLED=1 OPEN_SPRITE_UI_ROOT=/tmp/forge-p2-ui \
  OPEN_SPRITE_SEGMENTER=fallback OPEN_SPRITE_ALLOW_REAL_GENERATION=0 \
  .venv/bin/python -m uvicorn open_sprite_pipeline.api:app --host 127.0.0.1 --port 8070
.venv/bin/python -m open_sprite_pipeline.forge_worker
```

Worker environment:

| Variable | Default / behavior |
| --- | --- |
| `FORGE_API` | `http://127.0.0.1:8070`; HTTP loopback IP origins only; no proxies or redirects |
| `FORGE_SPIKE_ASSETS` | `/home/alexk/debt-city-greybox-spike/apps/greybox/assets` |
| `FORGE_WORKER_TOKEN` | Sent as `X-Forge-Worker`; configure the same token on the API |
| `FORGE_POLL_INTERVAL` | 2 seconds; finite and positive |
| `FORGE_ONCE` | `1`, `true`, `yes`, `on` process one available job, or exit on an empty claim |

The worker inserts `<spike>/tools` into `sys.path`, disables bytecode writes, and
imports `sheet_match` read-only. It never calls the tool's writing `run`/`main`
entrypoints. Its temporary image buffers stay in memory. Synthetic tests prepare
a temporary spike fixture (including a copy of the installed real helper) before
snapshotting its directories, mtimes and file hashes; workers cannot alter it.

Claims use `kind=pipeline` with additive `stages=[matching, review]` filtering so
this worker never claims bake work. Existing unfiltered P1 claims retain bake
behavior. The API changes uploaded jobs to matching on claim; matching jobs and
submitted reviews can be reclaimed after the 300-second lease expires. A
background thread heartbeats every 30 seconds during image work. Successful
stage completion clears the lease. Worker progress appends MATCH/VIEW/error
markers to the existing 200-line `worker.log` ring.

MATCH lists `<spike>/renders/<asset>/<variant>/*.png` and PATCHes `canonical_views`.
Every upload follows `object_mask` → `find_panels` → RGBA crops → `match_panels`,
including individual full-frame files. Fully contained detection boxes are
removed because their pixels are already in the outer crop (the real roof render
has a detached detail inside its main bbox). This avoids duplicate panel findings
without dropping detail pixels or separate extra panels. As in the spike driver, `match_mask`
provides the stricter crop alpha/scoring mask that removes soft shadows. `iou`
and `margin` map directly to the helper's threshold and minimum margin. Other
P1 parameters are retained for later bake phases. Each finding retains stable
upload/panel identifiers, bbox, candidate view, auto view (null for rejects),
IoU, margin and score table. Missing views and rejected/extra panels remain
reviewable. `extras_allowed` follows spike behavior: `allow_extra` must be true
and all canonical views must be claimed; extras always remain visible and need
explicit human decisions.

Worker HTTP additions, all requiring worker authentication and a live lease:

| Method/path below `/v1/forge` | Body |
| --- | --- |
| `PATCH /jobs/{id}` | `{canonical_views, lease_id}` |
| `POST /jobs/{id}/panels/{panel_id}` | PNG bytes; `X-Forge-Lease` header |
| `POST /jobs/{id}/match` | `{report, lease_id}`; saves summary and enters review |
| `POST /jobs/{id}/staged/views/{view}.png` | PNG bytes; `X-Forge-Lease` header |
| `POST /jobs/{id}/staged` | `{lease_id}`; verifies exact staged filename set and enters staged |

For worker-report jobs, review submit requires each reported panel exactly once,
unique accepted/repinned canonical views, and explicit `views_missing` equal to
the uncovered views. Submit records `match.submitted=true` and stays in review;
submitted decisions are immutable. Drafts may be partial. On the next claim,
`align_to_view` uses each decision's view, including human repins, and the API
stores the resulting RGBA under `staged/views/`. No PIL work runs in the review
handler. Approval becomes possible after materialization reaches staged.

Compatibility deviation: P1 tests and clients manually create reportless reviews
and expect immediate staging. Those reportless jobs retain their original
submit behavior; strict panel inventory and deferred staging apply to jobs with
`match.report_saved=true` (including zero-panel reports). P1 tests remain
unchanged. The additive claim filter and artifact endpoints retain existing
request defaults and completion/version shapes.

Crash retry redoes matching from uploads or materialization from frozen decisions
with deterministic PNG bytes. Transport failures retain state and the lease for
expiry/retry; invalid inputs or alignment failures record an error and fail the
job. Staged and later jobs are never processed by this worker. A staged retry is
an empty one-shot claim, so neither images nor job records change.
