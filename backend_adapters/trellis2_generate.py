#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path

os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
os.environ["OPENCV_IO_ENABLE_OPENEXR"] = "1"

import imageio
from PIL import Image
import torch

from trellis2.pipelines import Trellis2ImageTo3DPipeline
from trellis2.utils import render_utils
import o_voxel


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--model-id", default="microsoft/TRELLIS.2-4B")
    parser.add_argument("--texture-size", type=int, default=4096)
    parser.add_argument("--decimation-target", type=int, default=1000000)
    parser.add_argument("--remesh", action="store_true")
    parser.add_argument("--skip-preview", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    pipeline = Trellis2ImageTo3DPipeline.from_pretrained(args.model_id)
    pipeline.cuda()

    image = Image.open(args.image)
    mesh = pipeline.run(image)[0]
    try:
        mesh.simplify(16777216)
    except Exception:
        pass

    glb = o_voxel.postprocess.to_glb(
        vertices=mesh.vertices,
        faces=mesh.faces,
        attr_volume=mesh.attrs,
        coords=mesh.coords,
        attr_layout=mesh.layout,
        voxel_size=mesh.voxel_size,
        aabb=[[-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]],
        decimation_target=args.decimation_target,
        texture_size=args.texture_size,
        remesh=args.remesh,
        remesh_band=1,
        remesh_project=0,
        verbose=True,
    )
    glb.export(output_dir / "model.glb", extension_webp=True)

    if not args.skip_preview:
        try:
            video = render_utils.render_video(mesh)
            frames = render_utils.make_pbr_vis_frames(video)
            imageio.mimsave(output_dir / "preview.mp4", frames, fps=15)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
