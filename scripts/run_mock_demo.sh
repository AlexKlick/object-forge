#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

python3 - <<'PY'
from pathlib import Path
from PIL import Image, ImageDraw
out = Path("tests/data")
out.mkdir(parents=True, exist_ok=True)
image = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((96, 96, 416, 416), radius=48, fill=(120, 180, 220, 255))
draw.rectangle((220, 150, 292, 360), fill=(220, 90, 70, 255))
image.save(out / "mock_input.png")
PY

python3 -m open_sprite_pipeline.cli run \
  --config configs/app.example.yaml \
  --image tests/data/mock_input.png \
  --prompt "toy robot." \
  --mode hero \
  --mock
