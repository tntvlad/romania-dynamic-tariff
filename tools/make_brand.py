"""Generate brand images: Romania-shaped tricolor flag + lightning bolt.

Renders transparent PNGs for the integration's ``brand/`` folder and the
repository root:

  custom_components/romania_dynamic_tariff/brand/icon.png     256x256
  custom_components/romania_dynamic_tariff/brand/icon@2x.png  512x512
  custom_components/romania_dynamic_tariff/brand/logo.png     512x256
  custom_components/romania_dynamic_tariff/brand/logo@2x.png  1024x512
  icon.png / logo.png                                          repo root

The country outline comes from Natural Earth 1:10m (public domain), filtered
to Romania and stored next to this script as ``romania.geojson``.

Usage:  python tools/make_brand.py   (run from the repository root)
Requires: Pillow
"""
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
SRC = Path(__file__).resolve().parent / "romania.geojson"
BRAND_DIR = ROOT / "custom_components" / "romania_dynamic_tariff" / "brand"
SS = 4  # supersampling factor

# Official Romanian flag colours
BLUE = (0, 43, 127, 255)
YELLOW = (252, 209, 22, 255)
RED = (206, 17, 38, 255)
OUTLINE = (9, 26, 58, 255)
WHITE = (255, 255, 255, 255)


def load_romania_rings():
    data = json.loads(SRC.read_text())
    feat = next(
        f
        for f in data["features"]
        if f["properties"].get("ISO_A3") == "ROU"
        or f["properties"].get("ADMIN") == "Romania"
    )
    geom = feat["geometry"]
    if geom["type"] == "Polygon":
        return [geom["coordinates"][0]]
    if geom["type"] == "MultiPolygon":
        return [poly[0] for poly in geom["coordinates"]]
    raise ValueError(geom["type"])


def project(rings):
    """Equirectangular projection with latitude aspect correction."""
    lats = [lat for ring in rings for _, lat in ring]
    mean_lat = sum(lats) / len(lats)
    k = math.cos(math.radians(mean_lat))
    return [[(lon * k, -lat) for lon, lat in ring] for ring in rings]


def fit(proj, box_w, box_h, margin_frac):
    xs = [x for ring in proj for x, _ in ring]
    ys = [y for ring in proj for _, y in ring]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    w, h = maxx - minx, maxy - miny
    mx, my = box_w * margin_frac, box_h * margin_frac
    scale = min((box_w - 2 * mx) / w, (box_h - 2 * my) / h)
    ox = (box_w - w * scale) / 2 - minx * scale
    oy = (box_h - h * scale) / 2 - miny * scale
    pts = [[(x * scale + ox, y * scale + oy) for x, y in ring] for ring in proj]
    bbox = (minx * scale + ox, miny * scale + oy, maxx * scale + ox, maxy * scale + oy)
    return pts, bbox


def bolt_points(cx, cy, height):
    """Classic lightning bolt (M13 2 L3 14 L11 14 L9 22 L19 10 L12 10 Z)."""
    base = [(13, 2), (3, 14), (11, 14), (9, 22), (19, 10), (12, 10)]
    bw, bh = 16.0, 20.0
    s = height / bh
    ox = cx - (bw * s) / 2 - 3 * s
    oy = cy - (bh * s) / 2 - 2 * s
    return [(x * s + ox, y * s + oy) for x, y in base]


def render(box_w, box_h, margin_frac=0.06, bolt_frac=0.60):
    w, h = box_w * SS, box_h * SS
    rings = load_romania_rings()
    proj = project(rings)
    pts, bbox = fit(proj, w, h, margin_frac)
    bx0, by0, bx1, by1 = bbox
    country_h = by1 - by0

    # country mask (with holes)
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    for i, ring in enumerate(pts):
        md.polygon(ring, fill=255 if i == 0 else 0)

    # tricolor spanning the country's width
    tri = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    td = ImageDraw.Draw(tri)
    third = (bx1 - bx0) / 3
    td.rectangle([bx0, 0, bx0 + third, h], fill=BLUE)
    td.rectangle([bx0 + third, 0, bx0 + 2 * third, h], fill=YELLOW)
    td.rectangle([bx0 + 2 * third, 0, bx1, h], fill=RED)

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    img.paste(tri, (0, 0), mask)

    # outline around the country
    dilated = mask.filter(ImageFilter.MaxFilter(size=max(3, int(6 * SS) | 1)))
    ring = Image.new("L", (w, h), 0)
    rp, mp, dp = ring.load(), mask.load(), dilated.load()
    for y in range(h):
        for x in range(w):
            if dp[x, y] and not mp[x, y]:
                rp[x, y] = 255
    img.paste(Image.new("RGBA", (w, h), OUTLINE), (0, 0), ring)

    # lightning bolt: soft shadow + white with dark outline
    cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
    bpts = bolt_points(cx, cy, country_h * bolt_frac)

    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    off = country_h * 0.015
    sd.polygon([(x + off, y + off) for x, y in bpts], fill=(0, 0, 0, 90))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=country_h * 0.02))
    img.alpha_composite(shadow)

    d = ImageDraw.Draw(img)
    d.polygon(bpts, fill=WHITE, outline=OUTLINE, width=max(2, int(country_h * 0.02)))

    return img.resize((box_w, box_h), Image.LANCZOS)


def main():
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    icon = render(256, 256, margin_frac=0.07)
    logo = render(512, 256, margin_frac=0.05, bolt_frac=0.58)

    outputs = {
        BRAND_DIR / "icon.png": icon,
        BRAND_DIR / "icon@2x.png": render(512, 512, margin_frac=0.07),
        BRAND_DIR / "logo.png": logo,
        BRAND_DIR / "logo@2x.png": render(1024, 512, margin_frac=0.05, bolt_frac=0.58),
        ROOT / "icon.png": icon,
        ROOT / "logo.png": logo,
    }
    for path, im in outputs.items():
        im.save(path)
        print(f"wrote {path.relative_to(ROOT)}  {im.size} {path.stat().st_size} bytes")


if __name__ == "__main__":
    main()
