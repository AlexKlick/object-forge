# Forge sets (Factory F1–F2)

A set stores one scene requirements manifest and its uploaded images, then fans
out ordinary Forge child jobs. It uses the scene lane's `requires`, `bindings`,
and `props` contract. F2 exports versions to a Godot-loadable library root; Sets UI follows in F3.

```yaml
scene: block                         # `set` alias; default name: set
description: A block with one hero
palette_only: true                   # default false; requirements may override
style:
  board: [board.png]                 # original uploaded basenames, case-sensitive
  hero: [bank/local]                 # must occur in requires
  seeds_per_view: 3                  # integer 1–6
  strength: 0.62                     # 0.2–0.95
  ip_scale: 0.6                      # 0–1.5
  control_scale: 0.8                 # 0–1.5
  prompt_override: painted miniature # optional, at most 400 characters
requires:
  - asset: bank
    variants: [local, public]        # scalar variants or singular variant also work
    turntable: 8                    # absent → the Forge default (8; the critic needs frames)
    selfcheck_min: 0.96              # forwarded to bake.py --selfcheck-min (job params)
  - asset: kiosk
    variant: local
    style: true                     # explicit override of hero membership
    style_refs: [kiosk.png]          # replaces the entire board for this pair
  - asset: prop
    variant: default
    source: generate                # default spec
    sources: [photo.png]             # generate requires 1–7 uploaded images
    height_hint: 12                  # optional, 1–300
    floor_height: 3                  # optional, 1–10
bindings:
  - kind: bank_branch
    asset: bank
    variants: {default: local, public: public}
    lod: false
props:
  - {asset: prop, variant: default, anchor: tile, count: 4, phase: 0.06}
```

Duplicate asset/variant pairs are errors. Names use Forge's confined identifier
rules. Bindings preserve `lod` and their variant mapping. Props support only the
`tile` anchor and a count of at least one (default four); `phase` is retained.
The rest of the existing `validate_style` settings and defaults are supported.

## Style and source precedence

A requirement with `style: true` styles every one of its variants. `style: false`
disables styling even for heroes. When omitted, hero membership decides. Styled
pairs always disable palette-only mode. Unstyled spec pairs inherit the
requirement's `palette_only`, falling back to the set default. Generate pairs
use the existing generate pipeline; palette-only is an authored-spec option.
A generate pair takes one to seven sources — the synthesis worker's input
ceiling — so a manifest never stores a job the worker would refuse.

`style_refs` replaces the board, including when explicitly empty. Upload the set
board through `style[]`, pair overrides through `pair_style[asset/variant][]`,
and generation inputs through `pair_sources[asset/variant][]`. Pair files share a
filename namespace. Names are case-sensitive original basenames, not paths;
duplicate uploaded basenames within a namespace are rejected. A named file must
be uploaded even when that requirement does not currently style. Each board or
pair override allows at most six references. Images must be supported PNG, JPEG,
WebP, or GIF, nonempty, at most 20 MiB, and at most 36 million pixels.

## HTTP and storage

- `POST /v1/forge/sets`: multipart `manifest` file or text field plus images;
  parser limits are 64 files and 128 fields. Returns 201. Invalid manifests or
  images return 422 and leave no partial set directory.
- `GET /v1/forge/sets`: summaries with `id`, `name`, `created_at`,
  `requested_pairs`, `coverage.percent`, and the integer `attention` count.
- `GET /v1/forge/sets/{id}`: manifest, current pairs, coverage, and launch history.
- `POST /v1/forge/sets/{id}/launch`: optional JSON `{"force": false}`.
- `POST /v1/forge/sets/{id}/retry`: retry eligible child pairs.
- `GET /v1/forge/sets/{id}/style/{n}` and
  `GET /v1/forge/sets/{id}/pairs/{asset}/{variant}/style/{n}`: stored images.
- `GET /v1/forge/jobs?set_id={id}`: only that set's child jobs.

Store records live at `sets/<uuid>/set.json`, with verbatim `manifest.yaml`, board
images in `style/<n>.<ext>`, and pair overrides/sources in
`pairs/<asset>__<variant>/{style,sources}/<n>.<ext>`. Ambiguous pair directory
encodings are rejected. Upload metadata preserves original names while stored
paths are numbered. Set records and launches use the existing atomic store
writers. Child jobs and their version summaries carry `set_id`.

## Launch and retry

Launch returns `{launched: [{asset, variant, job_id}], skipped: [{asset, variant,
reason}]}` and appends `{at, force, launched, skipped}` to `launches`.
Any live child (a state other than `ready` or `failed`) prevents another launch
for that pair, even with force. An accepted version also prevents launching
unless force is true; this checks all versions for the pair. A second launch
while those children are live creates no jobs.

Spec pairs without a matching published catalog asset are skipped with
`no spec in catalog` and reported as blocked. An absent/empty catalog blocks spec
pairs as well. Generate pairs have no authored spec to look up. Publishing the
missing catalog spec permits a later normal launch. `turntable` and
`selfcheck_min` are forwarded to the child job's params only when the manifest
sets them; an absent value keeps the Forge default. This differs from the scene
lane on purpose: its turntable default is 0, but the Forge critic scores the
turntable frames, and a version without them is `critic: skipped` and therefore
never auto-accepted under `FORGE_POLICY=enforce` (the first live set round
flagged 15 of 15 new versions that way). `selfcheck_min` reaches
`bake.py --selfcheck-min`, so a prop like the streetlight (roof view 0.9679
against the 0.97 default floor) bakes with the floor its manifest declares.

Retry is launch without force, restricted to pairs whose latest child is failed
or has attention. Live children, including those awaiting review, are never
duplicated. Planned and catalog-blocked pairs require a normal launch. All child
creation and attachment copies hold the store lock; attachment failure rolls
back children created by that call. Existing worker and server policy hooks
continue to apply to these jobs.

## Coverage

Reads refresh the latest child by creation time and the latest version for each
required asset/variant. Versions are pair-wide, including existing/imported work;
child jobs are scoped to the set. Each pair reports `job_id`, `intent`, `state`,
`accepted`, `version` (number or null), `attention`, `policy_mode`, `reason`, and
`views_complete`. Read-time projections do not rewrite `set.json`.

The scene lane's names and baked percentage are retained, with an additive
acceptance count:

- `coverage.requested_pairs`: number of required pairs.
- `coverage.baked_pairs`: required pairs with a version, even during a later job.
- `coverage.accepted_pairs`: required pairs whose latest version is accepted.
- `coverage.view_complete_pairs`: baked pairs with no missing views in their
  bake report (or explicit `metrics.views_missing`); absent evidence is incomplete.
- `coverage.percent`: baked/requested × 100, rounded to one decimal; zero when empty.
- `status_counts`: counts by current child state (or planned/blocked), sorted by name.
- `unbound_kinds`: `{kind, asset, missing_variants}` for binding variants without
  a baked version; acceptance is counted separately.
- `unplaced_assets`: sorted required assets used by neither a binding nor a prop.
- `attention`: list of pair keys carrying child or latest-version attention.

These are repository/store facts, not live export or Godot acceptance. The
operator launches `assets/sets/debt_city_core.yaml` for live proof.


## Export

`POST /v1/forge/sets/{id}/export` returns 202 and queues a host worker task.
`GET /v1/forge/sets/{id}/export` reads its durable `export.json` record: status
(`requested`, `running`, `done`, `failed`), request/start/finish timestamps,
lease, relative `library_root`, result, error, and history. A live running lease
makes another request return 409. Export leases last 300 seconds; reads/claims
lazily return expired work to the requested queue. Claims choose the oldest
request. A subsequent request resets the result and retains prior history.

Each required pair selects its latest accepted version, or its latest version
when none is accepted. Unaccepted choices are explicitly recorded as
`accepted: false`. Selection is frozen in the worker claim. A pair with no
version is an item with `status: skipped` and `reason: no version`. Other skips
come unchanged from repo B's `asset_library.build_library`: missing GLB/report
parts, BLEND materials, or multiple UV sets. Acceptance does not override these
library checks. The export report's coverage describes advertised pairs, which
can differ from the set's latest-version coverage.

The host worker links artifacts (copies with metadata when linking is unavailable)
from immutable version `artifacts/` directories into this store-relative root:

```text
sets/<id>/library_root/
  bakes/<asset>/<variant>/<asset>_<variant>.glb
  bakes/<asset>/<variant>/<asset>_<variant>_lod.glb  # optional
  bakes/<asset>/<variant>/bake_report.json
  blockouts/<asset>/<variant>/build_plan.json     # optional
  library/asset_library.json
  library/city_bindings.json
  library/report.json
```

Missing required bake files are not invented; repo B skips those pairs. Its own
`build_library(..., digest=True)` and `write_library` produce the inventory,
`bindings_document` produces bindings, and `scene_report(..., applied=True)`
produces items, coverage, status counts, unbound kinds and unplaced assets.
The completion result retains items, coverage, status counts, repo B's skipped
list, and library totals. The skipped list excludes pairs with no version;
those appear in report items. `applied: true` means files were materialized,
not that Godot verification ran.

Every build starts in an empty `.pending-<uuid>` sibling. First publication uses
`os.replace`; a re-export uses Linux `renameat2(RENAME_EXCHANGE)` through the
standard library because `os.replace` cannot overwrite a nonempty directory.
Readers see a complete old or new root, never a partially built tree. A per-set
host lock serializes publication and cleanup; the worker checks lease ownership
before publication. Failed owned attempts remove the root and pending output;
crashed pending directories are removed when the next worker retries. Re-export
never merges old output, so obsolete LODs and blockout plans disappear. Version
artifacts are never moved or rewritten. Treat exported hardlinks as read-only.

When done, the export GET adds absolute `host_paths` for `library_root`,
`asset_library`, `city_bindings`, and `report`. The worker supplies its resolved
`host_store_root` at completion, retained as metadata so a container API can
return host-absolute paths without opening them. Direct store callers that omit
this optional metadata use the API store root. File responses always read via
the API's own mounted store root.
`GET /v1/forge/sets/{id}/library/{name}` serves only `asset_library.json`,
`city_bindings.json`, or `report.json`, and returns 404 until done. Set details
include compact `export` status/finished time/coverage/root (null before the
first request); list summaries include `export_status`.

Godot resolves inventory GLB paths relative to the manifest's grandparent,
`library_root`. No client changes or editor import are required. From repo B,
the operator runs:

```bash
godot --headless --path apps/greybox --script res://tools/verify_library.gd -- --manifest=<library_root>/library/asset_library.json
```

Repository tests use real GLB fixture bytes and read-only repo B helpers.
Live set export and Godot placement verification remain operator proof.
