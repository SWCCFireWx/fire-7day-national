#!/usr/bin/env python3
"""
Read PSA categories (Above / Normal / Below) off the official NIFC outlook map images.

The NIFC archive keeps past outlooks only as images (PNG) and PDFs, so this lines each map up with
the PSA boundaries and reads the color inside every PSA:
  * the lower 48 are drawn in an Albers equal-area projection; the scale and position are fitted to the
    map's own PSA and GACC boundary lines (bright blue and navy), and checked by how closely the PSA
    outlines land on them;
  * Alaska and Puerto Rico are insets, fitted the same way in their corners of the map;
  * each PSA is shrunk a little (to stay off the boundary lines) and its pixels counted:
    mostly red = Above, mostly green = Below, otherwise Normal.
A fit that doesn't line up (mean distance from the lines too large) is reported and that map skipped,
rather than guessed. Needs numpy, scipy and Pillow.

Used by import_archive.py. Run on its own to check a map:
    python scripts/read_outlook_maps.py data/raw/outlooks/2025-07/month_1.png [--overlay out.png]
"""

import json
import math
import os
import sys

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage, optimize

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gaccs  # noqa: E402

PSA_GEO = os.path.join(gaccs.DATA, "psa_polygons.geojson")
MAX_FIT = 4.0          # mean distance (pixels on a 3300-wide map) from the boundary lines for a fit to be trusted
INSET_MAX_FIT = 6.0
PR_PSAS = ("SA52",)    # Puerto Rico PSA codes start with this


def albers(lat1, lat2, lat0, lon0):
    p1, p2, p0 = map(math.radians, (lat1, lat2, lat0))
    n = (math.sin(p1) + math.sin(p2)) / 2
    C = math.cos(p1) ** 2 + 2 * n * math.sin(p1)
    r0 = math.sqrt(C - 2 * n * math.sin(p0)) / n

    def f(lon, lat):
        lon = np.where(np.asarray(lon) > 0, np.asarray(lon) - 360, lon)
        r = np.sqrt(C - 2 * n * np.sin(np.radians(lat))) / n
        th = n * np.radians(lon - lon0)
        return r * np.sin(th), r0 - r * np.cos(th)
    return f


def mercator(lon, lat):
    return np.radians(lon), np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))


CONUS = albers(29.5, 45.5, 37.5, -96)
ALASKA = albers(55, 65, 50, -154)


def load_psas():
    with open(PSA_GEO) as fh:
        return {f["properties"]["psa"]: f["geometry"] for f in json.load(fh)["features"]}


def group_of(psa):
    if psa.startswith("AK"):
        return "ak"
    if psa.startswith(PR_PSAS):
        return "pr"
    return "conus"


def rings(geom):
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    return [ring for poly in polys for ring in poly]


class MapImage:
    def __init__(self, path):
        im = Image.open(path).convert("RGB")
        self.img = np.asarray(im).astype(np.int16)
        self.H, self.W = self.img.shape[:2]
        self.k = self.W / 3300.0
        r, g, b = self.img[..., 0], self.img[..., 1], self.img[..., 2]
        self.line = (b > 90) & (r < 90) & (g < 150)                       # navy GACC and blue PSA boundaries
        self.dist = ndimage.distance_transform_edt(~self.line)
        self.red = (r > 170) & (g < 90) & (b < 90)
        self.green = (g > 160) & (r < 150) & (b < 100)

    def score(self, x, y, p, region=None):
        s, tx, ty = p
        px, py = x * s + tx, -y * s + ty
        ok = (px >= 0) & (px < self.W - 1) & (py >= 0) & (py < self.H - 1)
        cap = 30 * self.k
        d = np.full(px.shape, cap)
        d[ok] = self.dist[py[ok].astype(int), px[ok].astype(int)]
        return float(np.mean(np.minimum(d, cap)))

    def fit(self, x, y, p0, steps):
        best = None
        for ds in steps[0]:
            for dx in steps[1]:
                for dy in steps[2]:
                    q = [p0[0] * ds, p0[1] + dx * self.k, p0[2] + dy * self.k]
                    r = optimize.minimize(lambda p: self.score(x, y, p), q, method="Nelder-Mead",
                                          options={"xatol": .01, "fatol": 1e-4, "maxiter": 800})
                    if best is None or r.fun < best.fun:
                        best = r
        return list(best.x), best.fun / self.k


def points(psas, group, proj):
    pts = [pt for code, g in psas.items() if group_of(code) == group for ring in rings(g) for pt in ring]
    if not pts:
        return None, None
    a = np.array(pts)
    if group == "conus":
        a = a[(a[:, 1] > 24) & (a[:, 0] < -66) & (a[:, 0] > -125.5)]
    return proj(a[:, 0], a[:, 1])


def fit_conus(m, psas, guess=None):
    x, y = points(psas, "conus", CONUS)
    if guess:
        p, f = m.fit(x, y, guess, ((1.0,), (0,), (0,)))
        if f <= MAX_FIT:
            return p, f
    band = m.line[int(.12 * m.H):int(.40 * m.H), int(.08 * m.W):int(.95 * m.W)]
    ys, xs = np.nonzero(band)
    xs = xs + int(.08 * m.W)
    ix0, ix1 = np.percentile(xs, .2), np.percentile(xs, 99.8)
    top = np.nonzero(m.line[:, int(.3 * m.W):int(.6 * m.W)].any(axis=1))[0]
    iy0 = top[top > .03 * m.H].min()
    s0 = (ix1 - ix0) / (x.max() - x.min())
    p0 = [s0, ix0 - x.min() * s0, iy0 + y.max() * s0]
    return m.fit(x, y, p0, ((0.97, 1.0, 1.03), (0,), (-40, 0, 40)))


def fit_inset(m, psas, group, region, proj):
    """Fit an inset (Alaska, Puerto Rico) inside region = (x0, x1, y0, y1) as fractions of the map."""
    x, y = points(psas, group, proj)
    if x is None:
        return None, None
    x0, x1, y0, y1 = int(region[0] * m.W), int(region[1] * m.W), int(region[2] * m.H), int(region[3] * m.H)
    ys, xs = np.nonzero(m.line[y0:y1, x0:x1])
    if len(xs) < 50:
        return None, None
    xs, ys = xs + x0, ys + y0
    bx0, bx1, by0, by1 = np.percentile(xs, 1), np.percentile(xs, 99), np.percentile(ys, 1), np.percentile(ys, 99)
    s0 = min((bx1 - bx0) / (x.max() - x.min()), (by1 - by0) / (y.max() - y.min()))
    p0 = [s0, bx0 - x.min() * s0, by0 + y.max() * s0]
    return m.fit(x, y, p0, ((0.85, 0.95, 1.05, 1.15), (-30, 0, 30), (-30, 0, 30)))


def classify(m, psas, fits):
    """{psa: (category, red share, green share)} for PSAs whose group has a trusted fit."""
    out = {}
    projs = {"conus": CONUS, "ak": ALASKA, "pr": mercator}
    erode = max(1, int(round(6 * m.k)))
    for code, geom in psas.items():
        grp = group_of(code)
        if fits.get(grp) is None:
            continue
        s, tx, ty = fits[grp]
        polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
        shapes = []
        for poly in polys:
            for i, ring in enumerate(poly):
                if len(ring) < 3:
                    continue
                a = np.array(ring)
                X, Y = projs[grp](a[:, 0], a[:, 1])
                shapes.append((i, X * s + tx, -Y * s + ty))
        if not shapes:
            continue
        x0 = max(0, int(min(sx.min() for _, sx, _ in shapes)) - 2); x1 = min(m.W, int(max(sx.max() for _, sx, _ in shapes)) + 3)
        y0 = max(0, int(min(sy.min() for _, _, sy in shapes)) - 2); y1 = min(m.H, int(max(sy.max() for _, _, sy in shapes)) + 3)
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        mask = Image.new("L", (x1 - x0, y1 - y0), 0)
        d = ImageDraw.Draw(mask)
        for i, sx, sy in shapes:
            d.polygon([(float(a) - x0, float(b) - y0) for a, b in zip(sx, sy)], fill=0 if i else 1)
        mk = np.asarray(mask, bool)
        if not mk.any():
            continue
        inner = ndimage.binary_erosion(mk, iterations=erode)
        mk = inner if inner.sum() >= 30 else mk
        red_a, green_a = m.red[y0:y1, x0:x1], m.green[y0:y1, x0:x1]
        n = mk.sum()
        red, green = red_a[mk].sum() / n, green_a[mk].sum() / n
        cat = "Above" if red >= 0.35 and red > green else "Below" if green >= 0.35 else "Normal"
        out[code] = (cat, round(float(red), 2), round(float(green), 2))
    return out


def read_puerto_rico(m, region=(.83, 1.0, .48, .70)):
    """Puerto Rico is one small island in its own corner: find the outlined island and read its fill."""
    x0, x1, y0, y1 = int(region[0] * m.W), int(region[1] * m.W), int(region[2] * m.H), int(region[3] * m.H)
    line = m.line[y0:y1, x0:x1]
    filled = ndimage.binary_fill_holes(line)
    inside = filled & ~line
    lab, n = ndimage.label(inside)
    if not n:
        return None
    sizes = ndimage.sum(inside, lab, range(1, n + 1))
    big = lab == (int(np.argmax(sizes)) + 1)
    if big.sum() < 40 * m.k * m.k:
        return None
    red, green = m.red[y0:y1, x0:x1][big].mean(), m.green[y0:y1, x0:x1][big].mean()
    return ("Above" if red >= 0.35 and red > green else "Below" if green >= 0.35 else "Normal", round(float(red), 2), round(float(green), 2))


_CACHE = {}


def read_map(path, psas=None, overlay=None):
    """({psa: category}, notes) for one US outlook map image."""
    psas = psas or load_psas()
    m = MapImage(path)
    key = (m.W, m.H)
    fits, notes = {}, []
    reused = False
    p, f = fit_conus(m, psas, _CACHE.get(key, {}).get("conus"))
    if f > MAX_FIT:
        if not _CACHE.get(key, {}).get("conus"):
            return {}, [f"{os.path.basename(path)}: map didn't line up with the PSA boundaries (off by {f:.1f} px); skipped"]
        # some maps are drawn without PSA lines (GACC and state lines only): use the alignment found on
        # other maps of the same layout (same image size) in this run
        p, reused = _CACHE[key]["conus"], True
        notes.append("no PSA lines on this map; used the alignment from other maps of the same layout")
    fits["conus"] = p
    for grp, region, proj in (("ak", (0, .36, .55, .98), ALASKA),):
        cached = _CACHE.get(key, {}).get(grp)
        if cached:
            x, y = points(psas, grp, proj)
            if x is not None and m.score(x, y, cached) / m.k <= INSET_MAX_FIT:
                fits[grp] = cached
                continue
        q, g = fit_inset(m, psas, grp, region, proj)
        if q is not None and g <= INSET_MAX_FIT:
            fits[grp] = q
        elif reused and cached:
            fits[grp] = cached
        elif q is not None:
            notes.append(f"{'Alaska' if grp == 'ak' else 'Puerto Rico'} inset didn't line up (off by {g:.1f} px); left out")
    if not reused:
        _CACHE[key] = {**_CACHE.get(key, {}), **fits}
    res = classify(m, psas, fits)
    pr = read_puerto_rico(m)
    for code in psas:
        if group_of(code) == "pr" and pr:
            res[code] = pr
    if overlay:
        im = Image.open(path).convert("RGB")
        d = ImageDraw.Draw(im)
        projs = {"conus": CONUS, "ak": ALASKA, "pr": mercator}
        for code, geom in psas.items():
            grp = group_of(code)
            if grp not in fits:
                continue
            s, tx, ty = fits[grp]
            for ring in rings(geom):
                a = np.array(ring)
                X, Y = projs[grp](a[:, 0], a[:, 1])
                d.line([(float(a), float(b)) for a, b in zip(X * s + tx, -Y * s + ty)], fill=(255, 0, 255), width=max(1, int(2 * m.k)))
        for code, (cat, _, _) in res.items():
            if cat != "Normal" and group_of(code) in fits:
                grp = group_of(code)
                s, tx, ty = fits[grp]
                a = np.array(rings(psas[code])[0])
                X, Y = projs[grp](a[:, 0], a[:, 1])
                d.text((float(np.mean(X * s + tx)), float(np.mean(-Y * s + ty))), f"{code} {cat[0]}", fill=(0, 0, 0))
        im.save(overlay)
    notes.insert(0, f"{os.path.basename(path)}: read from the map image ({'layout reused' if reused else f'fit {f:.1f} px'}; "
                    f"{sum(1 for v in res.values() if v[0] == 'Above')} Above, {sum(1 for v in res.values() if v[0] == 'Below')} Below)")
    return {k: v[0] for k, v in res.items()}, notes


if __name__ == "__main__":
    ov = sys.argv[sys.argv.index("--overlay") + 1] if "--overlay" in sys.argv else None
    cats, notes = read_map(sys.argv[1], overlay=ov)
    print("\n".join(notes))
    for k, v in sorted(cats.items()):
        if v != "Normal":
            print(k, v)
