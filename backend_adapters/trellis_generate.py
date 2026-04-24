#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path

os.environ["SPCONV_ALGO"] = "native"

import imageio
from PIL import Image

from trellis.pipelines import TrellisImageTo3DPipeline
from trellis.utils import postprocessing_utils, render_utils


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--model-id", default="microsoft/TRELLIS-image-large")
    parser.add_argument("--texture-size", type=int, default=1024)
    parser.add_argument("--simplify", type=float, default=0.95)
    parser.add_argument("--skip-previews", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    pipeline = TrellisImageTo3DPipeline.from_pretrained(args.model_id)
    pipeline.cuda()
    image = Image.open(args.image)

    outputs = pipeline.run(image, seed=1)
    glb = postprocessing_utils.to_glb(
        outputs["gaussian"][0],
        outputs["mesh"][0],
        simplify=args.simplify,
        texture_size=args.texture_size,
    )
    glb.export(output_dir / "model.glb")
    outputs["gaussian"][0].save_ply(output_dir / "gaussian.ply")

    if not args.skip_previews:
        try:
            video = render_utils.render_video(outputs["gaussian"][0])["color"]
            imageio.mimsave(output_dir / "preview_gs.mp4", video, fps=30)
        except Exception:
            pass
        try:
            video = render_utils.render_video(outputs["mesh"][0])["normal"]
            imageio.mimsave(output_dir / "preview_mesh.mp4", video, fps=30)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
