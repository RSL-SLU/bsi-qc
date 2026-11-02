#!/usr/bin/env python3
"""BSI QC: one review pass that produces both the Climate TRACE operation-level QC and the
polygon-level candidate library.

  python bsi_qc.py ingest --data /path/to/BSI_QC_Data --gpkg /path/to/International_BSI_Polygon_QC.gpkg
  python bsi_qc.py serve                    # opens http://localhost:8765
  python bsi_qc.py serve --host 0.0.0.0     # let a labmate on the same network connect
  python bsi_qc.py export --initials LM_AP

All state lives in one folder (default ./qc_workspace): qc.sqlite, templates/, cache/, exports/.
"""
import argparse
import os
import sys
import webbrowser

from bsi_qc import core

DEFAULT_LAYERS = "tci,bsi-e,bsi-mask"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", default=os.environ.get("BSI_QC_WORKSPACE", "qc_workspace"),
                    help="folder holding qc.sqlite, cache and exports (can be on a shared drive)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("ingest", help="load (or refresh) regions; safe to re-run, never overwrites app reviews")
    a.add_argument("--data", required=True, help="BSI_QC_Data folder (contains AUS/, CAN/, USA/texas/ ...)")
    a.add_argument("--gpkg", help="International_BSI_Polygon_QC.gpkg (authoritative polygon IDs)")

    s = sub.add_parser("serve", help="run the review app")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true")

    pl = sub.add_parser("pull", help="download a region's QC files from the bucket (report, qc folder or gs:// link) and ingest it")
    pl.add_argument("url", help="run report .html, qc/cohort-N/ folder URL, or gs:// path")
    pl.add_argument("--data", default="data", help="local data folder (default: data)")
    pl.add_argument("--region", help="override region name (default: taken from the URL, e.g. RUS)")
    pl.add_argument("--prefetch", action="store_true", help="also cache TCI, BSI-E and the BSI mask for every operation now")

    pf = sub.add_parser("prefetch", help="download and cache imagery for a region so reviewing is instant/offline")
    pf.add_argument("--region", required=True)
    pf.add_argument("--layers", default=DEFAULT_LAYERS, help="comma list from tci,bsi-e,bsi-mask,ndvi,savi,ndbi,ndci")

    f = sub.add_parser("fetch-ponds", help="download pond polygons for BSI-positive operations that have no local ponds file (e.g. US states)")
    f.add_argument("--region", help="only this region, e.g. USA/texas")

    e = sub.add_parser("export", help="write Climate TRACE files, polygon GeoPackage and candidate library")
    e.add_argument("--initials", default="RSL", help="suffix for file names, e.g. RSL_LM_AP")
    e.add_argument("--strict", action="store_true",
                   help="Climate TRACE files carry only qc_result/feature/issue/notes (no site_valid, reviewer, date)")
    eg = e.add_mutually_exclusive_group()
    eg.add_argument("--region", action="append", help="region to export (repeatable), e.g. --region RUS")
    eg.add_argument("--all", action="store_true", help="export every region")

    args = ap.parse_args()
    ws = os.path.abspath(args.workspace)
    os.makedirs(ws, exist_ok=True)
    db_path = os.path.join(ws, "qc.sqlite")
    # the vocabulary lives with the shared data, so promoted labels reach every reviewer
    ws_vocab = os.path.join(ws, "vocab.json")
    if not os.path.exists(ws_vocab):
        import shutil
        shutil.copy2(core.VOCAB_PATH, ws_vocab)
    core.VOCAB_PATH = ws_vocab

    if args.cmd == "ingest":
        report = os.path.join(ws, "import_report.csv")
        core.ingest(db_path, args.data, args.gpkg, report)
        print("Vocabulary fixes for already-completed regions: %s" % report)
    elif args.cmd == "serve":
        n = core.connect(db_path).execute("SELECT COUNT(*) FROM ops").fetchone()[0] if os.path.exists(db_path) else 0
        if not n:
            sys.exit("Nothing ingested yet. Run first:\n  python bsi_qc.py ingest --data <BSI_QC_Data folder> --gpkg <International_BSI_Polygon_QC.gpkg>")
        from bsi_qc.server import create_app
        app = create_app(db_path, os.path.join(ws, "cache"), os.path.join(ws, "exports"))
        url = "http://%s:%d" % ("localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host, args.port)
        print("BSI QC running at %s  (workspace: %s)" % (url, ws))
        if args.host == "0.0.0.0":
            print("Labmates on the same network can open http://<this-computer's-IP>:%d" % args.port)
        if not args.no_browser:
            webbrowser.open(url)
        app.run(host=args.host, port=args.port, threaded=True)
    elif args.cmd in ("pull", "prefetch"):
        if args.cmd == "pull":
            from bsi_qc import remote
            print("Pulling QC files ...")
            region, dest = remote.pull(args.url, args.data, args.region)
            print("Saved to %s" % dest)
            core.ingest(db_path, args.data, None, os.path.join(ws, "import_report.csv"))
        if args.cmd == "prefetch" or args.prefetch:
            prefetch(db_path, ws, args.region if args.cmd == "prefetch" else region,
                     (getattr(args, "layers", None) or DEFAULT_LAYERS).split(","))
    elif args.cmd == "fetch-ponds":
        from bsi_qc import rasters
        import json
        c = core.connect(db_path)
        keys = core.ops_missing_ponds(c, args.region)
        ok = bad = 0
        for i, k in enumerate(keys, 1):
            props = json.loads(c.execute("SELECT props FROM ops WHERE op_key=?", (k,)).fetchone()[0])
            try:
                core.add_remote_ponds(c, k, rasters.fetch_json(props["asset:bsi-ponds"], os.path.join(ws, "cache")))
                ok += 1
            except Exception as ex:  # keep going; the app retries when the operation is opened
                bad += 1
                print("  %s: %s" % (k, ex))
            if i % 25 == 0:
                print("  %d/%d" % (i, len(keys)))
        print("Fetched pond polygons for %d operations (%d failed)" % (ok, bad))
    elif args.cmd == "export":
        c = core.connect(db_path)
        if args.all:
            regions = None
        elif args.region:
            regions = args.region
        else:
            regions = core.edited_regions(c)
            if not regions:
                sys.exit("Nothing has been reviewed in the app yet. Use --region <name> or --all.")
            print("No --region given: exporting regions with app edits (%s). Use --all for everything." % ", ".join(regions))
        out, _, _ = core.export_all(c, os.path.join(ws, "exports"), args.initials, args.strict, regions=regions)
        print("Wrote %s" % out)


def prefetch(db_path, ws, region, layers):
    import json
    from concurrent.futures import ThreadPoolExecutor
    from bsi_qc import rasters
    c = core.connect(db_path)
    rows = [(r[0], json.loads(r[1])) for r in c.execute("SELECT op_key, props FROM ops WHERE region=?", (region,))]
    if not rows:
        sys.exit("No operations for region %r" % region)
    jobs = [(k, p, l.strip()) for k, p in rows for l in layers if l.strip()]
    cache = os.path.join(ws, "cache")
    done = [0, 0]

    def one(job):
        k, p, l = job
        try:
            rasters.get_layer(p, l, cache)
            done[0] += 1
        except Exception as ex:
            done[1] += 1
            print("  %s %s: %s" % (k, l, ex))

    with ThreadPoolExecutor(8) as ex:
        for i, _ in enumerate(ex.map(one, jobs), 1):
            if i % 50 == 0:
                print("  %d/%d" % (i, len(jobs)))
    print("Cached %d rasters for %s (%d failed)" % (done[0], region, done[1]))


if __name__ == "__main__":
    main()
