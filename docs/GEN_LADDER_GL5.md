# Gen Ladder GL5: parametric blockout edits

`iterate_blockout` creates a child of a generate-family version containing
`blockout/spec.yaml`. Multipart creation supplies `asset`, `variant`,
`intent=iterate_blockout`, `parent_job`, `parent_version`, and JSON `edit`.
Bake/match settings remain in `params`; geometry edits live only in
`generate.edit`. New uploads, synthesis hints, and replacement views are
refused for this edit-only lane.

The edit must contain at least one supported field:

- `height`: finite number, 1–300 meters.
- `floor_height`: finite number, 1–10 meters.
- `plinth_floors`: integer, 1–40.
- `tower`: boolean `enabled`, optional positive `width` up to 300 meters and
  `location` in `rear_center`, `front_center`, or `center`. Width/location
  require an enabled tower.
- `palette`: nonempty map of role to exactly six hex digits, without `#`.
  The companion validates roles against the inherited spec.

The companion owns geometry surgery: tower toggles apply first, height scales
section heights, floors are recomputed using each section's floor height, and
an explicit plinth floor count applies last. Floor quantization means the
resulting height can differ from the requested height. The review displays
geometry and palette read from the resulting spec.

## Sources, workspace, and review

The worker fetches the selected version artifact through the existing API,
copies it to `workspace/specs/<asset>.parent.yaml`, and invokes the companion
with a distinct output at `workspace/specs/<asset>.yaml`. Both copies belong
to the Forge workspace. It renders again, then matches inherited parent source
uploads through `GET /jobs/<parent>/uploads/<index>`. Their panels use
`p<index>-p<panel>` and `parent_upload_index`. Successive edits retain those
indexes through the existing upload route; Capture references are copied as
references and use the existing cutout route.

No staged coverage or saved decisions are inherited. Normal match findings
supply auto-accept defaults, and the operator reviews all views again.
The child uses GL4 workspace locking, restoration, staging, approval, bake,
and completion. Locks remain `locks/<asset>__<variant>.workspace.lock` because
all jobs for a pair share a workspace; a per-job lock would permit races.
Version lineage retains the selected parent and original root.

The version detail's Edit blockout button is gated by the spec artifact. Its
form sends only changed values, previews palette colors, creates the child,
and opens its board card. Child reviews show the edited blockout and source
panels; photo regeneration controls are omitted.

## Command override additions

`FORGE_SYNTH_CMD` keeps the GL4 placeholders and adds `{edit_in}` and `{edit}`.
For edit calls, `{inputs}` still expands to `--edit-in PATH --edit JSON`.
Alternatively, an edit-specific override can use:

```text
python fake_synth.py --edit-in {edit_in} --edit {edit} --out {out} --report {report}
```

Overrides are tokenized before substitution and never executed through a shell.
The edit report includes the applied edit. The real CLI's null next-view
recommendation becomes an empty string for the existing blockout schema;
no image confidence is invented.

## Evidence boundaries

Tests exercise fake render/bake subprocesses, real HTTP routes and matching,
GLB validation, v2/v3 lineage, parent immutability, workspace lock contention,
restoration, explicit override placeholders, and the copied real surgery CLI.
Existing test bodies and fixtures are preserved. Real Blender rendering/baking,
browser interaction, and deployment into the service on port 8050 are outside
these tests. The requested health probe is host-only evidence.

Commit message:
`Gen Ladder GL5: iterate_blockout — parametric edits with full lineage`
