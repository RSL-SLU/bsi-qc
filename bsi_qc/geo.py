"""Small geometry helpers: GeoPackage blobs <-> GeoJSON, areas, and a minimal GeoPackage writer.

No GDAL needed. Handles Polygon and MultiPolygon (all BSI geometries are one of these).
"""
import json
import math
import sqlite3
import struct
import hashlib

R_EARTH = 6378137.0


# ---------- WKB / GPKG decode ----------

def _read_rings(buf, off, bo, nrings):
    rings = []
    for _ in range(nrings):
        (npts,) = struct.unpack_from(bo + "I", buf, off)
        off += 4
        pts = struct.unpack_from(bo + "%dd" % (2 * npts), buf, off)
        off += 16 * npts
        rings.append([[pts[i], pts[i + 1]] for i in range(0, len(pts), 2)])
    return rings, off


def _wkb_to_geojson(buf, off=0):
    bo = "<" if buf[off] == 1 else ">"
    (gtype,) = struct.unpack_from(bo + "I", buf, off + 1)
    off += 5
    gtype = gtype % 1000  # ignore Z/M flavours
    if gtype == 3:
        (n,) = struct.unpack_from(bo + "I", buf, off)
        rings, off = _read_rings(buf, off + 4, bo, n)
        return {"type": "Polygon", "coordinates": rings}, off
    if gtype == 6:
        (n,) = struct.unpack_from(bo + "I", buf, off)
        off += 4
        polys = []
        for _ in range(n):
            g, off = _wkb_to_geojson(buf, off)
            polys.append(g["coordinates"])
        return {"type": "MultiPolygon", "coordinates": polys}, off
    raise ValueError("Unsupported WKB geometry type %s" % gtype)


def gpkg_blob_to_geojson(blob):
    if blob is None:
        return None
    b = bytes(blob)
    if b[:2] != b"GP":
        g, _ = _wkb_to_geojson(b)
        return g
    flags = b[3]
    env = (flags >> 1) & 7
    elen = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}[env]
    g, _ = _wkb_to_geojson(b, 8 + elen)
    return g


# ---------- WKB / GPKG encode ----------

def _wkb_polygon(rings):
    out = [struct.pack("<BII", 1, 3, len(rings))]
    for r in rings:
        out.append(struct.pack("<I", len(r)))
        out.append(struct.pack("<%dd" % (2 * len(r)), *[c for p in r for c in p[:2]]))
    return b"".join(out)


def geojson_to_wkb(g):
    if g["type"] == "Polygon":
        return _wkb_polygon(g["coordinates"])
    if g["type"] == "MultiPolygon":
        parts = [_wkb_polygon(p) for p in g["coordinates"]]
        return struct.pack("<BII", 1, 6, len(parts)) + b"".join(parts)
    raise ValueError("Unsupported geometry " + g["type"])


def bbox(g):
    xs, ys = [], []
    polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
    for p in polys:
        for r in p:
            for x, y in (pt[:2] for pt in r):
                xs.append(x)
                ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def geojson_to_gpkg_blob(g, srs_id=4326):
    minx, miny, maxx, maxy = bbox(g)
    header = b"GP" + struct.pack("<BBi", 0, 0b00000011, srs_id) + struct.pack("<4d", minx, maxx, miny, maxy)
    return header + geojson_to_wkb(g)


def explode(g):
    """MultiPolygon/Polygon/GeometryCollection -> list of Polygon geometries, in order."""
    if g is None:
        return []
    t = g["type"]
    if t == "Polygon":
        return [g]
    if t == "MultiPolygon":
        return [{"type": "Polygon", "coordinates": c} for c in g["coordinates"]]
    if t == "GeometryCollection":
        out = []
        for gg in g["geometries"]:
            out += explode(gg)
        return out
    return []


def wkb_sha256(g):
    return hashlib.sha256(geojson_to_wkb(g)).hexdigest()


# ---------- measures ----------

def _ring_area(r):
    # spherical excess approximation (good to <0.5% at farm scale)
    a = 0.0
    n = len(r)
    for i in range(n - 1):
        x1, y1 = math.radians(r[i][0]), math.radians(r[i][1])
        x2, y2 = math.radians(r[i + 1][0]), math.radians(r[i + 1][1])
        a += (x2 - x1) * (2 + math.sin(y1) + math.sin(y2))
    return abs(a * R_EARTH * R_EARTH / 2.0)


def area_ha(g):
    polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
    tot = 0.0
    for p in polys:
        if not p:
            continue
        tot += _ring_area(p[0]) - sum(_ring_area(h) for h in p[1:])
    return round(tot / 10000.0, 5)


def centroid(g):
    minx, miny, maxx, maxy = bbox(g)
    return (minx + maxx) / 2.0, (miny + maxy) / 2.0


# ---------- minimal GeoPackage writer ----------

def write_gpkg(path, table, rows, columns, geom_type="POLYGON", description=""):
    """rows: list of dicts with key 'geometry' (GeoJSON) + columns. columns: list of (name, sqltype)."""
    import os
    if os.path.exists(path):
        os.remove(path)
    c = sqlite3.connect(path)
    c.execute("PRAGMA application_id = 1196444487")
    c.execute("PRAGMA user_version = 10200")
    c.executescript(
        """
        CREATE TABLE gpkg_spatial_ref_sys (srs_name TEXT NOT NULL, srs_id INTEGER PRIMARY KEY, organization TEXT NOT NULL,
          organization_coordsys_id INTEGER NOT NULL, definition TEXT NOT NULL, description TEXT);
        CREATE TABLE gpkg_contents (table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL, identifier TEXT UNIQUE,
          description TEXT DEFAULT '', last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER);
        CREATE TABLE gpkg_geometry_columns (table_name TEXT NOT NULL, column_name TEXT NOT NULL, geometry_type_name TEXT NOT NULL,
          srs_id INTEGER NOT NULL, z TINYINT NOT NULL, m TINYINT NOT NULL, CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name));
        """
    )
    wgs = ('GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],'
           'AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,'
           'AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","4326"]]')
    c.executemany("INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)", [
        ("Undefined cartesian SRS", -1, "NONE", -1, "undefined", None),
        ("Undefined geographic SRS", 0, "NONE", 0, "undefined", None),
        ("WGS 84 geodetic", 4326, "EPSG", 4326, wgs, None),
    ])
    coldefs = ", ".join('"%s" %s' % (n, t) for n, t in columns)
    c.execute('CREATE TABLE "%s" (fid INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, geom %s%s)'
              % (table, geom_type, (", " + coldefs) if coldefs else ""))
    allb = [bbox(r["geometry"]) for r in rows if r.get("geometry")]
    ext = (min(b[0] for b in allb), min(b[1] for b in allb), max(b[2] for b in allb), max(b[3] for b in allb)) if allb else (None,) * 4
    c.execute("INSERT INTO gpkg_contents (table_name, data_type, identifier, description, min_x, min_y, max_x, max_y, srs_id) "
              "VALUES (?,?,?,?,?,?,?,?,4326)", (table, "features", table, description) + ext)
    c.execute("INSERT INTO gpkg_geometry_columns VALUES (?, 'geom', ?, 4326, 0, 0)", (table, geom_type))
    names = [n for n, _ in columns]
    q = 'INSERT INTO "%s" (geom%s) VALUES (?%s)' % (
        table, "".join(', "%s"' % n for n in names), ",?" * len(names))
    c.executemany(q, [
        (geojson_to_gpkg_blob(r["geometry"]) if r.get("geometry") else None,) + tuple(r.get(n) for n in names)
        for r in rows
    ])
    c.commit()
    c.close()


def register_gpkg_functions(conn):
    """Spatial SQL functions GeoPackage rtree triggers call (QGIS/GDAL files ship these triggers)."""
    def _bb(b):
        g = gpkg_blob_to_geojson(b) if b is not None else None
        return bbox(g) if g else None

    def empty(b):
        if b is None:
            return 1
        return 1 if (bytes(b)[3] >> 4) & 1 else 0
    conn.create_function("ST_IsEmpty", 1, empty, deterministic=True)
    for name, i in (("ST_MinX", 0), ("ST_MinY", 1), ("ST_MaxX", 2), ("ST_MaxY", 3)):
        conn.create_function(name, 1, (lambda i: lambda b: (_bb(b) or (None,) * 4)[i])(i), deterministic=True)


def dumps(g):
    return json.dumps(g, separators=(",", ":"))
