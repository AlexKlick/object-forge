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

3b. **FIRST BOOT / RECREATE ONLY — fix store ownership.** The container runs
   as root, so the forge store it creates under the bind mount is root-owned
   and the host worker (running as your user) gets `PermissionError` on
   `<store>/locks`. After the first request that materializes the store (or
   preemptively after any recreate that wipes it), chown it from inside the
   container — no host sudo needed:

   ```bash
   docker exec open-sprite-object-forge chown -R 1000:1000 /data/runs/ui/forge
   ```

   Symptom if skipped: the worker log repeats
   `PermissionError: ... /ui/forge/locks` and bake jobs sit in `queued_bake`.

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
