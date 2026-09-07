# Gen Ladder GL1: TRELLIS import lane

Capture's **Save to Library** imports a completed run as `origin=trellis` with
`job_id=null`. It uses existing artifact files and never creates a worker job.
Asset defaults to the upload filename normalized to a valid identifier; variant
defaults to `default`. Restored sessions without an upload filename use
`trellis-asset`. **Open Library** switches tabs through the single
`forge:open-library` DOM event.

## API and storage contract

`POST /v1/ui/runs/{record_key}/library` accepts `{"asset":"chair","variant":"default"}`.
First publication returns **201** and a version summary. Repeating the run key
returns **200** and the existing summary, including concurrent requests. Keys
dedupe across the whole library: changing the requested destination does not
create another version. A retry still requires a completed run record but does
not require the original artifacts to remain present after successful import.

Unknown records return **404**; incomplete runs, missing primary assets, unsafe
paths, excessive artifact sizes, or GLB inspection violations return **422**.
Forge disabled returns **503**. Each artifact has a **256 MiB** ceiling. This
route uses filesystem placement, without base64 or the completion route's
32 MiB limit. GLB bytes pass the existing `forge_glb.inspect` and `violations`
checks; atlas coverage remains unmeasured when no atlas is supplied.

The GLB becomes `artifacts/model.glb`. The first existing MP4 preview becomes
`artifacts/preview.mp4`; both URL and manifest filesystem previews are supported.
Sources must be regular files confined to the artifact root, with no symlink
components. Publication tries `os.link`, then `shutil.copy2` on link failure.
Only the store's pending directory is renamed; Capture artifact paths remain
available. An unpublished failure does not allocate a version number.

Version metadata retains run provenance, GLB inspection, artifact byte sizes,
and empty part layers. Publication writes a complete skipped critic verdict.
Imports have no iteration action or job-history fetch; history shows the run.
They are excluded from the iteration parent picker. Accept supports jobless
versions, and critic reruns remain skipped instead of queuing unclaimable work.
Existing job-backed completion and critic behavior is preserved.

## Validation and scope

The new import tests cover retained source and served bytes, hard links, a
second temporary source directory with forced `EXDEV` and real `copy2`, path
rejection, failure cleanup, concurrent dedupe, jobless critic/accept behavior,
metadata, disabled Forge, unusable meshes, and a valid GLB above 32 MiB.
The configurable store ceiling is exercised with an 8-byte limit. UI static
checks cover controls, module syntax, navigation, and jobless detail behavior.
The original Capture checksum still checks all bytes outside explicit GL1
additions; existing bake/store/critic tests are unchanged.

Retained gate logs: `/tmp/genladder-gl1-suite.log` and
`/tmp/genladder-gl1-healthz.log`. Repository tests and the existing loopback
health endpoint are separate evidence lanes. Browser rendering and live model
generation are not established by these gates. No Git commands, Docker
commands, service changes, or container deployment are part of this phase.

Commit message:
`Gen Ladder GL1: TRELLIS import lane — run-to-library versions (origin=trellis)`
