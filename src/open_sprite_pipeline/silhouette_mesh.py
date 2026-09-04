from __future__ import annotations

from pathlib import Path
import shutil
from typing import Any

import numpy as np
from PIL import Image

from .io_utils import ensure_dir, sha256_file


def _face(indices: list[int], texture_indices: list[int] | None = None) -> str:
    if texture_indices is None:
        return "f " + " ".join(str(index) for index in indices)
    return "f " + " ".join(
        f"{vertex}/{texture}" for vertex, texture in zip(indices, texture_indices)
    )


def create_silhouette_mesh(
    cutout_path: str | Path,
    output_dir: str | Path,
    *,
    grid_size: int = 72,
    depth: float = 0.16,
) -> dict[str, Any]:
    """Create a textured, extruded OBJ preview directly from a cutout alpha mask."""

    if grid_size < 16 or grid_size > 160:
        raise ValueError("grid_size must be between 16 and 160.")
    if depth <= 0 or depth > 2:
        raise ValueError("depth must be greater than 0 and at most 2.")

    destination = ensure_dir(output_dir).resolve()
    source = Image.open(cutout_path).convert("RGBA")
    if source.width >= source.height:
        width = grid_size
        height = max(1, round(source.height * grid_size / source.width))
    else:
        height = grid_size
        width = max(1, round(source.width * grid_size / source.height))

    reduced = source.resize((width, height), Image.Resampling.LANCZOS)
    alpha = np.asarray(reduced.getchannel("A"))
    occupied = alpha >= 48
    if not occupied.any():
        raise ValueError("The selected cutout has no opaque pixels to extrude.")

    model_path = destination / "model.obj"
    material_path = destination / "model.mtl"
    texture_path = destination / "texture.png"
    shutil.copyfile(cutout_path, texture_path)

    scale = 2.0 / max(width, height)
    half_depth = depth / 2.0
    lines = [
        "# Open Sprite Pipeline deterministic silhouette preview",
        "mtllib model.mtl",
        "o extracted_object",
        "usemtl cutout",
    ]
    texture_lines: list[str] = []
    face_lines: list[str] = []
    vertex_index = 1
    texture_index = 1
    cell_count = 0
    face_count = 0

    def is_occupied(x: int, y: int) -> bool:
        return 0 <= x < width and 0 <= y < height and bool(occupied[y, x])

    for y in range(height):
        for x in range(width):
            if not occupied[y, x]:
                continue
            cell_count += 1
            left = (x - width / 2.0) * scale
            right = (x + 1 - width / 2.0) * scale
            top = (height / 2.0 - y) * scale
            bottom = (height / 2.0 - (y + 1)) * scale
            vertices = [
                (left, bottom, half_depth),
                (right, bottom, half_depth),
                (right, top, half_depth),
                (left, top, half_depth),
                (left, bottom, -half_depth),
                (right, bottom, -half_depth),
                (right, top, -half_depth),
                (left, top, -half_depth),
            ]
            lines.extend(f"v {vx:.6f} {vy:.6f} {vz:.6f}" for vx, vy, vz in vertices)

            u1 = x / width
            u2 = (x + 1) / width
            v1 = 1.0 - (y + 1) / height
            v2 = 1.0 - y / height
            texture_lines.extend(
                [
                    f"vt {u1:.6f} {v1:.6f}",
                    f"vt {u2:.6f} {v1:.6f}",
                    f"vt {u2:.6f} {v2:.6f}",
                    f"vt {u1:.6f} {v2:.6f}",
                ]
            )
            front = [vertex_index + offset for offset in (0, 1, 2, 3)]
            back = [vertex_index + offset for offset in (7, 6, 5, 4)]
            uv = [texture_index + offset for offset in (0, 1, 2, 3)]
            face_lines.append(_face(front, uv))
            face_lines.append(_face(back, [uv[3], uv[2], uv[1], uv[0]]))
            face_count += 2

            boundary_faces = [
                (not is_occupied(x - 1, y), [0, 3, 7, 4]),
                (not is_occupied(x + 1, y), [1, 5, 6, 2]),
                (not is_occupied(x, y - 1), [3, 2, 6, 7]),
                (not is_occupied(x, y + 1), [0, 4, 5, 1]),
            ]
            for exposed, offsets in boundary_faces:
                if exposed:
                    face_lines.append(_face([vertex_index + offset for offset in offsets]))
                    face_count += 1

            vertex_index += 8
            texture_index += 4

    model_path.write_text(
        "\n".join([*lines, *texture_lines, *face_lines, ""]),
        encoding="utf-8",
    )
    material_path.write_text(
        "\n".join(
            [
                "newmtl cutout",
                "Ka 1.000000 1.000000 1.000000",
                "Kd 1.000000 1.000000 1.000000",
                "Ks 0.050000 0.050000 0.050000",
                "Ns 20.000000",
                "d 1.000000",
                "illum 2",
                "map_Kd texture.png",
                "map_d texture.png",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return {
        "provider_id": "silhouette_extrusion",
        "primary_asset_path": str(model_path),
        "material_path": str(material_path),
        "texture_path": str(texture_path),
        "grid_width": width,
        "grid_height": height,
        "cell_count": cell_count,
        "face_count": face_count,
        "depth": depth,
        "sha256": sha256_file(model_path),
        "quality_boundary": "2.5D silhouette preview; not inferred full-volume geometry",
    }
