"""GLB invariants — the part of a bake its own gates cannot see.

Every metric Forge records for a bake is measured inside Blender, on Blender's
own scene: selfcheck silhouette IoU, per-view coverage, harmonize drift, and
the vision critic's verdict on turntable renders. None of it survives the glTF
export boundary. A bake can therefore be green on every gate and still export a
mesh that renders as nothing in a real-time engine, which is exactly what
happened to v1:

  * ``alphaMode: BLEND`` — Blender renders opaque texels identically whether
    the material blends or not, but glTF BLEND puts the mesh in a real-time
    renderer's transparent pass with no depth write, so pale assets dissolve
    into a bright background.
  * a stray second UV set — Blender's texture node reads the *active* layer,
    but the exporter writes every layer in order and ``baseColorTexture``
    omits ``texCoord``, which glTF defaults to 0. The client then samples
    whichever layer happened to land first, not the baked layout.

Both were invisible upstream and fatal downstream. This module reads the
exported bytes and the exported atlas, and nothing else — it is deliberately
not allowed to consult the bake report, because agreeing with the bake report
is what let these ship.
"""
from __future__ import annotations

from io import BytesIO
import json
import struct

import numpy as np

GLB_MAGIC = b"glTF"
JSON_CHUNK = 0x4E4F534A
BIN_CHUNK = 0x004E4942
## glTF's default when a material omits alphaMode.
DEFAULT_ALPHA_MODE = "OPAQUE"
## Share of sampled UVs that must land on a painted texel. Not 1.0: a UV on a
## tile seam can round onto the pad between tiles, and one stray corner is not
## worth failing a 100k-triangle bake over.
MIN_TEXTURED = 0.98

_COMPONENT = {5120: "i1", 5121: "u1", 5122: "i2", 5123: "u2", 5125: "u4", 5126: "f4"}
_COUNT = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def chunks(glb: bytes) -> tuple[dict, bytes] | None:
    """(JSON document, binary blob) of a GLB, or None when it is not one."""
    if len(glb) < 12 or glb[:4] != GLB_MAGIC:
        return None
    document: dict | None = None
    blob = b""
    offset = 12
    try:
        while offset + 8 <= len(glb):
            length, kind = struct.unpack_from("<II", glb, offset)
            offset += 8
            payload = glb[offset:offset + length]
            if len(payload) < length:
                return None
            offset += length
            if kind == JSON_CHUNK:
                document = json.loads(payload.decode("utf-8"))
            elif kind == BIN_CHUNK:
                blob = payload
    except (ValueError, struct.error):
        return None
    return (document, blob) if isinstance(document, dict) else None


def accessor(document: dict, blob: bytes, index: int) -> np.ndarray | None:
    """Accessor `index` as an (count, components) array, honouring byteStride.

    Returns None for anything this reader does not handle (sparse accessors,
    unknown component types) so a caller degrades to "not measured" rather than
    reporting a false violation.
    """
    try:
        spec = document["accessors"][index]
    except (KeyError, IndexError, TypeError):
        return None
    if "sparse" in spec or "bufferView" not in spec:
        return None
    dtype = _COMPONENT.get(spec.get("componentType"))
    components = _COUNT.get(spec.get("type"))
    if dtype is None or components is None:
        return None
    try:
        view = document["bufferViews"][spec["bufferView"]]
    except (KeyError, IndexError, TypeError):
        return None
    item = np.dtype("<" + dtype)
    start = view.get("byteOffset", 0) + spec.get("byteOffset", 0)
    count = int(spec.get("count", 0))
    stride = view.get("byteStride") or item.itemsize * components
    need = start + stride * (count - 1) + item.itemsize * components if count else start
    if count <= 0 or need > len(blob):
        return None
    rows = np.lib.stride_tricks.as_strided(
        np.frombuffer(blob, dtype=np.uint8, offset=start, count=need - start),
        shape=(count, item.itemsize * components), strides=(stride, 1))
    return rows.copy().view(item).reshape(count, components)


def _alpha_modes(document: dict) -> list[str]:
    materials = document.get("materials")
    if not isinstance(materials, list):
        return []
    return [str(m.get("alphaMode", DEFAULT_ALPHA_MODE)) if isinstance(m, dict)
            else DEFAULT_ALPHA_MODE for m in materials]


def _base_colour_texcoord(document: dict, material_index) -> int:
    """Which TEXCOORD set baseColorTexture reads. glTF defaults it to 0, and
    that default is half of why the second-UV-set bug was invisible."""
    try:
        material = document["materials"][material_index]
        texture = material["pbrMetallicRoughness"]["baseColorTexture"]
    except (KeyError, IndexError, TypeError):
        return 0
    return int(texture.get("texCoord", 0)) if isinstance(texture, dict) else 0


def _atlas_alpha(atlas: bytes):
    """Alpha channel of the atlas as a (h, w) array, or None if undecodable."""
    try:
        from PIL import Image

        with Image.open(BytesIO(atlas)) as image:
            return np.asarray(image.convert("RGBA"))[:, :, 3]
    except Exception:  # noqa: BLE001 — an unreadable atlas is "not measured"
        return None


def inspect(glb: bytes, atlas: bytes | None = None) -> dict:
    """What the exported bytes say about themselves.

    `textured` is the share of sampled UVs landing on a painted (non-zero
    alpha) atlas texel, sampled through the UV set baseColorTexture actually
    reads. It is None when there is no atlas to sample against — the check is
    then simply not claimed, never assumed to pass.
    """
    parsed = chunks(glb)
    if parsed is None:
        return {"parsed": False, "alpha_modes": [], "uv_sets": 0, "textured": None}
    document, blob = parsed
    report = {
        "parsed": True,
        "alpha_modes": _alpha_modes(document),
        "uv_sets": 0,
        "textured": None,
    }

    alpha = _atlas_alpha(atlas) if atlas else None
    hits = total = 0
    for mesh in document.get("meshes", []) or []:
        if not isinstance(mesh, dict):
            continue
        for primitive in mesh.get("primitives", []) or []:
            if not isinstance(primitive, dict):
                continue
            attributes = primitive.get("attributes")
            if not isinstance(attributes, dict):
                continue
            sets = sum(1 for key in attributes if key.startswith("TEXCOORD_"))
            report["uv_sets"] = max(report["uv_sets"], sets)
            if alpha is None:
                continue
            used = f"TEXCOORD_{_base_colour_texcoord(document, primitive.get('material'))}"
            if used not in attributes:
                continue
            uv = accessor(document, blob, attributes[used])
            if uv is None or uv.shape[1] < 2:
                continue
            height, width = alpha.shape
            # glTF UV origin is top-left, so v maps straight to the image row.
            x = np.clip((uv[:, 0] * width).astype(np.int64), 0, width - 1)
            y = np.clip((uv[:, 1] * height).astype(np.int64), 0, height - 1)
            hits += int(np.count_nonzero(alpha[y, x]))
            total += int(uv.shape[0])
    if total:
        report["textured"] = round(hits / total, 4)
    return report


def violations(report: dict, *, min_textured: float = MIN_TEXTURED) -> list[str]:
    """Human-readable reasons this GLB is unusable in a real-time renderer."""
    if not report.get("parsed"):
        return ["GLB is not parseable as glTF binary"]
    problems = []
    blended = [mode for mode in report.get("alpha_modes", []) if mode == "BLEND"]
    if blended:
        problems.append(
            f"{len(blended)} material(s) use alphaMode BLEND — the mesh draws in the "
            "transparent pass with no depth write")
    if report.get("uv_sets", 0) > 1:
        problems.append(
            f"{report['uv_sets']} UV sets exported — baseColorTexture reads one of them, "
            "so the client may sample an unbaked layout")
    if report.get("uv_sets", 0) < 1:
        problems.append("no TEXCOORD set exported — the atlas cannot be sampled")
    textured = report.get("textured")
    if textured is not None and textured < min_textured:
        problems.append(
            f"only {textured:.1%} of sampled UVs land on painted atlas texels "
            f"(need {min_textured:.0%}) — the mesh renders blank or black")
    return problems
