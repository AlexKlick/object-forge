#!/usr/bin/env python3
"""Submit a blockout view to the style sidecar; stdlib + Pillow only."""
from __future__ import annotations

import argparse
import base64
import io
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

from PIL import Image


def png_b64(path: Path) -> str:
    raw = path.read_bytes()
    with Image.open(io.BytesIO(raw)) as image:
        if image.format != "PNG":
            raise ValueError(f"not a PNG: {path}")
        image.verify()
    return base64.b64encode(raw).decode("ascii")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render", required=True, type=Path)
    parser.add_argument("--depth", type=Path)
    parser.add_argument("--ref", action="append", required=True, type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:8056")
    parser.add_argument("--seeds", default="11,22,33")
    parser.add_argument("--prompt", default="hand-painted game prop")
    parser.add_argument("--negative", default="")
    parser.add_argument("--long-side", default=768, type=int)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    depth = args.depth or args.render.parent / "passes" / f"{args.render.stem}.depth.png"
    try:
        payload = {
            "view": args.render.stem, "init_png": png_b64(args.render),
            "depth_png": png_b64(depth), "refs": [png_b64(ref) for ref in args.ref],
            "prompt": args.prompt, "negative": args.negative,
            "seeds": [int(seed.strip()) for seed in args.seeds.split(",")],
            "long_side": args.long_side,
        }
        request = urllib.request.Request(
            args.url.rstrip("/") + "/v1/style/render", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=1800) as response:
            result = json.load(response)
        seconds = time.perf_counter() - started
        args.out.mkdir(parents=True, exist_ok=True)
        for candidate in result["candidates"]:
            seed = int(candidate["seed"])
            raw = base64.b64decode(candidate["png"], validate=True)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format != "PNG" or image.mode != "RGBA":
                    raise ValueError("sidecar returned a non-RGBA PNG candidate")
                image.verify()
            (args.out / f"{args.render.stem}-{seed}.png").write_bytes(raw)
        print(json.dumps({
            "seconds": seconds, "peak_mb": result["peak_mb"],
            "prompt_tokens": result["prompt_tokens"], "truncated": result["truncated"],
            "box": result["box"],
            "working_size": [item["working_size"] for item in result["candidates"]],
        }))
        return 0
    except urllib.error.HTTPError as error:
        print(error.read().decode("utf-8", errors="replace"))
        return 2 if error.code == 503 else 1
    except (OSError, ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
