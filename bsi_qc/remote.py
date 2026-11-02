"""Pull a region's QC sample files straight from the public trace-cafo-runs-prd bucket (no gcloud needed).

Accepts any of:
  - a run report:  https://storage.googleapis.com/trace-cafo-runs-prd/RUS/2025-06-01_2025-09-03/reports/<run>.html
  - a qc folder:   https://storage.googleapis.com/trace-cafo-runs-prd/RUS/2025-06-01_2025-09-03/qc/cohort-1/
  - the gs:// form: gs://trace-cafo-runs-prd/RUS/2025-06-01_2025-09-03/qc/cohort-1/
Only the small QC files are downloaded (samples, ponds, csv, VRTs). Per-operation rasters are fetched by the
app on demand, so the big outputs/ folder is never needed.
"""
import json
import os
import re
import urllib.parse
import urllib.request

GCS = os.environ.get("BSI_QC_GCS", "https://storage.googleapis.com/")
QC_FILE_RE = re.compile(r"[\w.\-]+_qc_samples(?:_ponds)?\.(?:geojson|csv)|[\w.\-]+_(?:bsi-e|tci|ndvi|savi|ndbi|ndci)\.vrt")


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "bsi-qc/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _norm(url):
    url = url.strip()
    if url.startswith("gs://"):
        url = GCS + url[5:]
    url = url.replace("https://console.cloud.google.com/storage/browser/", GCS)
    return url


def _split(url):
    """https://storage.googleapis.com/<bucket>/<path> -> (bucket, path)"""
    rest = url[len(GCS):] if url.startswith(GCS) else url
    bucket, _, path = rest.partition("/")
    return bucket, urllib.parse.unquote(path)


def list_folder(qc_url):
    """List object names in a public folder via the JSON API. Returns [] if listing is not allowed."""
    bucket, path = _split(qc_url)
    if not path.endswith("/"):
        path += "/"
    names, token = [], None
    while True:
        q = {"prefix": path, "fields": "items(name),nextPageToken"}
        if token:
            q["pageToken"] = token
        try:
            j = json.loads(_get("%sstorage/v1/b/%s/o?%s" % (GCS, bucket, urllib.parse.urlencode(q))))
        except Exception:
            return []
        names += [i["name"] for i in j.get("items", [])]
        token = j.get("nextPageToken")
        if not token:
            return names


def resolve(url):
    """Return (qc_folder_url, [file names]) for a report, folder or gs:// link."""
    url = _norm(url)
    if url.endswith(".html"):
        html = _get(url).decode("utf-8", "replace")
        m = re.search(r"(" + re.escape(GCS) + r"[^\s\"'<>]+?/qc/[^\s\"'<>/]+/)", html) or \
            re.search(r"gs://([^\s\"'<>]+?/qc/[^\s\"'<>/]+/)", html)
        if not m:
            raise SystemExit("Could not find a qc/ folder link in the report.")
        folder = m.group(1) if m.group(1).startswith("http") else GCS + m.group(1)
        files = sorted(set(QC_FILE_RE.findall(html)))
    else:
        folder = _folder_of(url)
        files = []
    listed = [n.rsplit("/", 1)[-1] for n in list_folder(folder)]
    files = sorted(set(files) | {f for f in listed if QC_FILE_RE.fullmatch(f)})
    if not files:
        files = _guess(folder)
    if not any(f.endswith("_qc_samples.geojson") for f in files):
        raise SystemExit("No *_qc_samples.geojson found under %s" % folder)
    return folder, files


def _folder_of(url):
    """A link to a file (last segment has an extension) -> its folder; anything else is a folder itself,
    with or without the trailing slash (.../qc/cohort-1 and .../qc/cohort-1/ are the same)."""
    url = url.split("?", 1)[0].split("#", 1)[0]
    if url.endswith("/"):
        return url
    last = url.rsplit("/", 1)[-1]
    if re.search(r"\.(geojson|json|csv|vrt|tif|tiff|html)$", last, re.I):
        return url.rsplit("/", 1)[0] + "/"
    return url + "/"


def _guess(folder):
    """Listing not allowed and no report: try the pipeline's standard names, e.g. RUS-2025_cohort-1_qc_samples.geojson."""
    _, path = _split(folder)
    parts = path.strip("/").split("/")
    region, window, cohort = parts[0], (parts[1] if len(parts) > 1 else ""), parts[-1]
    year = window[:4]
    for pre in ("%s-%s_%s" % (region, year, cohort), "%s-%s" % (region, year)):
        try:
            _get(folder + pre + "_qc_samples.geojson", timeout=20)
        except Exception:
            continue
        return [pre + s for s in ("_qc_samples.geojson", "_qc_samples_ponds.geojson", "_qc_samples.csv")] + \
               ["%s_%s.vrt" % (pre, l) for l in ("bsi-e", "tci", "ndvi", "savi", "ndbi", "ndci")]
    return []


def region_from(folder):
    _, path = _split(folder)
    return path.split("/")[0]  # e.g. RUS, or usa.44_1_texas


US_STATE_RE = re.compile(r"^usa[._]\d+_\d+_(?P<state>[a-z_]+)$", re.I)


def local_region(bucket_region, data_root):
    """Map a bucket region name to the local folder convention when that folder already exists:
    usa.44_1_texas (or usa_17_1_kansas) -> USA/texas. Otherwise the bucket name is kept."""
    m = US_STATE_RE.match(bucket_region)
    if m:
        cand = "USA/" + m.group("state").lower()
        if os.path.isdir(os.path.join(data_root, "USA", m.group("state").lower())):
            return cand
    return bucket_region


def pull(url, data_root, region=None, log=print):
    folder, files = resolve(url)
    if not region:
        bucket_region = region_from(folder)
        region = local_region(bucket_region, data_root)
        if region != bucket_region:
            log("  region %s -> existing local region %s" % (bucket_region, region))
        elif US_STATE_RE.match(bucket_region):
            log("  no local USA/<state> folder for %s; saving as %s (use --region USA/<state> to override)"
                % (bucket_region, region))
    dest = os.path.join(data_root, *region.split("/"), "qc")
    os.makedirs(dest, exist_ok=True)
    for f in files:
        out = os.path.join(dest, f)
        try:
            data = _get(folder + urllib.parse.quote(f))
        except Exception as ex:
            if f.endswith(".vrt") or f.endswith(".csv"):
                log("  %-55s skipped (%s)" % (f, ex))
                continue
            raise
        tmp = out + ".part"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, out)
        log("  %-55s %8.1f KB" % (f, len(data) / 1024))
    return region, dest
