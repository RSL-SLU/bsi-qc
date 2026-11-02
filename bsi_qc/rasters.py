"""Fetch per-operation COGs (TCI, BSI-E, indices) from the public bucket, cache them, and render PNG overlays.

The browser never talks to Google Cloud Storage directly (that would need CORS on the bucket); the local
server fetches the small tiles and hands back a PNG plus its lat/lon bounds.
"""
import base64
import hashlib
import io
import json
import os
import threading
import urllib.request

import numpy as np
import tifffile
from PIL import Image

LAYERS = {
    # name: (asset key, colour ramp, fixed range or None for percentile stretch)
    "tci": ("asset:tci", None, None),
    "bsi-e": ("asset:bsi-e", "bsi", None),
    "bsi-mask": ("asset:bsi-e", None, None),  # binary: BSI-E > the operation's bsi:threshold
    "ndvi": ("asset:ndvi", "veg", (-0.2, 0.9)),
    "savi": ("asset:savi", "veg", (-0.2, 0.8)),
    "ndbi": ("asset:ndbi", "div", (-0.5, 0.5)),
    "ndci": ("asset:ndci", "chl", None),
}

RAMPS = {
    # reversed: low BSI-E (ponds are dark targets) is the bright end
    "bsi": [(0.0, (240, 249, 33)), (0.2, (248, 149, 64)), (0.45, (204, 71, 120)), (0.7, (126, 3, 168)), (1.0, (13, 8, 135))],
    "veg": [(0.0, (140, 81, 10)), (0.35, (223, 194, 125)), (0.55, (199, 234, 229)), (0.75, (90, 180, 172)), (1.0, (1, 102, 94))],
    "div": [(0.0, (33, 102, 172)), (0.5, (247, 247, 247)), (1.0, (178, 24, 43))],
    "chl": [(0.0, (8, 29, 88)), (0.4, (65, 182, 196)), (0.7, (199, 233, 180)), (1.0, (255, 237, 160))],
}

MASK_ON = (255, 255, 255, 235)   # BSI-E below threshold = what the pipeline turns into ponds
MASK_OFF = (0, 0, 0, 110)        # valid pixel at/above threshold (faint dark veil)


def ramp_css(name):
    return "linear-gradient(90deg,%s)" % ",".join("rgb(%d,%d,%d) %d%%" % (c + (int(s * 100),)) for s, c in RAMPS[name])


def _apply_ramp(t, name):
    stops = RAMPS[name]
    xs = np.array([s for s, _ in stops])
    out = np.zeros(t.shape + (3,), np.uint8)
    for ch in range(3):
        out[..., ch] = np.interp(t, xs, [c[ch] for _, c in stops]).astype(np.uint8)
    return out


def _fetch(url, cache_dir):
    os.makedirs(cache_dir, exist_ok=True)
    fn = os.path.join(cache_dir, hashlib.sha1(url.encode()).hexdigest() + os.path.splitext(url)[1])
    if not os.path.exists(fn):
        req = urllib.request.Request(url, headers={"User-Agent": "bsi-qc/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        # unique temp name: bsi-e and bsi-mask share one file and may be fetched in parallel
        tmp = "%s.%d.%d.part" % (fn, os.getpid(), threading.get_ident())
        with open(tmp, "wb") as f:
            f.write(data)
        try:
            os.replace(tmp, fn)
        except OSError:
            if not os.path.exists(fn):
                raise
            os.remove(tmp)  # another thread finished the same download first
    return fn


def fetch_json(url, cache_dir):
    with open(_fetch(url, cache_dir)) as f:
        return json.load(f)


def _geokeys(tags):
    """GeoKeyDirectoryTag -> {key_id: value} for keys stored inline (TIFFTagLocation 0)."""
    out = {}
    if "GeoKeyDirectoryTag" in tags:
        gk = tags["GeoKeyDirectoryTag"].value
        for k in range(4, len(gk) - 3, 4):
            if gk[k + 1] == 0:
                out[int(gk[k])] = int(gk[k + 3])
    return out


def _georef(page):
    """Return the raster grid: dict(x0, y0, sx, sy, w, h, epsg, nodata, pixel_is_point).

    (x0, y0) is the outer top-left corner of the top-left pixel; sx, sy are positive pixel sizes.
    """
    tags = page.tags
    h, w = page.shape[:2]
    if "ModelPixelScaleTag" in tags and "ModelTiepointTag" in tags:
        scale = tags["ModelPixelScaleTag"].value
        tie = tags["ModelTiepointTag"].value
        i, j, x, y = tie[0], tie[1], tie[3], tie[4]
        sx, sy = float(scale[0]), float(scale[1])
        x0, y0 = x - i * sx, y + j * sy
    elif "ModelTransformationTag" in tags:
        m = tags["ModelTransformationTag"].value
        if abs(m[1]) > 1e-12 or abs(m[4]) > 1e-12:
            raise RuntimeError("Rotated rasters are not supported")
        sx, sy, x0, y0 = float(m[0]), float(-m[5]), float(m[3]), float(m[7])
    else:
        raise RuntimeError("Raster has no georeferencing tags")
    keys = _geokeys(tags)
    # GTRasterTypeGeoKey (1025) = 2 means RasterPixelIsPoint: the tiepoint is the centre of the pixel,
    # so the outer corner sits half a pixel up and left (same convention GDAL applies)
    pixel_is_point = keys.get(1025) == 2
    if pixel_is_point:
        x0 -= sx / 2.0
        y0 += sy / 2.0
    epsg = 4326
    for k in (3072, 2048):  # ProjectedCSType wins over GeographicType
        if keys.get(k) not in (None, 0, 32767):
            epsg = keys[k]
            break
    nodata = None
    if "GDAL_NODATA" in tags:
        try:
            nodata = float(str(tags["GDAL_NODATA"].value).strip("\x00 "))
        except ValueError:
            pass
    return {"x0": x0, "y0": y0, "sx": sx, "sy": sy, "w": w, "h": h, "epsg": epsg, "nodata": nodata,
            "pixel_is_point": pixel_is_point}


def _bounds(gr):
    return [[gr["y0"] - gr["h"] * gr["sy"], gr["x0"]], [gr["y0"], gr["x0"] + gr["w"] * gr["sx"]]]


def _warp_to_4326(arr, gr):
    """Nearest-neighbour reprojection of a projected raster onto a regular lon/lat grid.

    Returns (warped array, outside mask, new grid). Nearest keeps class edges and nodata crisp, which is
    what a QC overlay needs.
    """
    try:
        from pyproj import Transformer
    except ImportError:
        raise RuntimeError("Raster is in EPSG:%d. Install pyproj (conda install pyproj) to reproject it; "
                           "without it the overlay would not line up." % gr["epsg"])
    fwd = Transformer.from_crs(gr["epsg"], 4326, always_xy=True)
    inv = Transformer.from_crs(4326, gr["epsg"], always_xy=True)
    w, h = gr["w"], gr["h"]
    # transform the whole outline (not just corners) to get the true lon/lat extent
    t = np.linspace(0, 1, 50)
    ex = np.concatenate([t * w, np.full(50, w), (1 - t) * w, np.zeros(50)])
    ey = np.concatenate([np.zeros(50), t * h, np.full(50, h), (1 - t) * h])
    lon, lat = fwd.transform(gr["x0"] + ex * gr["sx"], gr["y0"] - ey * gr["sy"])
    minx, maxx, miny, maxy = float(np.min(lon)), float(np.max(lon)), float(np.min(lat)), float(np.max(lat))
    # keep roughly the source pixel count and square-ish pixels in degrees
    ow, oh = w, h
    dsx, dsy = (maxx - minx) / ow, (maxy - miny) / oh
    cx = minx + (np.arange(ow) + 0.5) * dsx
    cy = maxy - (np.arange(oh) + 0.5) * dsy
    gx, gy = np.meshgrid(cx, cy)
    px, py = inv.transform(gx.ravel(), gy.ravel())
    col = np.floor((np.asarray(px) - gr["x0"]) / gr["sx"]).astype(np.int64).reshape(oh, ow)
    row = np.floor((gr["y0"] - np.asarray(py)) / gr["sy"]).astype(np.int64).reshape(oh, ow)
    outside = (col < 0) | (col >= w) | (row < 0) | (row >= h)
    out = arr[np.clip(row, 0, h - 1), np.clip(col, 0, w - 1)]
    ngr = dict(gr, x0=minx, y0=maxy, sx=dsx, sy=dsy, w=ow, h=oh, epsg=4326, source_epsg=gr["epsg"])
    return out, outside, ngr


def render(path, layer, stretch=True, threshold=None):
    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        arr = page.asarray()
        gr = _georef(page)
    if arr.ndim == 3 and arr.shape[0] in (3, 4) and arr.shape[-1] not in (3, 4):
        arr = np.moveaxis(arr, 0, -1)
    outside = None
    src_epsg = gr["epsg"]
    if gr["epsg"] != 4326:
        arr, outside, gr = _warp_to_4326(arr, gr)
    nodata = gr["nodata"]
    legend = {}
    if layer == "tci":
        rgb = arr[..., :3].astype(np.float32)
        mask = np.all(rgb == (nodata if nodata is not None else 0), axis=-1)
        if outside is not None:
            mask |= outside
        if stretch and (~mask).any():
            for ch in range(3):
                v = rgb[..., ch][~mask]
                lo, hi = np.percentile(v, [1, 99])
                rgb[..., ch] = np.clip((rgb[..., ch] - lo) / max(hi - lo, 1e-6) * 255, 0, 255)
        img = np.dstack([rgb.astype(np.uint8), np.where(mask, 0, 255).astype(np.uint8)])
    else:
        a = arr.astype(np.float32)
        if a.ndim == 3:
            a = a[..., 0]
        mask = ~np.isfinite(a)
        if nodata is not None:
            mask |= a == nodata
        if outside is not None:
            mask |= outside
        valid = a[~mask]
        if layer == "bsi-mask":
            if threshold is None:
                raise KeyError("No bsi:threshold for this operation")
            # ponds are dark targets: the pipeline detects BSI-E < threshold
            below = (~mask) & (np.nan_to_num(a, nan=np.inf) < threshold)
            img = np.zeros(a.shape + (4,), np.uint8)
            img[~mask] = MASK_OFF
            img[below] = MASK_ON
            n_valid = int((~mask).sum())
            legend = {"kind": "mask", "threshold": threshold,
                      "pct_below": round(100.0 * int(below.sum()) / n_valid, 1) if n_valid else None}
        else:
            _, rampname, fixed = LAYERS[layer]
            if fixed:
                lo, hi = fixed
            elif valid.size:
                lo, hi = [float(x) for x in np.percentile(valid, [2, 98])]
            else:
                lo, hi = 0.0, 1.0
            t = np.clip((np.nan_to_num(a) - lo) / max(hi - lo, 1e-6), 0, 1)
            rgb = _apply_ramp(t, rampname)
            img = np.dstack([rgb, np.where(mask, 0, 255).astype(np.uint8)])
            legend = {"min": round(lo, 3), "max": round(hi, 3), "css": ramp_css(rampname),
                      "data_min": round(float(valid.min()), 3) if valid.size else None,
                      "data_max": round(float(valid.max()), 3) if valid.size else None}
    buf = io.BytesIO()
    Image.fromarray(img, "RGBA").save(buf, "PNG")
    return {"png": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(), "bounds": _bounds(gr),
            "size": [int(img.shape[1]), int(img.shape[0])], "epsg": src_epsg,
            "pixel_is_point": gr["pixel_is_point"], "legend": legend}


# bump when rendering changes so stale cached PNGs (e.g. pre half-pixel fix) are not served
RENDER_VERSION = 3


def get_layer(props, layer, cache_dir, stretch=True):
    key = LAYERS[layer][0]
    url = props.get(key)
    if not url:
        raise KeyError("No %s asset for this operation" % key)
    threshold = None
    if layer == "bsi-mask":
        threshold = props.get("bsi:threshold")
        if threshold is None:
            raise KeyError("No bsi:threshold for this operation")
        threshold = float(threshold)
    out_cache = os.path.join(cache_dir, "rendered")
    os.makedirs(out_cache, exist_ok=True)
    rk = os.path.join(out_cache, hashlib.sha1(("v%d|%s|%s|%s|%s" % (RENDER_VERSION, url, layer, stretch, threshold))
                                              .encode()).hexdigest() + ".json")
    if os.path.exists(rk):
        with open(rk) as f:
            return json.load(f)
    res = render(_fetch(url, cache_dir), layer, stretch, threshold)
    res["url"] = url
    with open(rk, "w") as f:
        json.dump(res, f)
    return res
