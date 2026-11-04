# BSI QC app

Local web app for Climate TRACE BSI (Bovine Slurry Index) QC at the Remote Sensing Lab (RSL), Saint Louis University. One review pass produces:
- **Operation-level QC** for Climate TRACE: `*_qc_samples_complete_<initials>.geojson` per region (same schema as the legacy QGIS files).
- **Polygon-level QC** for research: `bsi_polygon_qc` GeoPackages, plus a cross-region candidate library (manure ponds vs false positives, and reviewer-marked missed ponds).

Used by several lab reviewers. Windows + conda env `bsi_app` is the usual setup; run from the repo root.

## Commands
```
python bsi_qc.py ingest --data data --gpkg data\International_BSI_Polygon_QC.gpkg   # idempotent; never overwrites app reviews
python bsi_qc.py pull "<run report .html | qc/cohort-N/ URL | gs:// path>" --prefetch # new region from the public bucket
python bsi_qc.py prefetch --region RUS [--layers tci,bsi-e,bsi-mask,ndvi]   # default tci,bsi-e,bsi-mask
python bsi_qc.py fetch-ponds [--region USA/texas]   # regions without a local *_qc_samples_ponds.geojson
python bsi_qc.py serve [--host 0.0.0.0]              # http://localhost:8765
python bsi_qc.py export --initials RSL_XY [--region RUS ...|--all] [--strict]   # default: regions with app edits
```
State lives in `qc_workspace/` (qc.sqlite, vocab.json, cache/, exports/, import_report.csv). `data/` and `qc_workspace/` are git-ignored.

## Layout
- `bsi_qc.py`: CLI.
- `bsi_qc/core.py`: SQLite schema, ingest, legacy vocabulary normalisation, verdict roll-up (`suggest`), saves with optimistic locking (`version`) and audit log, proposals, exports.
- `bsi_qc/rasters.py`: fetches per-operation COGs from `asset:*` URLs (server side, so no bucket CORS needed), caches them, and renders PNG overlays plus bounds. Layers: tci, bsi-e, bsi-mask (binary BSI-E < `bsi:threshold`: ponds are dark targets, white = below threshold, from asset:bsi-e; threshold is part of the render cache key), ndvi, savi, ndbi, ndci. Honours RasterPixelIsPoint (half-pixel shift); non-4326 rasters are reprojected to a lon/lat grid with pyproj (nearest neighbour), or fail with a clear message if pyproj is missing. Bump `RENDER_VERSION` when rendering changes so cached PNGs are rebuilt.
- `bsi_qc/remote.py`: `pull` resolves report/folder/gs:// links to the qc files and downloads only small files (samples, ponds, csv, VRTs).
- `bsi_qc/geo.py`: GPKG blob <-> GeoJSON, area, minimal GeoPackage writer. Registers ST_* functions that GPKG rtree triggers need.
- `bsi_qc/static/`: index.html, app.js, app.css; Leaflet + Leaflet.draw vendored in `static/vendor/`.
- `bsi_qc/vocab.json`: default vocabulary, copied to `qc_workspace/vocab.json` on first run (that copy is the live one).

## Data conventions
- Region = folder path above `qc/` (e.g. `AUS`, `USA/texas`, `RUS`). op_key = `<region>:<id>`.
- Sample sets are keyed by file prefix (`AUS-2024`, `RUS-2025_cohort-1`), so several cohorts can share one `qc/` folder.
- Polygon IDs: `<COUNTRY>_<detection_id>_p###` for BSI parts (matches the international gpkg), `..._m###` for reviewer-drawn missed ponds. US state regions (`USA/<state>`) use the state folder name instead of the country, e.g. `texas_<detection_id>_p001`. Existing IDs are never renamed.
- `pull` maps bucket region names like `usa.44_1_texas` / `usa_17_1_kansas` to an existing local `USA/<state>`; `--region` always wins.
- `qc_site_valid` is required on every app-saved verdict (never defaulted, not filled by "Use suggestion"). Null on imported rows = not assessed. Part order = order inside the ponds MultiPolygon (verified against the gpkg).
- Legacy verdicts in `qc/processed/*complete*.geojson` (not `dump/`) are imported locked; fixes are logged in import_report.csv.

## Decisions (agreed in the lab)
- Operation verdict is **pre-filled** from polygon labels; the reviewer confirms or overrides it.
- Added `qc_site_valid` (yes/no/unsure): "possible CAFO FP / not a cattle farm" is a facility-location error, not a BSI error.
- No `below_resolution` value: "too small to detect" / "resolution" maps to `missed_small_feature`.
- Polygon "uncertain" vs operation "unclear" are kept as in the two guides.
- "other..." stores `proposed:<label>`, which is promoted, mapped or rejected on the Proposals tab.

## Status
- Verified on real RUS cohort-1 data: rasters and pond polygons align at 0 m; mask white = BSI-E < 0.3 covers 100% of pond pixels.
- Export is region-scoped (UI picker / `--region`); warns about unresolved `proposed:` labels and verdicts with no qc_feature.
- Rejecting a proposal that sits on an imported (legacy) verdict is refused; use Map instead.
- Each clone has its own qc_workspace (separate DB). Shared review = one host with `serve --host 0.0.0.0`.

## Style
No em dashes or en dashes in user-facing text. Keep UI copy short and plain.
