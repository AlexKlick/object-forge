import argparse
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector


def parse_args() -> argparse.Namespace:
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:
        argv = []
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--views", type=int, default=8)
    parser.add_argument("--resolution", type=int, default=512)
    parser.add_argument("--elevation-deg", type=float, default=18.0)
    parser.add_argument("--radius", type=float, default=3.0)
    parser.add_argument("--engine", default="CYCLES")
    parser.add_argument("--transparent-background", action="store_true")
    return parser.parse_args(argv)


def clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for datablock_collection in [bpy.data.meshes, bpy.data.materials, bpy.data.images, bpy.data.cameras, bpy.data.lights]:
        for datablock in list(datablock_collection):
            if datablock.users == 0:
                datablock_collection.remove(datablock)


def import_asset(path: str) -> None:
    suffix = Path(path).suffix.lower()
    if suffix in {".glb", ".gltf"}:
        bpy.ops.import_scene.gltf(filepath=path)
        return
    if suffix == ".obj":
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=path)
        else:
            bpy.ops.import_scene.obj(filepath=path)
        return
    if suffix == ".ply":
        if hasattr(bpy.ops.wm, "ply_import"):
            bpy.ops.wm.ply_import(filepath=path)
        else:
            bpy.ops.import_mesh.ply(filepath=path)
        return
    raise ValueError(f"Unsupported asset type: {suffix}")


def imported_mesh_objects():
    return [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]


def normalize_scene(mesh_objects) -> None:
    bpy.context.view_layer.update()
    bbox_points = []
    for obj in mesh_objects:
        for corner in obj.bound_box:
            bbox_points.append(obj.matrix_world @ Vector(corner))
    mins = Vector((min(p.x for p in bbox_points), min(p.y for p in bbox_points), min(p.z for p in bbox_points)))
    maxs = Vector((max(p.x for p in bbox_points), max(p.y for p in bbox_points), max(p.z for p in bbox_points)))
    center = (mins + maxs) / 2.0
    size = max(maxs.x - mins.x, maxs.y - mins.y, maxs.z - mins.z)
    scale = 1.6 / max(size, 1e-6)

    for obj in mesh_objects:
        obj.location -= center
        obj.scale *= scale
    bpy.context.view_layer.update()


def create_camera(radius: float, elevation_deg: float):
    cam_data = bpy.data.cameras.new("SpriteCamera")
    cam = bpy.data.objects.new("SpriteCamera", cam_data)
    bpy.context.collection.objects.link(cam)
    bpy.context.scene.camera = cam
    cam.location = (radius, 0.0, radius * math.sin(math.radians(elevation_deg)))
    return cam


def track_camera_to_origin(camera):
    constraint = camera.constraints.new(type="TRACK_TO")
    target = bpy.data.objects.new("CameraTarget", None)
    target.location = (0.0, 0.0, 0.0)
    bpy.context.collection.objects.link(target)
    constraint.target = target
    constraint.track_axis = "TRACK_NEGATIVE_Z"
    constraint.up_axis = "UP_Y"


def add_lights() -> None:
    key_data = bpy.data.lights.new(name="KeyLight", type="AREA")
    key_data.energy = 3000
    key = bpy.data.objects.new(name="KeyLight", object_data=key_data)
    key.location = (3.0, -3.0, 4.0)
    bpy.context.collection.objects.link(key)

    fill_data = bpy.data.lights.new(name="FillLight", type="AREA")
    fill_data.energy = 1500
    fill = bpy.data.objects.new(name="FillLight", object_data=fill_data)
    fill.location = (-3.0, 2.0, 2.5)
    bpy.context.collection.objects.link(fill)


def configure_render(output_dir: Path, resolution: int, engine: str, transparent: bool) -> None:
    scene = bpy.context.scene
    if engine == "CYCLES":
        try:
            scene.render.engine = "CYCLES"
        except Exception:
            scene.render.engine = "BLENDER_EEVEE"
        else:
            cycles = getattr(scene, "cycles", None)
            if cycles is not None:
                cycles.samples = 64
                if hasattr(cycles, "use_adaptive_sampling"):
                    cycles.use_adaptive_sampling = False
                # Disable built-in denoising without assigning invalid denoiser enum values.
                if hasattr(cycles, "use_denoising"):
                    cycles.use_denoising = False
    else:
        scene.render.engine = engine
    scene.render.resolution_x = resolution
    scene.render.resolution_y = resolution
    scene.render.image_settings.file_format = "PNG"
    scene.render.film_transparent = transparent
    output_dir.mkdir(parents=True, exist_ok=True)


def render_frames(camera, views: int, radius: float, elevation_deg: float, output_dir: Path) -> None:
    elev_z = radius * math.sin(math.radians(elevation_deg))
    xy_radius = radius * math.cos(math.radians(elevation_deg))
    for index in range(views):
        angle = 2.0 * math.pi * index / max(views, 1)
        camera.location = (
            xy_radius * math.cos(angle),
            xy_radius * math.sin(angle),
            elev_z,
        )
        bpy.context.scene.render.filepath = str(output_dir / f"frame_{index:03d}.png")
        bpy.ops.render.render(write_still=True)


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    clear_scene()
    import_asset(args.input)
    mesh_objects = imported_mesh_objects()
    if not mesh_objects:
        raise RuntimeError("No mesh objects imported.")
    normalize_scene(mesh_objects)
    camera = create_camera(args.radius, args.elevation_deg)
    track_camera_to_origin(camera)
    add_lights()
    configure_render(output_dir, args.resolution, args.engine, args.transparent_background)
    render_frames(camera, args.views, args.radius, args.elevation_deg, output_dir)


if __name__ == "__main__":
    main()
