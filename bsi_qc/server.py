"""Local web server for BSI QC. Run via `python bsi_qc.py serve`."""
import json
import os
import traceback

from flask import Flask, g, jsonify, request, send_from_directory

from . import core, rasters

HERE = os.path.dirname(os.path.abspath(__file__))


def create_app(db_path, cache_dir, export_dir):
    app = Flask(__name__, static_folder=os.path.join(HERE, "static"), static_url_path="/static")
    app.config.update(DB=db_path, CACHE=cache_dir, EXPORT=export_dir)

    def db():
        if "db" not in g:
            g.db = core.connect(app.config["DB"])
        return g.db

    @app.teardown_appcontext
    def _close(_):
        d = g.pop("db", None)
        if d is not None:
            d.close()

    def body():
        return request.get_json(force=True, silent=True) or {}

    def reviewer():
        r = (body().get("reviewer") or request.headers.get("X-Reviewer") or "").strip()
        if not r:
            raise PermissionError("Set your reviewer initials first.")
        return r[:20]

    @app.errorhandler(Exception)
    def _err(e):
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):  # plain 404s etc. (e.g. favicon) are not server errors
            return e
        if isinstance(e, core.Conflict):
            return jsonify(error="conflict", current=e.current,
                           message="Someone else saved this record since you opened it."), 409
        if isinstance(e, PermissionError):
            return jsonify(error="forbidden", message=str(e)), 403
        if isinstance(e, KeyError):
            return jsonify(error="not_found", message=str(e.args[0]) if e.args else "not found"), 404
        if isinstance(e, ValueError):
            return jsonify(error="invalid", message=str(e)), 400
        traceback.print_exc()
        return jsonify(error="server", message="%s: %s" % (type(e).__name__, e)), 500

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/api/vocab")
    def vocab():
        v = core.load_vocab()
        v["ramps"] = {k: rasters.ramp_css(k) for k in rasters.RAMPS}
        return jsonify(v)

    @app.get("/api/queue")
    def queue():
        return jsonify(core.queue(db()))

    @app.get("/api/op/<path:key>")
    def op(key):
        d = core.get_op(db(), key)
        if d is None:
            raise KeyError(key)
        return jsonify(d)

    @app.post("/api/op/<path:key>/qc")
    def save_op(key):
        b = body()
        core.save_op(db(), key, b.get("data", {}), reviewer(), b.get("version"))
        return jsonify(core.get_op(db(), key))

    @app.post("/api/poly/<path:pid>/qc")
    def save_poly(pid):
        b = body()
        core.save_poly(db(), pid, b.get("data", {}), reviewer(), b.get("version"))
        r = db().execute("SELECT op_key FROM polys WHERE pid=?", (pid,)).fetchone()
        return jsonify(core.get_op(db(), r["op_key"]))

    @app.post("/api/op/<path:key>/missed")
    def add_missed(key):
        b = body()
        core.add_missed(db(), key, b["geometry"], reviewer(), b.get("qc_class"))
        return jsonify(core.get_op(db(), key))

    @app.post("/api/poly/<path:pid>/delete")
    def del_poly(pid):
        r = db().execute("SELECT op_key FROM polys WHERE pid=?", (pid,)).fetchone()
        if not r:
            raise KeyError("No polygon %s" % pid)
        core.delete_missed(db(), pid, reviewer())
        return jsonify(core.get_op(db(), r["op_key"]))

    @app.post("/api/op/<path:key>/load_ponds")
    def load_ponds(key):
        o = db().execute("SELECT props FROM ops WHERE op_key=?", (key,)).fetchone()
        if not o:
            raise KeyError("No operation %s" % key)
        url = json.loads(o["props"]).get("asset:bsi-ponds")
        if not url:
            raise KeyError("No asset:bsi-ponds link for this operation")
        fc = rasters.fetch_json(url, app.config["CACHE"])
        core.add_remote_ponds(db(), key, fc)
        return jsonify(core.get_op(db(), key))

    @app.get("/api/op/<path:key>/layer/<layer>")
    def layer(key, layer):
        o = db().execute("SELECT props FROM ops WHERE op_key=?", (key,)).fetchone()
        if not o:
            raise KeyError(key)
        if layer not in rasters.LAYERS:
            raise KeyError(layer)
        try:
            return jsonify(rasters.get_layer(json.loads(o["props"]), layer, app.config["CACHE"],
                                             request.args.get("stretch", "1") == "1"))
        except KeyError:
            raise
        except Exception as e:  # network or decode failure: report, the UI keeps the basemap
            return jsonify(error="raster", message="%s: %s" % (type(e).__name__, e)), 502

    @app.get("/api/stats")
    def stats():
        return jsonify(core.stats(db()))

    @app.post("/api/proposals/<int:pid>")
    def proposal(pid):
        b = body()
        core.resolve_proposal(db(), pid, b["action"], b.get("value"), reviewer(), b.get("label"), b.get("help", ""))
        return jsonify(core.stats(db()))

    @app.get("/api/regions")
    def regions():
        return jsonify(all=core.all_regions(db()), edited=core.edited_regions(db()))

    @app.post("/api/export")
    def export():
        b = body()
        out, files, warns = core.export_all(db(), app.config["EXPORT"], (b.get("initials") or "RSL").strip() or "RSL",
                                            bool(b.get("strict")), log=lambda *a: None, regions=b.get("regions") or None)
        return jsonify(folder=os.path.abspath(out), warnings=warns,
                       files=[{"path": os.path.relpath(f, out), "note": n} for f, n in files])

    return app
