# Gen Ladder GL4: photos to workspace bake

Generate jobs collect uploaded photos and Capture cutouts, synthesize a core
spec, render five canonical views, and enter the existing match/review/staging
and approved bake flow. The worker imports only the companion's pure image
helpers and invokes its CLIs with bytecode writes disabled. Rendering defaults
to CPU. No new packages, model calls, Git commands, Docker commands, container
changes, or service changes are part of this phase.

## Workspace and ownership

The worker creates `assets/<asset>/workspace/<variant>` beneath its resolved
Forge store root. Its layout includes `specs`, `in`, `blockouts`, `renders`,
`styled`, and `bakes`. Every workspace copy writes independent bytes; worker
path checks reject symlink components and files with multiple hardlinks.
Generate subprocesses receive workspace destinations. Ordinary bakes retain
the existing spike snapshot/restore flow.

Workspace locks use `locks/<asset>__<variant>.workspace.lock`; spike bake locks
retain `locks/<asset>__<variant>.lock`. Workspace locks cover matching, staging,
and baking, including claims. A job's spec/renders are retained in its API-owned
job directory. Staging and baking restore those exact bytes before using the
shared workspace, because another job for the pair may have run during review.
API-owned staged PNGs are copied into the workspace before every bake.

The exported GLB passes the unchanged P7 gate before completion. Versions keep
`origin=bake`, include the `blockout/spec.yaml` artifact, and expose synthesis
confidence and geometry under `metrics.synth`.

## API

Generate creation uses multipart `intent=generate`, `height_hint` (1–300,
default 12), `floor_height` (1–10, default 3), and a JSON `segment_refs` array of
`{image_id, segment_id}`. The store accepts up to eight references; the API
requires one to seven combined photos/cutouts, matching the current synthesis
CLI's maximum. Eight or more are refused with 422; no selected input is dropped.
A single input adds `--force-single` to synthesis.

Job routes beneath `/v1/forge/jobs/<id>`:

- `GET cutouts/<index>.png` proxies the UiAssetStore cutout for the indexed ref.
- `GET renders/<view>.png` serves the retained render. Worker POST accepts raw
  PNG bytes with `X-Forge-Lease` and the existing worker authentication.
- `GET blockout` serves params, palette, confidence, assumptions, next-view
  guidance, synthesis report, and view names. Before synthesis it returns 404.
- Worker `POST blockout` accepts `{blockout, lease_id, artifact}`, where
  `artifact` is `{encoding: "base64", data: "..."}` containing spec YAML bytes.
  All declared renders must exist. The spec is limited to 2 MiB.
- `GET blockout/spec.yaml` serves the retained spec for workspace restoration.
- `POST blockout/regenerate` accepts optional height/floor hints, a tower
  override (`none`, `keep`, or width/location object), and palette hex edits.
  It requires generate intent and an unsubmitted review, caps regeneration at
  eight, clears match decisions and the lease, and returns the job to matching.

Tower `none` becomes `--override tower=none`; `keep` retains photo inference.
Tower width/location and palette edits use the companion's validated
`--edit-in/--edit/--out` surgery after rerunning photo synthesis. The original
photo confidence is retained with an explicit operator-edit assumption.

## Command overrides and companion adaptations

Overrides are tokenized with `shlex.split` before substitution, with no shell.
An override without placeholders preserves exactly its original argument list.

- `FORGE_BAKE_CMD`: `{assets_root}`, `{spec}`.
- `FORGE_SYNTH_CMD`: `{inputs}`, `{asset}`, `{out}`, `{height_hint}`,
  `{floor_height}`, `{report}`. The standalone `{inputs}` token expands into
  repeated `--image PATH` arguments plus single-input/tower flags. During a
  surgery invocation it expands to `--edit-in PATH --edit JSON` instead.
- `FORGE_BLOCKOUT_CMD`: `{spec}`, `{out}`.

The companion synthesizes a `default` variant. For a named Forge variant, the
worker adds an empty variant alias in the workspace spec, renders `default`,
and copies its canonical PNGs into the requested variant directory. This
changes no geometry or companion files. Bake receives the requested variant
plus explicit workspace `--assets-root` and `--spec` paths.

## Validation boundaries

The tests include fake synthesis/render/bake subprocesses exercising real HTTP
routes, real image matching, the real GLB gate, lease refusal, regeneration,
workspace locking and restoration, cutout proxies, and companion-tree hash
snapshots. Additional tests invoke the current real companion synthesis and
edit CLI from copied fixtures, including the `default` variant and an asset
named `edited`. Existing test bodies remain unchanged; API, bake, and UI suites
have additive tests. UI proof covers source checks and JavaScript syntax.

Retained logs are `/tmp/genladder-gl4-focused.log`,
`/tmp/genladder-gl4-suite.log`, `/tmp/genladder-gl4-suite-final.log`,
`/tmp/genladder-gl4-healthz.log`, and `/tmp/genladder-gl4-qa-scan.log`.
The first full suite preceded the scratch-name collision fix; final acceptance
uses the final suite log. The health probe concerns the existing loopback
service only. Real Blender rendering/baking, browser interaction, and deployment
of GL4 into that service are not established by these tests.

Commit message:
`Gen Ladder GL4: generate lane — photo→spec_synth→blockout→match→review→workspace bake`
