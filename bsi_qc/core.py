"""Database, ingest, legacy normalisation, verdict roll-up and exports for BSI QC."""
import csv
import datetime as dt
import glob
import json
import os
import re
import shutil
import sqlite3
from collections import Counter

from . import geo

HERE = os.path.dirname(os.path.abspath(__file__))
VOCAB_PATH = os.path.join(HERE, "vocab.json")
PROPOSED = "proposed:"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS ops (
  op_key TEXT PRIMARY KEY, region TEXT, country TEXT, season TEXT, op_id TEXT,
  sample_class TEXT, bsi_detected INTEGER, pond_count INTEGER,
  geom TEXT, props TEXT, file_prefix TEXT, ops_file TEXT, ponds_loaded INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS op_qc (
  op_key TEXT PRIMARY KEY, qc_result TEXT, qc_feature TEXT, qc_issue TEXT, qc_site_valid TEXT,
  qc_notes TEXT, qc_status TEXT DEFAULT 'not_reviewed', qc_reviewer TEXT, qc_date TEXT,
  origin TEXT DEFAULT 'app', locked INTEGER DEFAULT 0, legacy_raw TEXT, version INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS polys (
  pid TEXT PRIMARY KEY, op_key TEXT, part INTEGER, origin TEXT, geom TEXT, area_ha REAL,
  sha TEXT, gpkg_fid INTEGER, extra TEXT, created_by TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS polys_op ON polys(op_key);
CREATE TABLE IF NOT EXISTS poly_qc (
  pid TEXT PRIMARY KEY, qc_class TEXT, qc_issue TEXT, qc_confidence TEXT, qc_facility_association TEXT,
  qc_status TEXT DEFAULT 'not_reviewed', qc_reviewer TEXT, qc_notes TEXT, qc_date TEXT, version INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, reviewer TEXT, tbl TEXT, rec TEXT, before TEXT, after TEXT);
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, reviewer TEXT, scope TEXT, field TEXT, label TEXT,
  rec TEXT, status TEXT DEFAULT 'open', resolved_value TEXT, resolved_by TEXT, resolved_ts TEXT);
"""

OP_FIELDS = ["qc_result", "qc_feature", "qc_issue", "qc_site_valid", "qc_notes", "qc_status"]
POLY_FIELDS = ["qc_class", "qc_issue", "qc_confidence", "qc_facility_association", "qc_notes", "qc_status"]


def now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def connect(db_path):
    c = sqlite3.connect(db_path, timeout=15)
    c.row_factory = sqlite3.Row
    # DELETE journal (not WAL) so the file is safe-ish on shared/network drives
    c.execute("PRAGMA journal_mode=DELETE")
    c.execute("PRAGMA busy_timeout=15000")
    c.executescript(SCHEMA)
    return c


def load_vocab():
    with open(VOCAB_PATH) as f:
        return json.load(f)


def save_vocab(v):
    tmp = VOCAB_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(v, f, indent=2)
    os.replace(tmp, VOCAB_PATH)


# --------------------------------------------------------------------------- ingest

def _season(props):
    s = props.get("start_datetime") or props.get("datetime") or ""
    return s[:4]


def _region_of(qc_dir, root):
    rel = os.path.relpath(os.path.dirname(qc_dir), root)
    return rel.replace(os.sep, "/")


def _load_fc(path):
    with open(path) as f:
        return json.load(f).get("features", [])


def _pick_complete(qc_dir, prefix=""):
    cands = [p for p in glob.glob(os.path.join(qc_dir, "processed", prefix + "*complete*.geojson"))]
    cands += [p for p in glob.glob(os.path.join(qc_dir, prefix + "*complete*.geojson"))]
    cands = [p for p in cands if os.sep + "dump" + os.sep not in p]
    return sorted(cands, key=os.path.getmtime)[-1] if cands else None


# Legacy vocabulary clean-up. Every change is written to the import report.
RESULT_MAP = {"uncertin": "uncertain", "uncertian": "uncertain", "incorect": "incorrect", "corect": "correct"}
FEATURE_MAP = {"water_body": "other_water", "waterbody": "other_water", "other": PROPOSED + "other",
               "uncertain": "unclear"}
ISSUE_MAP = {"resolution": "missed_small_feature", "reesolution": "missed_small_feature",
             "missed_small_featurel": "missed_small_feature", "too small to detect": "missed_small_feature",
             "possible cafo fp": "none",
             "": "none", "none": "none", "null": "none"}
SITE_RE = re.compile(r"cafo\s*fp|not (look )?like a cattle|does not look like a (cattle )?farm|solar panel|not a (cattle )?farm", re.I)


def normalise_legacy(p, vocab):
    """Returns (clean dict, list of (field, raw, clean, reason))."""
    changes = []
    out = {}
    ok_result = {v["value"] for v in vocab["operation"]["qc_result"]}
    ok_feature = {v["value"] for v in vocab["operation"]["qc_feature"]}
    ok_issue = {v["value"] for v in vocab["operation"]["qc_issue"]}

    def norm(field, raw, mapping, ok):
        r = "" if raw is None else str(raw)
        s = r.strip().lower()
        if s in ("none",) and field != "qc_issue":
            s = ""
        if s in ok:
            val = s
        elif s in mapping:
            val = mapping[s]
        elif s == "":
            val = None
        else:
            val = "other" if field == "qc_issue" else PROPOSED + s
        if (val or "") != r and not (r.strip() in ("", "None") and val in (None, "none")):
            changes.append((field, r, val, "normalised"))
        return val

    out["qc_result"] = norm("qc_result", p.get("qc_result"), RESULT_MAP, ok_result)
    out["qc_feature"] = norm("qc_feature", p.get("qc_feature"), FEATURE_MAP, ok_feature)
    raw_issue = p.get("qc_issue")
    out["qc_issue"] = norm("qc_issue", raw_issue, ISSUE_MAP, ok_issue)
    notes = p.get("qc_notes")
    notes = "" if notes in (None, "None") else str(notes).strip()
    # issue text that was really a note ("possible CAFO FP", "one detection is a fp") is kept in notes
    ri = "" if raw_issue is None else str(raw_issue).strip()
    if ri and ri.lower() not in ok_issue and ri.lower() not in ("none", "null"):
        notes = (notes + "; " if notes else "") + "[legacy qc_issue: %s]" % ri
    out["qc_notes"] = notes
    blob = " ".join([ri, notes])
    if SITE_RE.search(blob):
        out["qc_site_valid"] = "no"
        changes.append(("qc_site_valid", "", "no", "inferred from notes/issue text, please confirm"))
    else:
        out["qc_site_valid"] = None
    return out, changes


def check_inputs(data_root, gpkg_path=None):
    """Fail early with a clear message instead of half-ingesting (or creating an empty .gpkg by accident)."""
    if not os.path.isdir(data_root):
        raise SystemExit("Data folder not found: %s" % os.path.abspath(data_root))
    qc_dirs = sorted(d for d in glob.glob(os.path.join(data_root, "**", "qc"), recursive=True) if os.path.isdir(d))
    if not qc_dirs:
        raise SystemExit("No <region>/qc/ folders under %s. Point --data at the folder that contains AUS/, CAN/, USA/ ..."
                         % os.path.abspath(data_root))
    if gpkg_path:
        if not os.path.isfile(gpkg_path) or os.path.getsize(gpkg_path) == 0:
            raise SystemExit("GeoPackage not found (or empty): %s\nCheck the file name; e.g. "
                             "--gpkg \"path\\to\\International_BSI_Polygon_QC.gpkg\"" % os.path.abspath(gpkg_path))
        g = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(gpkg_path).replace("\\", "/"), uri=True)
        try:
            tables = [r[0] for r in g.execute("SELECT table_name FROM gpkg_contents")]
        except sqlite3.DatabaseError:
            raise SystemExit("%s is not a GeoPackage." % gpkg_path)
        finally:
            g.close()
        if "bsi_polygon_qc" not in tables:
            raise SystemExit("%s has no 'bsi_polygon_qc' layer (layers found: %s)" % (gpkg_path, ", ".join(tables) or "none"))
    return qc_dirs


def ingest(db_path, data_root, gpkg_path=None, report_path=None, log=print):
    qc_dirs = check_inputs(data_root, gpkg_path)
    vocab = load_vocab()
    c = connect(db_path)
    report = []
    n_ops = n_polys = n_legacy = 0
    for qd in qc_dirs:
        region = _region_of(qd, data_root)
        # one qc/ folder can hold several sample sets (e.g. cohort-1, cohort-2); each is keyed by its file prefix
        sets = []
        for sp in sorted(glob.glob(os.path.join(qd, "*_qc_samples.geojson"))):
            pre = os.path.basename(sp).split("_qc_samples")[0]
            sets.append((pre, sp, _pick_complete(qd, pre), os.path.join(qd, pre + "_qc_samples_ponds.geojson")))
        if not sets:
            comp = _pick_complete(qd)
            if comp:
                sets.append((os.path.basename(comp).split("_qc_samples")[0], comp, comp, None))
        for prefix, src, complete, ponds_file in sets:
            for f in _load_fc(src):
                p = f["properties"]
                op_id = p.get("id") or p.get("detection_id")
                key = "%s:%s" % (region, op_id)
                c.execute("""INSERT INTO ops (op_key, region, country, season, op_id, sample_class, bsi_detected, pond_count,
                             geom, props, file_prefix, ops_file) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                             ON CONFLICT(op_key) DO UPDATE SET props=excluded.props, geom=excluded.geom""",
                          (key, region, p.get("country_id") or region.split("/")[0], _season(p), op_id,
                           p.get("qc:sample_class"), 1 if p.get("bsi:pond_detected") else 0, p.get("bsi:pond_count") or 0,
                           geo.dumps(f["geometry"]), json.dumps(p), prefix, os.path.relpath(src, data_root)))
                c.execute("INSERT OR IGNORE INTO op_qc (op_key) VALUES (?)", (key,))
                n_ops += 1
            # legacy verdicts
            if complete:
                for f in _load_fc(complete):
                    p = f["properties"]
                    key = "%s:%s" % (region, p.get("id") or p.get("detection_id"))
                    row = c.execute("SELECT origin, version, qc_result FROM op_qc WHERE op_key=?", (key,)).fetchone()
                    if row is None or row["version"] > 0 or row["origin"] != "app" or row["qc_result"]:
                        continue
                    if not any(p.get(k) not in (None, "") for k in ("qc_result", "qc_feature")):
                        continue
                    clean, changes = normalise_legacy(p, vocab)
                    c.execute("""UPDATE op_qc SET qc_result=?, qc_feature=?, qc_issue=?, qc_site_valid=?, qc_notes=?,
                                 qc_status='reviewed', origin='legacy', locked=1, legacy_raw=?, qc_reviewer=?, qc_date=?
                                 WHERE op_key=?""",
                              (clean["qc_result"], clean["qc_feature"], clean["qc_issue"], clean["qc_site_valid"],
                               clean["qc_notes"],
                               json.dumps({k: p.get(k) for k in ("qc_result", "qc_feature", "qc_issue", "qc_notes")}),
                               "legacy:" + os.path.basename(complete).split("complete_")[-1].replace(".geojson", ""),
                               dt.datetime.fromtimestamp(os.path.getmtime(complete)).date().isoformat(), key))
                    n_legacy += 1
                    _record_proposals(c, "operation", key, clean, "legacy import")
                    for fld, raw, val, why in changes:
                        report.append({"region": region, "op_key": key, "field": fld, "raw": raw, "clean": val,
                                       "reason": why, "source_file": os.path.relpath(complete, data_root)})
            # pond polygons from the local ponds file (gpkg, if given, overrides IDs below)
            if ponds_file and os.path.exists(ponds_file):
                for f in _load_fc(ponds_file):
                    p = f["properties"]
                    key = "%s:%s" % (region, p.get("id") or p.get("detection_id"))
                    n_polys += _add_bsi_parts(c, key, region, p.get("detection_id") or p.get("id"), f["geometry"])
                    c.execute("UPDATE ops SET ponds_loaded=1 WHERE op_key=?", (key,))
    if gpkg_path:
        n_polys += _ingest_gpkg(c, gpkg_path, data_root)
        tdir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "templates")
        os.makedirs(tdir, exist_ok=True)
        shutil.copy2(gpkg_path, os.path.join(tdir, "International_BSI_Polygon_QC.gpkg"))
        c.execute("INSERT OR REPLACE INTO meta VALUES ('gpkg_template', ?)", ("templates/International_BSI_Polygon_QC.gpkg",))
    c.execute("INSERT OR REPLACE INTO meta VALUES ('data_root', ?)", (os.path.abspath(data_root),))
    c.execute("INSERT OR REPLACE INTO meta VALUES ('ingested_at', ?)", (now(),))
    c.commit()
    if report_path and report:  # keep the earlier report when a later ingest has nothing to fix
        with open(report_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["region", "op_key", "field", "raw", "clean", "reason", "source_file"])
            w.writeheader()
            w.writerows(report)
    log("Ingested %d operations, %d new BSI polygons, %d legacy verdicts (%d vocabulary fixes logged)"
        % (n_ops, n_polys, n_legacy, len(report)))
    return c


def _add_bsi_parts(c, key, region, det_id, geometry, id_prefix=None):
    parts = geo.explode(geometry)
    added = 0
    country = region.split("/")[0]
    pref = id_prefix or ("%s_%s" % (country if "/" not in region else region.split("/")[-1], det_id))
    for i, g in enumerate(parts, 1):
        sha = geo.wkb_sha256(g)
        if c.execute("SELECT 1 FROM polys WHERE op_key=? AND origin='bsi' AND part=?", (key, i)).fetchone():
            continue
        pid = "%s_p%03d" % (pref, i)
        c.execute("INSERT OR IGNORE INTO polys (pid, op_key, part, origin, geom, area_ha, sha, extra, created_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?)",
                  (pid, key, i, "bsi", geo.dumps(g), geo.area_ha(g), sha,
                   json.dumps({"parts_in_operation": len(parts)}), now()))
        c.execute("INSERT OR IGNORE INTO poly_qc (pid) VALUES (?)", (pid,))
        added += 1
    return added


def _ingest_gpkg(c, gpkg_path, data_root):
    g = sqlite3.connect(gpkg_path)
    g.row_factory = sqlite3.Row
    n = 0
    for r in g.execute("SELECT * FROM bsi_polygon_qc"):
        key = "%s:%s" % (r["country"], r["operation_id"])
        if not c.execute("SELECT 1 FROM ops WHERE op_key=?", (key,)).fetchone():
            continue
        geom = geo.gpkg_blob_to_geojson(r["geom"])
        extra = {k: r[k] for k in ("parts_in_operation", "reported_bsi_pond_count", "count_mismatch",
                                   "geometry_sha256", "original_ponds_file", "geometry_area_ha")}
        # replace any provisional ponds-file part with the gpkg's authoritative ID
        old = c.execute("SELECT pid FROM polys WHERE op_key=? AND origin='bsi' AND part=?",
                        (key, r["polygon_part"])).fetchone()
        if old and old["pid"] != r["polygon_qc_id"]:
            c.execute("UPDATE polys SET pid=? WHERE pid=?", (r["polygon_qc_id"], old["pid"]))
            c.execute("UPDATE poly_qc SET pid=? WHERE pid=?", (r["polygon_qc_id"], old["pid"]))
        if old:
            c.execute("UPDATE polys SET gpkg_fid=?, extra=?, area_ha=? WHERE pid=?",
                      (r["fid"], json.dumps(extra), r["geometry_area_ha"], r["polygon_qc_id"]))
        else:
            c.execute("INSERT INTO polys (pid, op_key, part, origin, geom, area_ha, sha, gpkg_fid, extra, created_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (r["polygon_qc_id"], key, r["polygon_part"], "bsi", geo.dumps(geom), r["geometry_area_ha"],
                       r["geometry_sha256"], r["fid"], json.dumps(extra), now()))
            n += 1
        c.execute("INSERT OR IGNORE INTO poly_qc (pid) VALUES (?)", (r["polygon_qc_id"],))
        # carry over anything already reviewed in QGIS
        if r["qc_status"] and r["qc_status"] != "not_reviewed":
            cur = c.execute("SELECT version FROM poly_qc WHERE pid=?", (r["polygon_qc_id"],)).fetchone()
            if cur and cur["version"] == 0:
                c.execute("""UPDATE poly_qc SET qc_class=?, qc_issue=?, qc_confidence=?, qc_facility_association=?,
                             qc_status=?, qc_reviewer=?, qc_notes=?, qc_date=?, version=1 WHERE pid=?""",
                          (r["qc_class"], r["qc_issue"], r["qc_confidence"], r["qc_facility_association"],
                           r["qc_status"], r["qc_reviewer"], r["qc_notes"], r["qc_date"], r["polygon_qc_id"]))
        c.execute("UPDATE ops SET ponds_loaded=1 WHERE op_key=?", (key,))
    return n


def ops_missing_ponds(c, region=None):
    q = "SELECT op_key FROM ops WHERE bsi_detected=1 AND ponds_loaded=0"
    return [r[0] for r in (c.execute(q + " AND region=?", (region,)) if region else c.execute(q))]


def add_remote_ponds(c, op_key, fc):
    """Register polygons fetched from an operation's asset:bsi-ponds (used for regions without a local ponds file)."""
    op = c.execute("SELECT * FROM ops WHERE op_key=?", (op_key,)).fetchone()
    feats = fc.get("features", []) if fc.get("type") == "FeatureCollection" else [fc]
    gs = []
    for f in feats:
        gs += geo.explode(f.get("geometry") if "geometry" in f else f)
    det = json.loads(op["props"]).get("detection_id") or op["op_id"]
    n = _add_bsi_parts(c, op_key, op["region"], det, {"type": "MultiPolygon", "coordinates": [g["coordinates"] for g in gs]}) if gs else 0
    c.execute("UPDATE ops SET ponds_loaded=1 WHERE op_key=?", (op_key,))
    c.commit()
    return n


# --------------------------------------------------------------------------- reads

def op_status(c, op_key):
    q = c.execute("SELECT * FROM op_qc WHERE op_key=?", (op_key,)).fetchone()
    ps = c.execute("SELECT q.qc_status, q.qc_class FROM polys p JOIN poly_qc q ON q.pid=p.pid WHERE p.op_key=?",
                   (op_key,)).fetchall()
    return q, ps


def queue(c):
    rows = c.execute("""
      SELECT o.op_key, o.region, o.country, o.season, o.op_id, o.sample_class, o.bsi_detected, o.pond_count,
             o.ponds_loaded, q.qc_status, q.qc_result, q.qc_feature, q.qc_site_valid, q.qc_reviewer, q.qc_date,
             q.origin, q.locked,
             (SELECT COUNT(*) FROM polys p WHERE p.op_key=o.op_key AND p.origin='bsi') AS n_bsi,
             (SELECT COUNT(*) FROM polys p WHERE p.op_key=o.op_key AND p.origin='missed') AS n_missed,
             (SELECT COUNT(*) FROM polys p JOIN poly_qc pq ON pq.pid=p.pid
                 WHERE p.op_key=o.op_key AND pq.qc_status IN ('reviewed','needs_followup')) AS n_poly_done,
             (SELECT COUNT(*) FROM polys p JOIN poly_qc pq ON pq.pid=p.pid
                 WHERE p.op_key=o.op_key AND pq.qc_status='needs_followup') AS n_poly_follow,
             (SELECT GROUP_CONCAT(DISTINCT pq.qc_reviewer) FROM polys p JOIN poly_qc pq ON pq.pid=p.pid
                 WHERE p.op_key=o.op_key AND pq.qc_reviewer IS NOT NULL) AS poly_reviewers
      FROM ops o JOIN op_qc q ON q.op_key=o.op_key ORDER BY o.region, o.sample_class DESC, o.op_id""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["state"] = op_state(d)
        out.append(d)
    return out


def op_state(d):
    """not_started / in_progress / done / needs_followup, considering both levels."""
    n_total = d["n_bsi"] + d["n_missed"]
    polys_done = d["n_poly_done"] >= n_total and (d["ponds_loaded"] or not d["bsi_detected"])
    op_done = d["qc_status"] in ("reviewed", "needs_followup")
    if d["qc_status"] == "needs_followup" or d["n_poly_follow"]:
        if op_done and polys_done:
            return "needs_followup"
    if op_done and polys_done:
        return "done"
    if op_done and d["origin"] and d["origin"].startswith("legacy"):
        return "polys_pending"
    if op_done or d["n_poly_done"] or d["qc_reviewer"]:
        return "in_progress"
    return "not_started"


def get_op(c, op_key):
    o = c.execute("SELECT * FROM ops WHERE op_key=?", (op_key,)).fetchone()
    if not o:
        return None
    q = dict(c.execute("SELECT * FROM op_qc WHERE op_key=?", (op_key,)).fetchone())
    polys = []
    for p in c.execute("""SELECT p.*, q.qc_class, q.qc_issue, q.qc_confidence, q.qc_facility_association, q.qc_status,
                          q.qc_reviewer, q.qc_notes, q.qc_date, q.version FROM polys p JOIN poly_qc q ON q.pid=p.pid
                          WHERE p.op_key=? ORDER BY p.origin, p.part""", (op_key,)):
        d = dict(p)
        d["geom"] = json.loads(d["geom"])
        d["extra"] = json.loads(d["extra"] or "{}")
        polys.append(d)
    od = dict(o)
    od["geom"] = json.loads(od["geom"])
    od["props"] = json.loads(od["props"])
    hist = [dict(h) for h in c.execute(
        "SELECT ts, reviewer, tbl, rec FROM audit WHERE rec=? OR rec IN (SELECT pid FROM polys WHERE op_key=?) "
        "ORDER BY id DESC LIMIT 15", (op_key, op_key))]
    return {"op": od, "qc": q, "polys": polys, "suggestion": suggest(od, polys), "history": hist}


# --------------------------------------------------------------------------- roll-up

POND_LIKE = {"manure_pond"}
MAYBE = {"possible_manure", "manure_related_nonpond"}
WRONG = {"other_water", "wet_runoff", "other_false_positive"}


def suggest(op, polys):
    """Suggest operation-level qc_result / qc_feature / qc_issue from polygon labels (per the regional guide)."""
    bsi = [p for p in polys if p["origin"] == "bsi"]
    missed = [p for p in polys if p["origin"] == "missed"]
    labelled = [p for p in bsi if p.get("qc_class") and not str(p["qc_class"]).startswith(PROPOSED)]
    why = []
    s = {"qc_result": None, "qc_feature": None, "qc_issue": None}
    positive = bool(op.get("bsi_detected")) or bool(bsi)
    if positive:
        if not bsi:
            why0 = ("BSI reported a pond but returned no polygons." if op.get("ponds_loaded")
                    else "Pond polygons not loaded yet.")
            return {**s, "why": why0, "complete": False}
        if len(labelled) < len(bsi):
            why.append("%d of %d polygons labelled; suggestion will update." % (len(labelled), len(bsi)))
        area = Counter()
        for p in labelled:
            area[p["qc_class"]] += p.get("area_ha") or 0
        classes = set(area)
        if classes & POND_LIKE:
            s["qc_result"], s["qc_feature"] = "correct", "manure_pond"
            why.append("At least one polygon is a manure pond.")
        elif classes & MAYBE:
            conf = [p.get("qc_confidence") for p in labelled if p["qc_class"] in MAYBE]
            s["qc_result"] = "correct" if any(x in ("high", "medium") for x in conf) else "uncertain"
            s["qc_feature"] = max((k for k in area if k in MAYBE), key=lambda k: area[k])
            why.append("Best evidence is %s (%s confidence) so %s." % (s["qc_feature"], "/".join(filter(None, conf)) or "no",
                                                                       s["qc_result"]))
        elif classes and classes <= WRONG:
            s["qc_result"] = "incorrect"
            s["qc_feature"] = max(area, key=lambda k: area[k])
            why.append("All labelled polygons are non-manure; largest area is %s." % s["qc_feature"])
        elif classes:
            s["qc_result"], s["qc_feature"] = "uncertain", "unclear"
            why.append("Polygons are uncertain or mixed with no manure evidence.")
        manure_missed = [m for m in missed if m.get("qc_class") in POND_LIKE | MAYBE]
        if manure_missed and s["qc_feature"] != "manure_pond":
            why.append("Note: %d missed manure feature(s) marked." % len(manure_missed))
    else:
        manure_missed = [m for m in missed if m.get("qc_class") in POND_LIKE | MAYBE]
        if manure_missed:
            best = "manure_pond" if any(m["qc_class"] == "manure_pond" for m in manure_missed) else manure_missed[0]["qc_class"]
            s["qc_result"], s["qc_feature"] = "incorrect", best
            why.append("BSI-negative but %d missed manure feature(s) marked." % len(manure_missed))
        else:
            s["qc_result"], s["qc_feature"] = "correct", "no_visible_candidate"
            why.append("BSI-negative and no missed manure feature marked.")
    issues = [p.get("qc_issue") for p in labelled + missed
              if p.get("qc_issue") and p.get("qc_issue") != "none" and not str(p["qc_issue"]).startswith(PROPOSED)]
    s["qc_issue"] = Counter(issues).most_common(1)[0][0] if issues else "none"
    s["why"] = " ".join(why)
    s["complete"] = len(labelled) == len(bsi)
    return s


# --------------------------------------------------------------------------- writes

class Conflict(Exception):
    def __init__(self, current):
        self.current = current


def _record_proposals(c, scope, rec, data, reviewer):
    for k, v in data.items():
        if isinstance(v, str) and v.startswith(PROPOSED):
            label = v[len(PROPOSED):].strip()
            if label and not c.execute("SELECT 1 FROM proposals WHERE scope=? AND field=? AND label=? AND rec=? AND status='open'",
                                       (scope, k, label, rec)).fetchone():
                c.execute("INSERT INTO proposals (ts, reviewer, scope, field, label, rec) VALUES (?,?,?,?,?,?)",
                          (now(), reviewer, scope, k, label, rec))


def save_op(c, op_key, data, reviewer, version):
    cur = c.execute("SELECT * FROM op_qc WHERE op_key=?", (op_key,)).fetchone()
    if cur is None:
        raise KeyError(op_key)
    if version is not None and int(version) != cur["version"]:
        raise Conflict(dict(cur))
    if cur["locked"] and not data.get("_unlock"):
        raise PermissionError("Operation verdict is locked (imported from completed QC). Unlock to edit.")
    upd = {k: (data.get(k) if data.get(k) != "" else None) for k in OP_FIELDS if k in data}
    # a saved verdict must say whether the site is a cattle operation; never assume "yes"
    status = upd.get("qc_status", cur["qc_status"])
    site = upd["qc_site_valid"] if "qc_site_valid" in upd else cur["qc_site_valid"]
    if status in ("reviewed", "needs_followup") and site not in ("yes", "no", "unsure"):
        raise ValueError("Answer 'Is this a cattle operation?' before saving the verdict.")
    upd.update({"qc_reviewer": reviewer, "qc_date": now(), "version": cur["version"] + 1, "locked": 0})
    if cur["origin"] == "legacy":
        upd["origin"] = "legacy_edited"
    sets = ", ".join("%s=?" % k for k in upd)
    c.execute("UPDATE op_qc SET %s WHERE op_key=?" % sets, list(upd.values()) + [op_key])
    c.execute("INSERT INTO audit (ts, reviewer, tbl, rec, before, after) VALUES (?,?,?,?,?,?)",
              (now(), reviewer, "op_qc", op_key, json.dumps(dict(cur)), json.dumps(upd)))
    _record_proposals(c, "operation", op_key, upd, reviewer)
    c.commit()


def save_poly(c, pid, data, reviewer, version):
    cur = c.execute("SELECT * FROM poly_qc WHERE pid=?", (pid,)).fetchone()
    if cur is None:
        raise KeyError(pid)
    if version is not None and int(version) != cur["version"]:
        raise Conflict(dict(cur))
    upd = {k: (data.get(k) if data.get(k) != "" else None) for k in POLY_FIELDS if k in data}
    upd.update({"qc_reviewer": reviewer, "qc_date": now(), "version": cur["version"] + 1})
    sets = ", ".join("%s=?" % k for k in upd)
    c.execute("UPDATE poly_qc SET %s WHERE pid=?" % sets, list(upd.values()) + [pid])
    c.execute("INSERT INTO audit (ts, reviewer, tbl, rec, before, after) VALUES (?,?,?,?,?,?)",
              (now(), reviewer, "poly_qc", pid, json.dumps(dict(cur)), json.dumps(upd)))
    _record_proposals(c, "polygon", pid, upd, reviewer)
    c.commit()


def add_missed(c, op_key, geometry, reviewer, qc_class=None):
    op = c.execute("SELECT * FROM ops WHERE op_key=?", (op_key,)).fetchone()
    n = c.execute("SELECT COUNT(*) FROM polys WHERE op_key=? AND origin='missed'", (op_key,)).fetchone()[0] + 1
    det = json.loads(op["props"]).get("detection_id") or op["op_id"]
    region_tag = op["region"].split("/")[-1] if "/" in op["region"] else op["country"]
    pid = "%s_%s_m%03d" % (region_tag, det, n)
    while c.execute("SELECT 1 FROM polys WHERE pid=?", (pid,)).fetchone():
        n += 1
        pid = "%s_%s_m%03d" % (region_tag, det, n)
    c.execute("INSERT INTO polys (pid, op_key, part, origin, geom, area_ha, sha, extra, created_by, created_at) "
              "VALUES (?,?,?,?,?,?,?,?,?,?)",
              (pid, op_key, n, "missed", geo.dumps(geometry), geo.area_ha(geometry), geo.wkb_sha256(geometry),
               "{}", reviewer, now()))
    c.execute("INSERT INTO poly_qc (pid, qc_class, qc_issue, qc_reviewer, qc_date, qc_status, version) VALUES (?,?,?,?,?,?,1)",
              (pid, qc_class, "none", reviewer, now(), "reviewed" if qc_class else "not_reviewed"))
    c.execute("INSERT INTO audit (ts, reviewer, tbl, rec, before, after) VALUES (?,?,?,?,?,?)",
              (now(), reviewer, "polys", pid, None, json.dumps({"added_missed": True, "qc_class": qc_class})))
    c.commit()
    return pid


def delete_missed(c, pid, reviewer):
    r = c.execute("SELECT * FROM polys WHERE pid=?", (pid,)).fetchone()
    if not r or r["origin"] != "missed":
        raise PermissionError("Only reviewer-drawn missed polygons can be deleted.")
    c.execute("INSERT INTO audit (ts, reviewer, tbl, rec, before, after) VALUES (?,?,?,?,?,?)",
              (now(), reviewer, "polys", pid, json.dumps(dict(r)), "deleted"))
    c.execute("DELETE FROM polys WHERE pid=?", (pid,))
    c.execute("DELETE FROM poly_qc WHERE pid=?", (pid,))
    c.commit()


def resolve_proposal(c, prop_id, action, value, reviewer, label=None, help_text=""):
    """action: 'promote' (add value to vocab; value=new key) or 'map' (replace with existing value) or 'reject'."""
    pr = c.execute("SELECT * FROM proposals WHERE id=?", (prop_id,)).fetchone()
    if not pr:
        raise KeyError(prop_id)
    raw = PROPOSED + pr["label"]
    if action == "promote":
        value = re.sub(r"[^a-z0-9_]+", "_", (value or pr["label"]).lower()).strip("_")
        v = load_vocab()
        grp = v["polygon" if pr["scope"] == "polygon" else "operation"]
        if pr["field"] in grp and value not in {x["value"] for x in grp[pr["field"]]}:
            grp[pr["field"]].append({"value": value, "label": label or pr["label"], "help": help_text or "Added from reviewer proposal."})
            save_vocab(v)
    elif action == "reject":
        value = value or None
    tbl, keycol = ("poly_qc", "pid") if pr["scope"] == "polygon" else ("op_qc", "op_key")
    if action == "reject" and tbl == "op_qc" and c.execute(
            "SELECT 1 FROM op_qc WHERE %s=? AND origin LIKE 'legacy%%'" % pr["field"], (raw,)).fetchone():
        raise PermissionError("This label comes from an imported (completed) verdict. Rejecting would blank that field "
                              "in the Climate TRACE file; use Map to pick an existing value instead.")
    # apply to every record using this proposed label for this field, not just the one that raised it
    c.execute("UPDATE %s SET %s=? WHERE %s=?" % (tbl, pr["field"], pr["field"]), (value, raw))
    c.execute("UPDATE proposals SET status=?, resolved_value=?, resolved_by=?, resolved_ts=? "
              "WHERE scope=? AND field=? AND label=? AND status='open'",
              ("promoted" if action == "promote" else ("mapped" if action == "map" else "rejected"), value, reviewer,
               now(), pr["scope"], pr["field"], pr["label"]))
    c.execute("INSERT INTO audit (ts, reviewer, tbl, rec, before, after) VALUES (?,?,?,?,?,?)",
              (now(), reviewer, "proposals", str(prop_id), raw, json.dumps({"action": action, "value": value})))
    c.commit()


def stats(c):
    q = queue(c)
    by_region = {}
    for r in q:
        d = by_region.setdefault(r["region"], Counter())
        d["ops"] += 1
        d[r["state"]] += 1
        d["polys"] += r["n_bsi"]
        d["polys_done"] += min(r["n_poly_done"], r["n_bsi"] + r["n_missed"])
        d["missed"] += r["n_missed"]
        if r["origin"] and r["origin"].startswith("legacy"):
            d["legacy"] += 1
        if r["qc_site_valid"] == "no":
            d["site_invalid"] += 1
        d["result_" + str(r["qc_result"])] += 1
    classes = Counter()
    for r in c.execute("SELECT o.region, q.qc_class FROM polys p JOIN poly_qc q ON q.pid=p.pid JOIN ops o ON o.op_key=p.op_key "
                       "WHERE q.qc_class IS NOT NULL"):
        classes[(r["region"], r["qc_class"])] += 1
    reviewers = Counter()
    for r in c.execute("SELECT reviewer, COUNT(*) n FROM audit WHERE tbl IN ('op_qc','poly_qc') GROUP BY reviewer"):
        reviewers[r["reviewer"]] = r["n"]
    today = dt.date.today().isoformat()
    today_n = Counter()
    for r in c.execute("SELECT reviewer, COUNT(DISTINCT rec) n FROM audit WHERE ts LIKE ? GROUP BY reviewer", (today + "%",)):
        today_n[r["reviewer"]] = r["n"]
    props = [dict(r) for r in c.execute("SELECT * FROM proposals ORDER BY status='open' DESC, id DESC")]
    return {"regions": {k: dict(v) for k, v in by_region.items()},
            "classes": [{"region": k[0], "qc_class": k[1], "n": n} for k, n in classes.items()],
            "reviewers": dict(reviewers), "today": dict(today_n), "proposals": props}


# --------------------------------------------------------------------------- exports

CT_FIELDS = ["qc_result", "qc_feature", "qc_issue", "qc_notes"]
CT_EXTRA = ["qc_site_valid", "qc_reviewer", "qc_date", "qc_status"]


def all_regions(c):
    return [r[0] for r in c.execute("SELECT DISTINCT region FROM ops ORDER BY region")]


def edited_regions(c):
    """Regions where anyone has saved something in the app (verdicts, polygon labels or missed ponds)."""
    q = """SELECT DISTINCT o.region FROM ops o WHERE o.op_key IN (SELECT rec FROM audit WHERE tbl='op_qc')
           OR o.op_key IN (SELECT p.op_key FROM polys p WHERE p.pid IN (SELECT rec FROM audit WHERE tbl IN ('poly_qc','polys')))
           ORDER BY 1"""
    return [r[0] for r in c.execute(q)]


def export_all(c, out_root, initials="RSL", strict=False, log=print, regions=None):
    """regions: list of region names to export (None = all)."""
    regions = list(regions) if regions else all_regions(c)
    known = set(all_regions(c))
    bad = [r for r in regions if r not in known]
    if bad:
        raise KeyError("Unknown region(s): %s" % ", ".join(bad))
    rq = "(%s)" % ",".join("?" * len(regions))
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    tag = regions[0].replace("/", "-") if len(regions) == 1 else ("all" if set(regions) == known else "%dregions" % len(regions))
    out = os.path.join(out_root, "export_%s_%s" % (stamp, tag))
    os.makedirs(os.path.join(out, "climate_trace"), exist_ok=True)
    os.makedirs(os.path.join(out, "research"), exist_ok=True)
    files = []
    warnings = []
    # 1. Climate TRACE operation-level files, one per region, same schema as *_qc_samples_complete_*.geojson
    for (region, prefix) in c.execute("SELECT DISTINCT region, file_prefix FROM ops WHERE region IN %s ORDER BY region" % rq,
                                      regions).fetchall():
        feats = []
        n_done = 0
        n_prop = n_nofeat = 0
        for r in c.execute("SELECT o.*, q.* FROM ops o JOIN op_qc q ON q.op_key=o.op_key "
                           "WHERE o.region=? AND o.file_prefix=? ORDER BY o.op_id", (region, prefix)):
            p = json.loads(r["props"])
            for k in CT_FIELDS:
                v = r[k]
                p[k] = v if v is not None else ("" if k == "qc_notes" else None)
            if not strict:
                for k in CT_EXTRA:
                    p[k] = r[k]
            if r["qc_result"]:
                n_done += 1
                if not r["qc_feature"]:
                    n_nofeat += 1
            if any(str(r[k] or "").startswith(PROPOSED) for k in ("qc_result", "qc_feature", "qc_issue")):
                n_prop += 1
            feats.append({"type": "Feature", "properties": p, "geometry": json.loads(r["geom"])})
        fn = os.path.join(out, "climate_trace", "%s_qc_samples_complete_%s.geojson" % (prefix, initials))
        with open(fn, "w") as f:
            json.dump({"type": "FeatureCollection", "name": os.path.basename(fn)[:-8],
                       "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
                       "features": feats}, f)
        files.append((fn, "%d/%d operations with a verdict" % (n_done, len(feats))))
        if n_prop:
            warnings.append("%s: %d operation(s) still carry an unresolved proposed label; resolve them on the Proposals tab "
                            "before sending to Climate TRACE" % (region, n_prop))
        if n_nofeat:
            warnings.append("%s: %d operation(s) have a result but no qc_feature" % (region, n_nofeat))
    # 2. Polygon QC GeoPackage in the exact international template, qc_* columns filled
    tpl = c.execute("SELECT v FROM meta WHERE k='gpkg_template'").fetchone()
    in_tpl = c.execute("SELECT COUNT(*) FROM polys p JOIN ops o ON o.op_key=p.op_key WHERE p.gpkg_fid IS NOT NULL "
                       "AND o.region IN %s" % rq, regions).fetchone()[0]
    if tpl and in_tpl:
        src = os.path.join(os.path.dirname(c.execute("PRAGMA database_list").fetchone()[2]), tpl[0])
        if os.path.exists(src):
            dst = os.path.join(out, "research", "International_BSI_Polygon_QC_%s.gpkg" % initials)
            shutil.copy2(src, dst)
            g = sqlite3.connect(dst)
            geo.register_gpkg_functions(g)
            n = 0
            for r in c.execute("SELECT p.pid, q.* FROM polys p JOIN poly_qc q ON q.pid=p.pid WHERE p.gpkg_fid IS NOT NULL"):
                g.execute("""UPDATE bsi_polygon_qc SET qc_class=?, qc_issue=?, qc_confidence=?, qc_facility_association=?,
                             qc_status=?, qc_reviewer=?, qc_notes=?, qc_date=? WHERE polygon_qc_id=?""",
                          (r["qc_class"], r["qc_issue"], r["qc_confidence"], r["qc_facility_association"],
                           r["qc_status"] or "not_reviewed", r["qc_reviewer"], r["qc_notes"], r["qc_date"], r["pid"]))
                n += 1 if r["qc_class"] else 0
            g.commit()
            g.close()
            files.append((dst, "%d polygons labelled (geometry and IDs untouched)" % n))
    # 2b. Same polygon-QC layout for regions that are not in the international package (e.g. newly pulled ones)
    cols = [("polygon_qc_id", "TEXT"), ("country", "TEXT"), ("season", "TEXT"), ("operation_id", "TEXT"),
            ("polygon_part", "INTEGER"), ("parts_in_operation", "INTEGER"), ("reported_bsi_pond_count", "INTEGER"),
            ("count_mismatch", "INTEGER"), ("geometry_sha256", "TEXT"), ("original_ponds_file", "TEXT"),
            ("qc_class", "TEXT"), ("qc_confidence", "TEXT"), ("qc_facility_association", "TEXT"), ("qc_reviewer", "TEXT"),
            ("qc_status", "TEXT"), ("qc_notes", "TEXT"), ("geometry_area_ha", "REAL"), ("qc_date", "TEXT"), ("qc_issue", "TEXT")]
    for (region, prefix) in c.execute("""SELECT DISTINCT o.region, o.file_prefix FROM ops o JOIN polys p ON p.op_key=o.op_key
                                         WHERE p.origin='bsi' AND p.gpkg_fid IS NULL AND o.region IN %s ORDER BY 1""" % rq,
                                      regions).fetchall():
        rows = []
        for r in c.execute("""SELECT p.*, q.*, o.country, o.season, o.op_id, o.pond_count, o.ops_file FROM polys p
                              JOIN poly_qc q ON q.pid=p.pid JOIN ops o ON o.op_key=p.op_key
                              WHERE o.region=? AND o.file_prefix=? AND p.origin='bsi' ORDER BY o.op_id, p.part""", (region, prefix)):
            ex = json.loads(r["extra"] or "{}")
            parts = ex.get("parts_in_operation")
            rows.append({"geometry": json.loads(r["geom"]), "polygon_qc_id": r["pid"], "country": r["country"],
                         "season": r["season"], "operation_id": r["op_id"], "polygon_part": r["part"],
                         "parts_in_operation": parts, "reported_bsi_pond_count": r["pond_count"],
                         "count_mismatch": int(parts is not None and parts != r["pond_count"]),
                         "geometry_sha256": r["sha"], "original_ponds_file": r["ops_file"].replace("_qc_samples.geojson", "_qc_samples_ponds.geojson"),
                         "qc_class": r["qc_class"], "qc_confidence": r["qc_confidence"],
                         "qc_facility_association": r["qc_facility_association"], "qc_reviewer": r["qc_reviewer"],
                         "qc_status": r["qc_status"] or "not_reviewed", "qc_notes": r["qc_notes"],
                         "geometry_area_ha": r["area_ha"], "qc_date": r["qc_date"], "qc_issue": r["qc_issue"]})
        if rows:
            dst = os.path.join(out, "research", "%s_BSI_Polygon_QC_%s.gpkg" % (prefix, initials))
            geo.write_gpkg(dst, "bsi_polygon_qc", rows, cols, "POLYGON", "BSI polygon-level QC (%s)" % region)
            files.append((dst, "%d polygons, %d labelled" % (len(rows), sum(1 for x in rows if x["qc_class"]))))
    # 3. Candidate library: every polygon (BSI + missed), all regions, with operation context
    rows = []
    for r in c.execute("""SELECT p.pid, p.op_key, p.part, p.origin, p.geom, p.area_ha, p.sha,
                                 q.qc_class, q.qc_issue, q.qc_confidence, q.qc_facility_association, q.qc_status,
                                 q.qc_reviewer, q.qc_notes, q.qc_date,
                                 o.region, o.country, o.season, o.op_id, o.sample_class, o.props,
                                 oq.qc_result AS op_qc_result, oq.qc_feature AS op_qc_feature, oq.qc_site_valid AS op_qc_site_valid
                          FROM polys p JOIN poly_qc q ON q.pid=p.pid JOIN ops o ON o.op_key=p.op_key
                          JOIN op_qc oq ON oq.op_key=o.op_key WHERE o.region IN %s
                          ORDER BY o.region, o.op_id, p.origin, p.part""" % rq, regions):
        props = json.loads(r["props"])
        g = json.loads(r["geom"])
        cx, cy = geo.centroid(g)
        d = {k: r[k] for k in r.keys() if k not in ("geom", "props")}
        d.update({"centroid_lon": round(cx, 6), "centroid_lat": round(cy, 6),
                  "bsi_threshold": props.get("bsi:threshold"), "bsi_footprint_mean": props.get("bsi:footprint_mean"),
                  "bsi_footprint_median": props.get("bsi:footprint_median"), "bsi_mean": props.get("bsi:mean"),
                  "bsi_valid_pixel_fraction": props.get("bsi:valid_pixel_fraction"),
                  "bsi_pond_count": props.get("bsi:pond_count"), "operation_source": props.get("operation:source"),
                  "stac_item_href": props.get("stac_item_href"),
                  "is_labelled": 1 if r["qc_class"] else 0,
                  "label_binary": ("manure" if r["qc_class"] in ("manure_pond", "possible_manure") else
                                   ("non_manure" if r["qc_class"] in ("wet_runoff", "other_water", "other_false_positive") else None)),
                  "geometry": g})
        rows.append(d)
    cols = [k for k in rows[0].keys() if k != "geometry"] if rows else []
    types = {k: ("REAL" if isinstance(rows[0][k], float) or k in ("area_ha", "centroid_lon", "centroid_lat")
                 or k.startswith("bsi_") and k != "bsi_pond_count" else
                 ("INTEGER" if k in ("part", "is_labelled", "bsi_pond_count") else "TEXT")) for k in cols}
    if rows:
        lib = os.path.join(out, "research", "bsi_candidate_library.gpkg")
        geo.write_gpkg(lib, "bsi_candidates", rows, [(k, types[k]) for k in cols], "POLYGON",
                       "BSI candidate polygons (detected + reviewer-marked missed) with QC labels")
        files.append((lib, "%d polygons (%d labelled)" % (len(rows), sum(r["is_labelled"] for r in rows))))
        csvp = os.path.join(out, "research", "bsi_candidate_library.csv")
        with open(csvp, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({k: r[k] for k in cols})
        files.append((csvp, "same table, no geometry"))
    # 4. audit log
    ap = os.path.join(out, "research", "qc_audit_log.csv")
    with open(ap, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "reviewer", "table", "record", "before", "after"])
        for r in c.execute("""SELECT ts, reviewer, tbl, rec, before, after FROM audit
                              WHERE rec IN (SELECT op_key FROM ops WHERE region IN %s)
                                 OR rec IN (SELECT p.pid FROM polys p JOIN ops o ON o.op_key=p.op_key WHERE o.region IN %s)
                                 OR tbl='proposals' ORDER BY id""" % (rq, rq), regions + regions):
            w.writerow(list(r))
    files.append((ap, "every save in these regions, with before/after"))
    log("Regions: %s" % ", ".join(regions))
    for fn, msg in files:
        log("  %s  (%s)" % (os.path.relpath(fn, out_root), msg))
    for wmsg in warnings:
        log("  WARNING: " + wmsg)
    return out, files, warnings
