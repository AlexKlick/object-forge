# Bake Forge deployment and operator runbook

Phase 6 prepares the checkout for deployment. Container rebuild/recreation,
host worker execution, real Blender bakes, browser checks, and critic inference
are operator gates; repository tests alone do not prove those lanes.

## Architecture

- **State:** FastAPI runs in the `object-forge` container, exposed only at
  `http://127.0.0.1:8050`. Enable `/v1/forge/*` with `FORGE_ENABLED=1`.
  Run one API writer process per store. The store is
  `/data/runs/ui/forge`, under `OPEN_SPRITE_UI_ROOT=/data/runs/ui`.
- **Execution:** one host worker runs MATCH, staging, Blender bakes, and the
  independent optional critic lane. Job/version mutations go through HTTP.
  Local locks and crash-recovery snapshots use the host-mounted store path.
- **Spike tree:** `/home/alexk/debt-city-greybox-spike/apps/greybox/assets` supplies
  read-only tool source and canonical renders. Do not edit its tools. Existing
  `bake.py` execution temporarily writes `styled/.../views` and
  `blockouts/.../build_plan.json`, which the worker snapshots and restores;
  generated outputs live in shared `bakes/`. The whole tree cannot be mounted
  read-only for a bake even though the tools themselves remain read-only.
- **Critic:** the host worker calls the optional loopback service at
  `http://127.0.0.1:18001/v1`; it does not run in the API container and never
  blocks the pipeline loop on model latency. Its verdict is advisory.

Compose resolves `../.runs/trellis2-container` relative to `deploy/`, so the host
path is **`<repo>/.runs/trellis2-container`**, not `<repo>/../.runs/trellis2-container`.
The operator confirmed this existing directory; no bind-mount change is needed.

## Environment

| Process | Variable | Default / meaning |
| --- | --- | --- |
| Server | `FORGE_ENABLED` | Unset disables Forge routes; compose sets `1`. |
| Server + worker | `FORGE_WORKER_TOKEN` | Unset/empty means no worker-token check. If set, pass the identical value to both processes; worker sends `X-Forge-Worker`. Keep real tokens out of committed files. |
| Worker | `FORGE_API` | `http://127.0.0.1:8070` for dev; set `http://127.0.0.1:8050` for this deployment. HTTP loopback IP origin only. |
| Worker | `FORGE_SPIKE_ASSETS` | `/home/alexk/debt-city-greybox-spike/apps/greybox/assets`. Must be disjoint from the resolved store in both directions. |
| Worker | `FORGE_STORE_ROOT` | Unset uses the API-reported root (shared-filesystem dev). For containers set the absolute host root shown below; relative/empty overrides are rejected and symlinks are resolved. Covers locks and recovery snapshots. |
| Worker | `FORGE_SYNTH_CMD` | Unset uses companion `tools/spec_synth.py`; template accepts `{inputs}`, `{asset}`, `{out}`, `{height_hint}`, `{floor_height}`, `{report}`, plus `{edit_in}` and `{edit}` for surgery. |
| Worker | `FORGE_BLOCKOUT_CMD` | Unset uses companion `tools/blockout.py`; template accepts `{spec}`, `{out}`. |
| Worker | `FORGE_BAKE_CMD` | Unset uses companion `tools/bake.py`; whole-command template accepts `{assets_root}`, `{spec}`. Overrides receive no appended arguments; see the Gen Ladder workspace section. |
| Worker | `FORGE_BAKE_PYTHON` | `/home/alexk/.venv/bin/python`, the host interpreter for spike `tools/bake.py`; separate from the repo worker interpreter. |
| Worker | `FORGE_CRITIC_URL` | `http://127.0.0.1:18001/v1`; empty disables. Only loopback HTTP is allowed. |
| Worker | `FORGE_CRITIC_MODEL` | `Qwen/Qwen3.5-4B`, the current code default; operator verifies the model served on the critic lane. |
| Worker | `FORGE_CRITIC_ENABLED` | Defaults on when URL is nonempty; set `0` to disable. |
| Worker | `FORGE_ONCE` | Unset/`0` polls continuously; `1` performs one pipeline polling pass and one critic attempt, then exits. It does not drain a whole job lifecycle. |
| Worker | `FORGE_POLL_INTERVAL` | `2` seconds; must be finite and positive. |

With an override, the API-reported path is diagnostic only and is never opened
on the host. No generic probe-file writer exists in the current API artifact
surface, so the worker compares SHA-256 hashes of canonical JSON from per-variant
versions endpoints against local `versions.json`. This tolerates HTTP formatting
differences. A nonempty index must match; mismatches, missing files, unavailable
API data, or an empty fresh store produce an **identity unverified** warning and
execution continues. This is a startup sanity check at first bake-lock use,
cached for the worker lifetime, not a unique store attestation. Check mount
mapping and restart the worker after correcting a warning. Locks still use the
override even if this advisory check cannot establish identity.

## Deploy procedure (operator session)

1. Commit the intended checkout, including Phase 6, before building. From that
   committed tree:

   ```bash
   cd /home/alexk/documents/open_sprite_pipeline/open_sprite_pipeline
   docker compose -f deploy/docker-compose.interactive-gpu.yml build
   ```

   **`docker cp` is banned from this point onward.** The hotfix path is edit,
   test, commit, rebuild, and recreate. Record the deployed commit with the
   operator gate evidence; do not patch code into the running container.

2. Wait for any existing bake to finish, then stop any previous host worker.
   Recreate the app using the new image:

   ```bash
   docker compose -f deploy/docker-compose.interactive-gpu.yml up -d --force-recreate
   ```

3. Verify both API surfaces:

   ```bash
   curl --fail --silent --show-error http://127.0.0.1:8050/healthz
   curl --fail --silent --show-error http://127.0.0.1:8050/v1/forge/status
   ```

   Expect health success and Forge JSON containing `enabled: true` and
   `store_root: /data/runs/ui/forge`. A health response alone does not prove
   Forge is enabled. Check host ownership/read-write access on the bind mount.

3b. **Store ownership — fixed in compose; migration needed once per deployment.**

   The API writes the forge store from inside the container; the host worker
   writes the same store from outside (Blender cannot run in the container).
   The compose service therefore sets `user: "1000:1000"` so both write as the
   same uid and nothing under the bind mounts is ever root-owned.

   A deployment that ran as root before this change still holds root-owned
   content the container can no longer write. Migrate it **once**, from inside
   the container while it is still running as root, i.e. *before* recreating:

   ```bash
   docker exec open-sprite-object-forge sh -c \
     'find /cache /data/runs /data/tmp /data/outputs ! -uid 1000 \
        -exec chown -h 1000:1000 {} +'
   ```

   `-h` matters: the HF cache under `/cache` is full of snapshot symlinks, and
   a plain `chown -R` follows the link and fixes the blob while leaving the
   link inode root-owned. The migration covers `/cache` deliberately — this
   container also hosts the TRELLIS/SAM2 lane, which would otherwise lose write
   access to its model cache.

   Then recreate (an operator step; `docker cp` stays banned):

   ```bash
   cd deploy && docker compose --env-file open-sprite-trellis2.env \
     -f docker-compose.interactive-gpu.yml up -d --force-recreate
   ```

   Verify: `docker exec open-sprite-object-forge id` reports `uid=1000`, and a
   newly created job directory under
   `<store>/assets/<asset>/variants/<variant>/jobs/` is owned by your user with
   no chown run.

   Historical symptoms, if you meet a deployment still running as root:
   `PermissionError: ... /ui/forge/locks` (store root never chowned, bakes sit
   in `queued_bake`) or `PermissionError: ... jobs/<id>/views_backup` (the job
   directory is root-owned, so the job reaches `queued_bake` and then fails at
   bake start). Re-running the chown per job is a stopgap, not a fix — the API
   creates a fresh `jobs/<job_id>/` every time.

4. Start **one** host worker. The repo `.venv` needs the existing project/API
   dependencies; the bake interpreter and Blender must already support the
   spike toolchain. These commands do not install anything:

   ```bash
   cd /home/alexk/documents/open_sprite_pipeline/open_sprite_pipeline
   export FORGE_API=http://127.0.0.1:8050
   export FORGE_STORE_ROOT=/home/alexk/documents/open_sprite_pipeline/open_sprite_pipeline/.runs/trellis2-container/ui/forge
   export FORGE_SPIKE_ASSETS=/home/alexk/debt-city-greybox-spike/apps/greybox/assets
   export FORGE_BAKE_PYTHON=/home/alexk/.venv/bin/python
   export FORGE_CRITIC_URL=http://127.0.0.1:18001/v1
   export FORGE_CRITIC_MODEL=Qwen/Qwen3.5-4B
   export FORGE_CRITIC_ENABLED=1
   export FORGE_ONCE=0
   export FORGE_POLL_INTERVAL=2
   # If configured in the container, also export the same FORGE_WORKER_TOKEN locally.
   .venv/bin/python -m open_sprite_pipeline.forge_worker
   ```

   Use this foreground command or the user service below, never both.

5. Smoke a job in another terminal. Choose an existing asset/variant with
   canonical renders under `$FORGE_SPIKE_ASSETS/renders/<asset>/<variant>` and
   replace these three example inputs with the intended asset and image sheet:

   ```bash
   ASSET=existing_asset
   VARIANT=existing_variant
   SHEET=/absolute/path/to/sheet.png
   curl --fail --silent --show-error http://127.0.0.1:8050/v1/forge/jobs \
     -F "asset=$ASSET" -F "variant=$VARIANT" -F "files=@$SHEET" \
     -F 'params={"turntable":8}'
   ```

   Record the returned job ID. In the Forge UI, confirm MATCH reaches review,
   review the panel assignments, submit them, wait for `staged`, and explicitly
   approve the bake. Observe `queued_bake -> baking -> ready`, fresh artifacts,
   restored spike inputs, and the critic outcome or explicit skipped reason.
   ACCEPTED is a separate human library decision. Capture the real job/version
   identifiers and browser/worker evidence for the operator E2E gate.

## Gen Ladder: Capture import to Library (GL1)

Capture's **Save to Library** publishes a completed TRELLIS run directly;
**Open Library** opens the resulting library view. The API contract is
`POST /v1/ui/runs/{record_key}/library` with JSON such as
`{"asset":"chair","variant":"default"}`. The UI defaults the asset to the
normalized upload filename (or `trellis-asset` for a restored session without
one) and the variant to `default`.

First publication returns **201**; repeating the record key returns **200** and
the existing version, including concurrent requests. Deduplication is library-wide:
changing asset/variant on a retry does not publish another version. A retry
still needs the completed run record, but after successful publication it does
not need the original artifact files. Unknown records return **404**; incomplete
runs, missing primary assets, unsafe paths, oversized artifacts or GLB gate
violations return **422**. Disabled Forge returns **503** on this import route.

Each artifact has a **256 MiB** ceiling. Import uses filesystem placement,
not the worker completion route's base64/32 MiB per-artifact contract. The GLB
passes the existing inspection gate and becomes `artifacts/model.glb`; the
first existing MP4 preview, when present, becomes `artifacts/preview.mp4`.
Source files must be regular files under the artifact root with no symlink
components. Import uses a hard link, falling back to `copy2` if linking fails:
**link-not-move** keeps Capture's `/v1/artifacts` paths working. Only the store's
pending directory is renamed. Failed unpublished imports consume no version.

Imported versions have `origin=trellis`, `job_id=null`, run provenance, GLB
inspection and byte-size metadata, and empty part layers. Atlas coverage is
unmeasured without an atlas. The critic is pre-skipped; rerun remains skipped
because there is no worker job. Operators can accept these versions, but they
have no job-history fetch or iteration action and are excluded from iteration
parent selection. History shows the Capture run.

## Gen Ladder: generate and blockout review (GL4)

Create a job at `POST /v1/forge/jobs` with multipart `asset`, `variant`,
`intent=generate`, and uploaded `files`, Capture `segment_refs`, or both.
`segment_refs` is a JSON array of `{"image_id":"...","segment_id":"..."}`.
Supply one to seven combined photos/cutouts; eight or more return **422** and
no selected input is silently dropped. The underlying store's eight-reference
limit does not raise this API limit. `height_hint` is 1–300 meters (default 12)
and `floor_height` is 1–10 meters (default 3).

The host worker runs `spec_synth → blockout renders → match → review`, using
`--force-single` for one input and CPU rendering by default. It synthesizes a
spec, renders five canonical views, then matches the source panels to them.
In review, inspect the blockout geometry, palette, confidence, assumptions,
next-view guidance and synthesis report alongside panel assignments.
`GET /v1/forge/jobs/{id}/blockout` returns that review data (404 before synthesis),
`GET /v1/forge/jobs/{id}/renders/{view}.png` serves a retained render, and
`GET /v1/forge/jobs/{id}/blockout/spec.yaml` serves the retained spec.
The cutout proxy is **`GET /v1/forge/jobs/{id}/cutouts/{n}.png`** (or
`GET /jobs/{id}/cutouts/{n}.png` relative to `/v1/forge`); `n` is the zero-based
index in the job's Capture references, not a panel number.

Before submitting a generate review, use
`POST /v1/forge/jobs/{id}/blockout/regenerate` with optional `height_hint`,
`floor_height`, `tower_override`, and `palette_hex` hints. `tower_override`
accepts `none`, `keep`, or an object with positive `width` and `location`
(`rear_center`, `front_center`, or `center`). `palette_hex` maps existing roles
to six hex digits, optionally prefixed with `#`. `none` removes the tower;
`keep` retains photo inference. Width/location and palette changes use the
companion's validated surgery after photo synthesis, retaining the original
photo confidence with an operator-edit assumption.

Regeneration is restricted to `intent=generate`, **unsubmitted review**, and
at most **eight regenerations per job**. It clears match decisions and the
lease and transitions **`review → matching`**. Review the fresh result before
submitting. Submit freezes the decisions; the worker then materializes the
staged views. Explicit bake approval is still required before
`queued_bake → baking → ready`, followed by the separate human ACCEPTED choice.

If every panel is rejected and all views are missing, a generate-family job
can have **zero staged views**. After normal review, staging and approval, the
default worker command passes `--allow-palette-only` for a degraded bake using
the spec palette. The log says `NO staged views — palette-only degraded bake`.
This is not photographic coverage; inspect the resulting geometry and colors.
The GLB validation gate still applies. Completed generate-family versions use
`origin=bake`, retain `blockout/spec.yaml`, and expose synthesis geometry and
confidence under `metrics.synth`.

- `<asset>_<variant>_lod.glb`: optional bake artifact; must be fresh and pass the GLB export gate when present, with results in `metrics.glb_lod` (null and a `LOD absent` marker when absent).
- `blockout/build_plan.json`: required generate-family artifact, retained from the workspace bake alongside `blockout/spec.yaml`.

## Gen Ladder: iterate a blockout (GL5)

Use **Edit blockout** on a version with `blockout/spec.yaml`. API creation is
multipart `POST /v1/forge/jobs` with `asset`, `variant`,
`intent=iterate_blockout`, `parent_job`, `parent_version`, and JSON `edit`.
The parent must be a generate-family job and the selected version must belong
to that job and asset/variant. New uploads, synthesis hints and replacement
views are refused. Bake/match settings stay in `params`; the API stores the
geometry edit separately in `generate.edit`.

Send at least one supported edit; the UI sends only changed values:

| Edit key | Contract |
| --- | --- |
| `height` | Finite number, 1–300 meters. |
| `floor_height` | Finite number, 1–10 meters. |
| `plinth_floors` | Integer, 1–40. |
| `tower` | Object with boolean `enabled`; optional positive `width` up to 300 meters and `location` in `rear_center`, `front_center`, `center`. Width/location require an enabled tower. |
| `palette` | Nonempty role-to-color map; exactly six hex digits **without `#`**. The companion validates inherited palette roles. |

The companion applies tower toggles first, scales section heights for a height
edit, recomputes floors using each section's floor height, then applies an
explicit `plinth_floors` last. Floor quantization can change the resulting total
height from the request. Review shows geometry and palette from the resulting
spec; use those values to assess the edit.

The worker copies the selected parent spec into its workspace, applies surgery
to a distinct output, renders again and rematches inherited source uploads and
Capture references. It inherits neither staged coverage nor saved decisions:
review every view again, then stage and explicitly approve the child bake.
Child review omits photo regeneration controls. Lineage retains the selected
parent and original root; the parent version remains unchanged.

## Gen Ladder workspace and command contracts

Generate and `iterate_blockout` jobs write beneath
`<store>/assets/<a>/workspace/<v>/`, where `<a>` is the asset and `<v>` is the
**variant**, not the version number. The workspace contains `specs/`, `in/`,
`blockouts/`, `renders/`, `styled/` and `bakes/`. Companion tools remain in the
spike tree; workspace copies have independent bytes and reject symlink/hardlink
aliases. The host store override must resolve to the API's mounted store.

All jobs for a pair share the workspace. The worker holds
`<store>/locks/<a>__<v>.workspace.lock` across workspace matching, staging and
baking, including claims. Ordinary spike bakes keep the separate
`<store>/locks/<a>__<v>.lock` namespace and snapshot/restore protocol. Before
staging or baking, the worker restores that job's API-retained spec/renders;
before each bake it copies the API-owned staged PNGs. A different job may have
used the workspace during review, so do not substitute its current files or
delete a held lock to resume work.

Command overrides in the environment table are split with `shlex.split`
**before** placeholder substitution and run without a shell. `{inputs}` must
be a standalone token: for synthesis it expands to repeated `--image PATH`
arguments plus single-input/tower flags; for surgery it expands to
`--edit-in PATH --edit JSON`. GL5 also supports explicit `{edit_in}` and
`{edit}` placeholders. `{out}` is the synthesized/edited spec for synthesis,
and the workspace assets root for blockout (renders go beneath `renders/`). `{assets_root}` and `{spec}` select
the workspace for generated bakes. An override is the whole argument list;
without placeholders it is unchanged, and no default flags are appended.
Custom bake commands must include any required palette-only flag themselves.

The companion synthesizes a `default` variant. For a named Forge variant, the
worker adds an empty alias in the workspace spec, renders `default`, and copies
canonical PNGs into the requested variant directory without changing geometry
or companion files. Default bake execution receives the requested variant and
explicit workspace `--assets-root`/`--spec` paths.

## User service (optional operator install)

`deploy/forge-worker.service` is a template prefilled for this checkout. Edit its
literal `Environment=` paths, `WorkingDirectory`, and `ExecStart` if moving it.
Systemd does not expand `$FORGE_STORE_ROOT` or `<repo>` in those fields. Configure
the same optional token locally as in the container. No root access is needed.
After stopping the foreground worker and verifying the API:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/forge-worker.service ~/.config/systemd/user/forge-worker.service
systemctl --user daemon-reload
systemctl --user enable --now forge-worker.service
systemctl --user status forge-worker.service
journalctl --user -u forge-worker.service -n 100 --no-pager
```

For maintenance, use `systemctl --user stop forge-worker.service`; start it again
with `systemctl --user start forge-worker.service`. `Restart=on-failure` restarts
process failures; ordinary API exceptions are logged and retried by the worker.
The unit is not installed or enabled by repository preparation. User-session
lifetime follows the machine's existing user-manager policy.

## Operations

**Single-flight bakes:** never run two workers, or a manual `bake.py`, while a
Forge bake is in flight. The spike has a shared `bakes/` output tree and fixed
staging paths. The worker holds a nonblocking per-asset/variant `flock` before
claiming a bake and through restoration. It is not a global cross-variant lock,
and manual tools do not participate. A stale lock filename does not mean the
lock is held; do not delete lock files to bypass another process.

**Leases and reaping:** pipeline and critic leases last 300 seconds. Pipeline
heartbeats renew every 30 seconds. Claims skip live leases; expired matching,
submitted-review, or baking leases can be reclaimed on later polling. There is
no separate reaper daemon. Bake recovery still needs the filesystem lock.
On retry the worker restores unfinished `views_backup/snapshot.json` snapshots
before staging another job. Do not remove unrestored snapshots, force an active
job forward, or start a second worker to bypass a stalled bake. Inspect the
original process first. Failed jobs are terminal; expired leases alone are not
permission for pruning active state.

**Growth and pruning:** uploads, staged inputs, snapshots, GLBs, atlases,
turntables, and reports accumulate in the host store. API log appends already
retain 200 lines; unusually large individual lines or older logs can still take
space. `scripts/forge-prune.py` is stdlib-only and defaults to dry-run:

```bash
export FORGE_STORE_ROOT=/home/alexk/documents/open_sprite_pipeline/open_sprite_pipeline/.runs/trellis2-container/ui/forge
.venv/bin/python scripts/forge-prune.py --keep-versions 10 --max-age-days 30 --dry-run
```

Review its DELETE/TRIM/REINDEX plan. Before `--apply`, finish any bake, stop the
worker (including critic work), and stop the app so no API writer or browser
mutation can race retention. The API lock is process-local; the prune program
cannot take it. Keep a recoverable store backup outside the store before deleting
data. In the operator session:

```bash
systemctl --user stop forge-worker.service  # if installed; otherwise stop the foreground worker
docker compose -f deploy/docker-compose.interactive-gpu.yml stop object-forge
.venv/bin/python scripts/forge-prune.py --keep-versions 10 --max-age-days 30 --apply
docker compose -f deploy/docker-compose.interactive-gpu.yml up -d
systemctl --user start forge-worker.service  # or use the foreground environment above
```

Recheck health and Forge status after maintenance. Retention rules:

- Keep the newest N version numbers per asset/variant (default 10). ACCEPTED and
  the newest version are always skipped with reasons, even with N=0. Preserve
  retained lineage, active iteration parents, active owning jobs, and versions
  with critic leases. These protections can keep more than N versions and
  prevent version-number reuse. Expired retained critic leases may require
  normal worker recovery before further pruning.
- Remove only unaccepted terminal `failed`/`ready` jobs older than the age limit
  (default 30 days, using the later of creation/update). Old unaccepted ready
  jobs are the never-accepted output lane; every nonterminal state, including
  never-bake-approved uploaded/review/staged jobs, stays protected. Retain jobs
  referenced by retained versions or child jobs, jobs with any lease, and
  unrestored snapshots. ACCEPTED means the persisted current flag; the store
  does not retain a historical ever-accepted flag after unaccepting.
- Rebuild affected `versions.json` indexes; never delete locks or spike outputs.
  Shared spike `bakes/` growth is outside this tool's scope.
- Trim retained inactive jobs' `worker.log` to the latest 200 lines and at most
  1 MiB, configurable with `--log-lines` and `--max-log-bytes`. Drop a partial
  leading line; a single line exceeding the byte budget is dropped. Active
  and unrestored-recovery logs are untouched. Dry-run also leaves indexes,
  directories, and log bytes unchanged.
- Refuse symlinks, hardlinks, special files, malformed records, spike overlap,
  and detected store changes during planning. Apply assumes stopped writers;
  it is not a transaction spanning the whole store. If interrupted, rerun the
  dry-run offline; API listing can also rebuild a stale derived version index.

**Critic skipped reasons:** disabled/empty configuration, refused nonloopback
URL, failed startup probe (disabled for that process lifetime), no turntable
frames, or unavailable/invalid model responses. Read the stored verdict summary
and worker journal. Correct the dependency and restart after a probe failure;
use the UI critic rerun action for the affected version. A skipped critic is
not a model pass. `enable_thinking=false` is sent, but HTTP acceptance alone
does not prove the provider honored that template setting.

## Troubleshooting

| Symptom | Check / action |
| --- | --- |
| Worker cannot claim; Forge routes return 404 | `FORGE_ENABLED` is missing from the running container or the image is stale. Follow commit + rebuild + recreate, then recheck `/v1/forge/status`. |
| Worker mutations return 403 | Match `FORGE_WORKER_TOKEN` in the container and host worker. |
| Lock/snapshot path errors under `/data/runs/...` | `FORGE_STORE_ROOT` is unset. Use the absolute host path above and verify bind-mount permissions. |
| `FORGE_STORE_ROOT identity unverified` | Empty first-deploy indexes cannot establish identity; otherwise compare the mount, index content, and API availability. The warning is advisory, so fix a mismatch before relying on locks or recovery. |
| Bake fails | Open the job card's `worker.log`: inspect PROJECT, SELFCHECK, VIEW-VALIDATE, TURNTABLE, BAKE, VERIFY, BLENDER, ERROR, and RESTORE markers. The job's `views_backup/bake-command.log` retains subprocess output. Check bake interpreter, Blender, missing/stale artifacts, and snapshot restoration. |
| MATCH reports no canonical renders | Check the exact asset/variant under spike `renders/` and the chosen `FORGE_SPIKE_ASSETS`. |
| Job appears stuck after a worker crash | Inspect process/lease/lock ownership and retained snapshots; allow lease expiry and restart one worker. Never launch a competing manual bake. |
| Prune refuses or retains more than expected | Inspect the reported protection/alias/record reason while writers remain stopped. Do not bypass protection by editing ACCEPTED, leases, or recovery manifests. |

## Evidence boundaries

Repository proof consists of the captured unittest and compile gates. The
pre-deploy `/healthz` curl observes the existing service only; it does not prove
the new code is deployed. The operator separately records image rebuild/recreate,
Forge status, host store translation, real bake/restoration, browser job flow,
and critic inference or a skipped reason. No Docker execution or service install
is part of the Phase 6 coding session.
