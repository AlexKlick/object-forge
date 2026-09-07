# Source: /home/alexk/debt-city-greybox-spike/apps/greybox/assets/tools/style_compose.py
# keep byte-identical below this line
"""Crop blockout inputs for styling and restore the exact source silhouette."""
from __future__ import annotations

import math

from PIL import Image, ImageChops, ImageFilter


def _alpha(mask: Image.Image) -> Image.Image:
    return mask.getchannel("A") if "A" in mask.getbands() else mask.convert("L")


def crop_box(mask: Image.Image, *, margin: float = 0.06,
             multiple: int = 64) -> tuple[int, int, int, int]:
    if not math.isfinite(margin) or margin < 0 or not isinstance(multiple, int) or multiple <= 0:
        raise ValueError("margin must be nonnegative and multiple a positive integer")
    bbox = _alpha(mask).point(lambda p: 255 if p > 8 else 0).getbbox()
    if bbox is None:
        raise ValueError("empty mask")
    left, top, right, bottom = bbox
    pad = margin * max(right - left, bottom - top)
    left, top = math.floor(left - pad), math.floor(top - pad)
    right, bottom = math.ceil(right + pad), math.ceil(bottom + pad)
    dx = (-(right - left)) % multiple
    dy = (-(bottom - top)) % multiple
    left, right = left - dx // 2, right + dx - dx // 2
    top, bottom = top - dy // 2, bottom + dy - dy // 2
    # Canvas bounds take precedence when outward growth reaches an edge.
    return max(0, left), max(0, top), min(mask.width, right), min(mask.height, bottom)


def _bleed_foreground(image: Image.Image, pixels: int) -> Image.Image:
    """Extend nearest foreground RGB outward, retaining every alpha byte.

    Eight-neighbour dilation uses Chebyshev distance and a fixed tie order.
    Only transparent pixels receive colour; each iteration advances one pixel.
    """
    if not isinstance(pixels, int) or pixels < 0:
        raise ValueError("bleed must be a nonnegative integer")
    result = image.convert("RGBA")
    alpha = result.getchannel("A")
    # Antialiased edge pixels also have valid straight RGB. Treat them as
    # seeds so a ring of partial alpha cannot block colour propagation.
    known = alpha.point(lambda p: 255 if p > 0 else 0)
    transparent = alpha.point(lambda p: 255 if p == 0 else 0)
    rgb = result.convert("RGB")
    for _ in range(pixels):
        next_rgb, next_known = rgb.copy(), known.copy()
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1),
                       (-1, -1), (1, -1), (-1, 1), (1, 1)):
            shifted_mask = Image.new("L", image.size)
            shifted_mask.paste(known, (dx, dy))
            fill = ImageChops.multiply(shifted_mask, ImageChops.invert(next_known))
            fill = ImageChops.multiply(fill, transparent)
            shifted_rgb = Image.new("RGB", image.size)
            shifted_rgb.paste(rgb, (dx, dy))
            next_rgb.paste(shifted_rgb, (0, 0), fill)
            next_known = ImageChops.lighter(next_known, fill)
        rgb, known = next_rgb, next_known
    rgb.putalpha(alpha)
    return rgb


def to_working(init: Image.Image, depth: Image.Image, box, *, long_side: int = 768,
               background=(128, 128, 128), bleed: int = 8
               ) -> tuple[Image.Image, Image.Image, float]:
    if init.size != depth.size:
        raise ValueError("init and depth sizes must agree")
    if not isinstance(long_side, int) or long_side <= 0 or long_side % 8:
        raise ValueError("long_side must be a positive multiple of 8")
    left, top, right, bottom = box
    if not (0 <= left < right <= init.width and 0 <= top < bottom <= init.height):
        raise ValueError("box must be nonempty and inside the images")
    cropped = init.convert("RGBA").crop(box)
    cropped = _bleed_foreground(cropped, bleed)
    if bleed:
        # RGB beneath alpha=0 alone would disappear in alpha_composite. Give
        # the conditioning fringe an opaque matte, keeping the source alpha
        # untouched for from_working. Distant surroundings stay neutral.
        alpha = cropped.getchannel("A")
        support = alpha.point(lambda p: 255 if p > 0 else 0).filter(
            ImageFilter.MaxFilter(2 * bleed + 1))
        fringe = ImageChops.multiply(support, alpha.point(lambda p: 255 if p == 0 else 0))
        cropped.putalpha(ImageChops.lighter(alpha, fringe))
    rgb = Image.new("RGBA", cropped.size, (*background, 255))
    rgb = Image.alpha_composite(rgb, cropped).convert("RGB")
    scale = long_side / max(cropped.size)
    size = tuple(max(8, int(dim * scale / 8 + 0.5) * 8) for dim in cropped.size)
    depth_crop = depth.crop(box)
    if depth.mode in ("I", "I;16", "I;16L", "I;16B"):
        # PNG depth is normalized uint16, not an 8-bit image to saturate at 255.
        depth_crop = depth_crop.convert("F").point(lambda p: p * (255 / 65535))
    depth_l = depth_crop.resize(size, Image.Resampling.BILINEAR).convert("L")
    return rgb.resize(size, Image.Resampling.LANCZOS), depth_l, scale


def from_working(styled: Image.Image, box, *, frame_size: int = 2048,
                 mask: Image.Image) -> Image.Image:
    if mask.size != (frame_size, frame_size):
        raise ValueError("mask size must match the square output frame")
    left, top, right, bottom = box
    if not (0 <= left < right <= frame_size and 0 <= top < bottom <= frame_size):
        raise ValueError("box must be nonempty and inside the frame")
    restored = styled.convert("RGB").resize((right - left, bottom - top), Image.Resampling.LANCZOS)
    frame = Image.new("RGBA", (frame_size, frame_size), (0, 0, 0, 0))
    frame.paste(restored, (left, top))
    frame.putalpha(_alpha(mask))
    return frame
