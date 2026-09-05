# Bake Forge host worker (P1–P5)

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
| `FORGE_BAKE_PYTHON` | `/home/alexk/.venv/bin/python`; runs the installed `tools/bake.py` |
| `FORGE_BAKE_CMD` | Unset; test-only whole-command override, split with `shlex` and executed without a shell |
| `FORGE_ONCE` | `1`, `true`, `yes`, `on`: process at most one pipeline job, then at most one critic version |
| `FORGE_CRITIC_URL` | `http://127.0.0.1:18001/v1`; HTTP localhost, 127.0.0.1 or ::1 only |
| `FORGE_CRITIC_MODEL` | `Qwen/Qwen3.5-4B` |
| `FORGE_CRITIC_ENABLED` | Enabled when URL is nonempty; `0` disables; truthy values: `1`, `true`, `yes`, `on` |

The worker inserts `<spike>/tools` into `sys.path`, disables bytecode writes, and
imports `sheet_match` read-only. It never calls the tool's writing `run`/`main`
entrypoints. Its temporary image buffers stay in memory. Synthetic tests prepare
a temporary spike fixture (including a copy of the installed real helper) before
snapshotting its directories, mtimes and file hashes; workers cannot alter it.

MATCH claims use `kind=pipeline, stages=[matching, review]`. The worker then
checks queued/expired bake candidates and acquires a store-side flock before an
exact-job claim with `stages=[baking], job_id=...`. Existing unfiltered P1 claims
retain bake behavior; a live baking lease prevents another claim for its pair. The API changes uploaded jobs to matching on claim; matching jobs and
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
and `margin` map directly to the helper's threshold and minimum margin. Bake parameters are consumed after approval as described below. Each finding retains stable
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
job. Unapproved staged and terminal jobs are not processed. An unapproved staged
retry remains an empty one-shot claim. Approved jobs enter the bake lane below.


## Bake execution and custody

Run all workers for a spike tree against the same API/store. The local store root
comes from `/v1/forge/status`; workers directly write only lock files and backup
bytes/logs there. All job state, staged inputs, progress and version publication
flow through P1 HTTP. The API remains the single record writer. Bake-only jobs
load no image helper, torch, bpy or Blender Python module.

The three sanctioned spike access locations are:

| Location under `<spike>` | Protocol |
| --- | --- |
| `styled/<asset>/<variant>/views/` | Snapshot PNG names, SHA-256 hashes and copies; replace with the job's exact staged PNG set; restore original names and bytes on success/failure |
| `blockouts/<asset>/<variant>/build_plan.json` | Explicitly approved transient derived-plan write by `bake.py`; snapshot hash and bytes before invocation, restore afterward (remove if originally absent) |
| `bakes/<asset>/<variant>/` | Bake tool output; worker reads only for log tailing and harvest; output is intentionally retained by the tool |

Tools, specs, canonical renders and other tracked spike content remain read-only.
`PYTHONDONTWRITEBYTECODE=1` prevents imported spike helpers from creating caches.
The worker does not construct Blender's `PYTHONHOME` or `PYTHONPATH`; the installed
bake driver owns that environment. Tests exclusively create temporary fake spike
trees and run a temporary fake bake script, without invoking Blender.

A nonblocking `flock` on `<store>/locks/<asset>__<variant>.lock` spans claim,
snapshot, bake, harvest, restore and completion. Other workers leave that pair's
jobs waiting; different pairs can proceed. Keep the persistent lock file: deleting
it would allow two different inodes to represent the same lock. An expired API
lease never bypasses a held worker flock. The API also prevents simultaneous live
baking claims for the same pair. Heartbeats renew the lease every 30 seconds.

`<job>/views_backup/views/*.png`, `build_plan.json` and `snapshot.json` retain
copies and hashes. Restoration checks the actual names and SHA-256 hashes before
marking the manifest restored. Under the pair lock, a subsequent worker first
restores any unfinished snapshot for that pair. SIGTERM/interrupt stops the bake
process group before restoring; an uncatchable kill requires this next-run
recovery and cannot promise immediate restoration. Do not discard backup areas
while recovery is pending. Symlink and hardlink aliases are rejected before
writes, and the store must be disjoint from the spike tree.

The invocation passes `--asset`, `--variant`, and nonzero job values for
`--atlas-tile`, `--turntable`, `--ownership-min`, `--view-iou-warn`, and
`--view-iou-fail`. `turntable=0` is valid and omits both the flag and harvest.
Zero values omit flags as requested, so the installed driver's defaults apply.
The whole-command override receives no appended arguments; fake tests encode
their tree path and behavior directly in that command.

The worker polls `blender.log` every 50 ms and posts every complete line matching
PROJECT/SELFCHECK/VIEW-VALIDATE/TURNTABLE/BAKE/VERIFY, followed by
`BLENDER exited rc=N` (the observed bake-driver exit code). The installed driver
buffers Blender output and writes this log only after Blender exits; live delivery
therefore begins when it writes the file. No spike tool is modified to change
that behavior. Driver stdout/stderr is fully captured in
`views_backup/bake-command.log`. Progress uses P1's existing 200-line ring.

Success requires driver rc=0, a fresh log containing `BAKE `, and fresh required
artifacts (inode/size/mtime/ctime must differ from pre-run observations). Harvest
includes the named GLB, atlas, both JSON reports, blender.log and the requested
`turntable/tt_XX.png` frames. Old frames outside the requested count are excluded.
P1 completion accepts at most 64 artifacts, 64 MiB total and 32 MiB per artifact;
the prior 8 MiB total limit was smaller than megabank's GLB/atlas/eight frames.
Harvested JSON is validated while retaining its exact source bytes, like binary
artifacts. Failure posts an error and marks the job failed through the API after
restoration. If the API is unreachable, error persistence is unverified and the
lease remains subject to expiry; disk restoration still runs.

`worker/complete` allocates v1, v2, etc. and changes the job to ready. The version
records `inputs.staged_views`, `metrics.parts_total`, layer counts from every
`parts[].layer`, coverage, bleed counts, fallback split, per-view harmonize drift,
and the harvested turntable count. A report-provided `selfcheck` is retained.
The installed report omits it, so the worker extracts per-view silhouette IoUs
from the harvested Blender log and the threshold from the read-only
`SELFCHECK_THRESHOLD` constant in `tools/bake_views.py`.

## P5 optional vision critic

The long-running worker has an independent critic thread. Slow or unavailable
inference cannot occupy the bake loop or change a ready job to failed. Critic
claims select the newest pending version whose job is ready, with an independent
300-second lease. Expired claims are reclaimable. `FORGE_ONCE=1` finishes its
pipeline action first, then handles one critic claim before exiting.

Each worker probes the configured endpoint once using a tiny text request and
`chat_template_kwargs: {enable_thinking: false}`. An unsuccessful probe disables
inference for that process lifetime and logs one disable line. Pending reviews
then become skipped, including reruns in that same process. A new worker process
is required to probe again. The retained `probe` records HTTP 200, HTTP acceptance
of the kwarg, and explicit echo separately; HTTP acceptance does not establish
that the server applied the template option.

All critic HTTP uses stdlib urllib with proxies and redirects disabled. The
client raises for nonlocal URLs; the worker catches configuration refusal and
skips critic work. `localhost` is pinned to numeric 127.0.0.1. No model catalog or
shared inference defaults are modified.

Up to four evenly sampled turntable frames are sent as JPEG data URLs, with a
768-pixel maximum dimension and quality 85, alongside the version metrics.
Pillow is already installed; no new dependency is required. Valid JSON fences
are stripped. Invalid JSON or verdict schema gets exactly one repair request;
a second invalid response yields `error` and its first 400 characters. Transport
or image-read failures yield `skipped`. Pass, warn, and fail are advisory verdicts
and never change bake readiness.

Version endpoint suffixes:

- `GET /critic`: full verdict, or `{status: "pending"}` before completion.
- `POST /critic/rerun`: local UI action; resets to pending and invalidates an
  in-flight lease so an older worker cannot overwrite the rerun.
- `POST /critic`: worker-authenticated completion with `lease_id` and `verdict`.

The API writes `critic/critic.json` and a compact `version.json` critic summary
whose `issues` field is a count. Library and version listings include that
summary. The Critic tab shows the full issues, score, summary, failure excerpt,
model and timestamp; it polls pending verdicts and provides a rerun button.
