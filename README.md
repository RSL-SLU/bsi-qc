# BSI QC

One review pass gives you both outputs:

- **Climate TRACE**: the operation-level `*_qc_samples_complete_*.geojson` per region, same schema as before.
- **Research**: a polygon-level candidate library (manure ponds vs false positives, plus the ponds BSI missed) across every region, and the international `International_BSI_Polygon_QC.gpkg` with its `qc_*` columns filled in.

Reviewers label each candidate polygon. The operation verdict is suggested from those labels and the reviewer confirms or overrides it. Everything is picked from controlled lists, and an "other..." option records proposed new labels for later review.

## Getting started (new reviewer, Windows + conda)

```
git clone <repo URL>
cd bsi-qc
conda create -n bsi_app python=3.11 -y
conda activate bsi_app
pip install -r requirements.txt
```

Then load data. You don't need everything, only the region you'll review:

- **A region from the bucket** (e.g. Russia), no other files needed:
  ```
  python bsi_qc.py pull "<run report link>" --prefetch
  ```
- **The already-QC'd regions** (AUS, CAN, CHN, DEU, FRA, TUR, US states): copy the `data` folder from the lab drive into the repo folder, then
  ```
  python bsi_qc.py ingest --data data --gpkg data\International_BSI_Polygon_QC.gpkg
  ```

Start the app with `python bsi_qc.py serve` (or double-click `start_qc.bat`), enter your initials, and pick your region.

To get updates later: `git pull` (your `data/` and `qc_workspace/` are never touched by git).

**Important:** each copy of the app has its own review database in `qc_workspace/`. If two people review the same region, one of you hosts (see "Working together" below) instead of both reviewing on separate laptops. Otherwise you end up with two separate sets of answers.

## Loading data in more detail

```
python bsi_qc.py ingest --data data --gpkg data\International_BSI_Polygon_QC.gpkg
python bsi_qc.py fetch-ponds        # optional: pre-downloads pond polygons for the US states
```

`ingest` reads every `<region>/qc/` folder:

| File | Used for |
|---|---|
| `*_qc_samples.geojson` (or the complete file for US states) | operations and footprints |
| `qc/processed/*complete*.geojson` | existing verdicts, imported and locked |
| `*_qc_samples_ponds.geojson` | BSI pond polygons |
| the GeoPackage | authoritative `polygon_qc_id`s (and any QC already done in QGIS) |

Re-running `ingest` is safe. It adds new regions and never overwrites reviews made in the app.

Typos in the old files (`"correct "`, `uncertin`, `reesolution`, `water_body`, and so on) are cleaned on import. Every change is listed in `qc_workspace/import_report.csv` so you can check it. Notes like "possible CAFO FP" or "does not look like a cattle farm" set `qc_site_valid = no`, and those rows are flagged in the report for confirmation.

## Adding a new region straight from the bucket

No gcloud needed. Paste the run report link (or the `qc/cohort-N/` folder, or its `gs://` path):

```
python bsi_qc.py pull "https://storage.googleapis.com/trace-cafo-runs-prd/RUS/2025-06-01_2025-09-03/reports/20261028-1859-rus-80e3.html" --prefetch
```

This:

1. Downloads only the small QC files (samples, ponds, csv, VRTs) into `data/RUS/qc/`.
2. Ingests them. Existing reviews in other regions are left alone.
3. With `--prefetch`, caches TCI, BSI-E and the BSI mask for every sampled operation, so reviewing is instant and works on a shaky connection.

You do **not** need the big `outputs/` folder; the app fetches each operation's tiles itself. Another cohort of the same region (`cohort-2`) can be pulled into the same folder; its files have their own prefix and are tracked separately.

To cache more layers later: `python bsi_qc.py prefetch --region RUS --layers tci,bsi-e,bsi-mask,ndvi,ndci` (default: `tci,bsi-e,bsi-mask`).

## Daily use

```
python bsi_qc.py serve
```

On macOS you can double-click `start_qc.command` instead; on Windows, `start_qc.bat`. The app opens at http://localhost:8765. Enter your initials the first time.

1. Filter the queue: region, "to do", pond / no-pond.
2. For each polygon pick a class (keys 1 to 8), then issue, confidence and association. Association is pre-filled from whether the polygon sits inside the footprint. Saves happen on every click.
3. Toggle TCI / BSI-E / **BSI mask** / NDVI / SAVI / NDBI / NDCI. The BSI mask shows in white the pixels where BSI-E is **below** the operation's threshold (0.3): ponds are dark targets, so this is exactly what the pipeline turned into pond polygons (blobs of 5+ pixels). White speckle with no polygon = blobs too small to keep. You can also change opacity, or switch the basemap (Esri, Google, OSM). "Google Maps" opens high-res imagery at the site.
4. **+ Mark missed pond** (key `m`): draw a pond BSI missed, then label it.
5. Check the suggested operation verdict, answer "Is this a cattle operation?", then **Save & next**.

Keys: `1`-`8` class, `n`/`p` polygon, `]`/`[` operation, `t` TCI, `b` BSI-E, `k` BSI mask, `0` basemap only, `o` outlines, `f` follow-up, `z` zoom.

Imagery: the server fetches each operation's small COGs from the public `trace-cafo-runs-prd` bucket and caches them in `qc_workspace/cache/`, so the first open of an operation needs internet and later opens are instant.

## Working together

All state lives in one folder, `qc_workspace/` (database, vocabulary, cache, exports). Two ways to share it:

- **A. One machine hosts (recommended).** One person runs `python bsi_qc.py serve --host 0.0.0.0`. The other opens `http://<host-ip>:8765` on the same network (SLU wired/VPN). There's one database, and it's always consistent.
- **B. Shared network drive.** Both run the app with `--workspace "//server/share/qc_workspace"`. This works for two people, but **do not use a OneDrive/Box/Dropbox sync folder**. Sync clients can corrupt an SQLite file that two people write to.

Either way, every save records the reviewer and time, and the full before/after goes into the audit log. If two people edit the same record, the second save is refused and that reviewer sees the other person's version, so nobody silently overwrites anyone.

## Proposals

"other..." on any field stores `proposed:<label>` and lists it under **Proposals**. From there you can:

- **Promote**: adds the label to the vocabulary for everyone.
- **Map**: replaces it with an existing value on every record that used it.
- **Reject**: clears it. Not allowed on imported (already completed) verdicts, because it would blank a field in the Climate TRACE file; use Map there.

Resolve open proposals before exporting for Climate TRACE: the export warns if any `proposed:` labels are left.

## Export

Use the **Export** tab and tick the regions you want (it pre-selects the regions where someone saved something), or on the command line:

```
python bsi_qc.py export --region RUS --initials RSL_AP     # one region (repeat --region for more)
python bsi_qc.py export --all --initials RSL_AP            # everything
python bsi_qc.py export --initials RSL_AP                  # regions with app edits
```

Each export creates a new folder, `qc_workspace/exports/export_<timestamp>_<region>/`, with files only for the chosen regions:

- `climate_trace/<REGION>-<year>_qc_samples_complete_<initials>.geojson`: original properties plus `qc_result`, `qc_feature`, `qc_issue`, `qc_notes`, and also `qc_site_valid`, `qc_reviewer`, `qc_date`, `qc_status` unless you choose strict mode.
- `research/International_BSI_Polygon_QC_<initials>.gpkg`: the international package with qc_* filled in. Geometry and IDs are untouched.
- `research/<REGION>-<year>_<cohort>_BSI_Polygon_QC_<initials>.gpkg`: the same polygon-QC layout (layer `bsi_polygon_qc`) for regions that aren't in the international package, such as a newly pulled RUS.
- `research/bsi_candidate_library.gpkg` and `.csv`: every polygon (detected `p###` and missed `m###`) with labels, the operation verdict, BSI stats, centroid, and a `label_binary` column (manure / non_manure) for training.
- `research/qc_audit_log.csv`: every save, with who, when, and before/after values.

## Vocabulary

`qc_workspace/vocab.json` holds the vocabulary. It follows the two QC guides, with these changes:

- `qc_site_valid` (yes / no / unsure) on operations, so facility-location false positives don't count against BSI accuracy.
- `other_false_positive` is allowed as an operation feature.
- "too small to detect" is recorded as `missed_small_feature`.
