/* BSI QC review app */
(() => {
"use strict";

const S = {
  vocab: null, queue: [], view: [], cur: null, sel: null, layer: "tci", reviewer: null,
  rasterCache: {}, draft: null, unlock: false, pendingNotes: {}, notesSending: {},
  openKey: null,   // operation the user has open (or is opening); responses for other ops only refresh the queue row
  polyById: {},    // latest server copy of every polygon seen, so a queued save sends the newest version
  opVer: {},       // latest verdict version per op_key
  chains: {}, epoch: {},
};
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const PROPOSED = "proposed:";
const CLASS_COLORS = {
  manure_pond: "#16a34a", possible_manure: "#84cc16", manure_related_nonpond: "#a16207",
  wet_runoff: "#0ea5e9", other_water: "#2563eb", other_false_positive: "#ef4444",
  mixed_or_merged: "#f59e0b", uncertain: "#a3a3a3",
};
const UNLABELLED = "#22d3ee";
const STATE_LABEL = { not_started: "Not started", polys_pending: "Polygons pending", in_progress: "In progress", needs_followup: "Follow-up", done: "Done" };

// ------------------------------------------------------------------ utils
function toast(msg, err = false, ms = 2600) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = err ? "err" : "";
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add("hidden"), ms);
}
async function api(path, opts = {}) {
  const o = { headers: { "Content-Type": "application/json", "X-Reviewer": S.reviewer || "" }, ...opts };
  if (o.body && typeof o.body !== "string") o.body = JSON.stringify({ reviewer: S.reviewer, ...o.body });
  const r = await fetch(path, o);
  let j = null;
  try { j = await r.json(); } catch (e) { /* ignore */ }
  if (!r.ok) {
    const e = new Error((j && j.message) || r.statusText);
    e.status = r.status; e.data = j;
    throw e;
  }
  return j;
}
const enc = (k) => k.split("/").map(encodeURIComponent).join("/");

// Saves to one record run one after another, so each sends the version the previous save produced.
// A 409 can then only mean another reviewer saved in between. After a 409 the record's epoch is bumped,
// which drops saves still queued against the old version.
function enqueue(rec, job) {
  const ep = S.epoch[rec] || 0;
  const run = () => ((S.epoch[rec] || 0) === ep ? job() : false);
  const p = (S.chains[rec] || Promise.resolve()).then(run, run);
  S.chains[rec] = p.catch(() => {});
  return p;
}
function bumpEpoch(rec) { S.epoch[rec] = (S.epoch[rec] || 0) + 1; }
function remember(d) {
  S.opVer[d.op.op_key] = d.qc.version;
  d.polys.forEach((p) => { S.polyById[p.pid] = p; });
}
function setSaving(s) { $("#saveState").textContent = s; }
function vlabel(list, v) {
  if (v == null) return "";
  if (String(v).startsWith(PROPOSED)) return "proposed: " + v.slice(PROPOSED.length);
  const f = list.find((x) => x.value === v);
  return f ? f.label : v;
}
function lsGet(k, d) { try { return localStorage.getItem(k) ?? d; } catch (e) { return d; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } }

// ------------------------------------------------------------------ reviewer
function askReviewer() {
  const d = $("#dlgWho");
  $("#whoInput").value = S.reviewer || "";
  d.showModal();
  d.onclose = () => {
    const v = $("#whoInput").value.trim();
    if (v) { S.reviewer = v; lsSet("bsiqc.reviewer", v); $("#whoName").textContent = v; }
    else if (!S.reviewer) setTimeout(askReviewer, 50);
  };
}

// ------------------------------------------------------------------ map
let map, baseLayer, footLayer, polyLayer, rasterLayer, drawer;
const BASEMAPS = {
  esri: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", { maxZoom: 20, maxNativeZoom: 19, attribution: "Esri World Imagery" }],
  google: ["https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}", { maxZoom: 21, maxNativeZoom: 20, attribution: "Google" }],
  osm: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 20, maxNativeZoom: 19, attribution: "OpenStreetMap" }],
};
function initMap() {
  map = L.map("map", { zoomControl: true, attributionControl: true }).setView([30, 0], 2);
  setBasemap(lsGet("bsiqc.basemap", "esri"));
  $("#basemap").value = lsGet("bsiqc.basemap", "esri");
  footLayer = L.layerGroup().addTo(map);
  polyLayer = L.layerGroup().addTo(map);
  map.createPane("rasters"); map.getPane("rasters").style.zIndex = 350;
  map.on(L.Draw.Event.CREATED, async (e) => {
    const g = e.layer.toGeoJSON().geometry;
    try {
      const d = await api(`/api/op/${enc(S.cur.op.op_key)}/missed`, { method: "POST", body: { geometry: g } });
      const added = d.polys.filter((p) => p.origin === "missed").pop();
      if (d.op.op_key === S.openKey) S.sel = added && added.pid;
      applyOp(d);
      toast("Missed polygon added. Label it on the right.");
    } catch (err) { toast(err.message, true); }
  });
}
function setBasemap(k) {
  if (baseLayer) map.removeLayer(baseLayer);
  const [u, o] = BASEMAPS[k] || BASEMAPS.esri;
  baseLayer = L.tileLayer(u, o).addTo(map);
  lsSet("bsiqc.basemap", k);
}
function drawOp() {
  footLayer.clearLayers(); polyLayer.clearLayers();
  const d = S.cur;
  if (!d) return;
  L.geoJSON(d.op.geom, { style: { color: "#facc15", weight: 2, dashArray: "6 4", fill: false }, interactive: false }).addTo(footLayer);
  if (!$("#showPolys").checked) return;
  d.polys.forEach((p) => {
    const sel = p.pid === S.sel;
    const color = CLASS_COLORS[p.qc_class] || (p.qc_class ? "#f59e0b" : UNLABELLED);
    const lyr = L.geoJSON(p.geom, {
      style: { color: sel ? "#e0219b" : color, weight: sel ? 3.5 : 2, fillOpacity: sel ? 0.08 : 0.0, fill: true,
               dashArray: p.origin === "missed" ? "5 4" : null },
    });
    lyr.on("click", () => selectPoly(p.pid, true));
    const lbl = p.origin === "missed" ? "m" + p.part : "p" + p.part;
    lyr.bindTooltip(lbl, { permanent: true, direction: "center", className: "plabel" });
    lyr.addTo(polyLayer);
  });
}
function zoomOp() {
  if (!S.cur) return;
  const b = L.geoJSON(S.cur.op.geom).getBounds();
  map.fitBounds(b.pad(0.15), { maxZoom: 18 });
}

async function showRaster() {
  if (rasterLayer) { map.removeLayer(rasterLayer); rasterLayer = null; }
  $("#legend").classList.add("hidden");
  $("#rasterMsg").classList.add("hidden");
  $$("#rasterSeg button").forEach((b) => b.classList.toggle("on", b.dataset.l === S.layer));
  if (!S.cur || !S.layer) return;
  const key = S.cur.op.op_key, layer = S.layer, stretch = $("#stretch").checked ? 1 : 0;
  const ck = `${key}|${layer}|${stretch}`;
  let r = S.rasterCache[ck];
  if (!r) {
    setSaving("loading " + layer.toUpperCase() + "...");
    try {
      r = await api(`/api/op/${enc(key)}/layer/${layer}?stretch=${stretch}`);
      S.rasterCache[ck] = r;
    } catch (e) {
      setSaving("");
      if (S.cur && S.cur.op.op_key === key && S.layer === layer) {
        $("#rasterMsg").textContent = `${layer.toUpperCase()} could not be loaded (${e.message}). Basemap and outlines still work.`;
        $("#rasterMsg").classList.remove("hidden");
      }
      return;
    }
    setSaving("");
  }
  if (!S.cur || S.cur.op.op_key !== key || S.layer !== layer) return;
  rasterLayer = L.imageOverlay(r.png, r.bounds, { opacity: $("#opacity").value / 100, pane: "rasters", className: "pix" }).addTo(map);
  if (r.legend && r.legend.kind === "mask") {
    const pct = r.legend.pct_below;
    $("#legend").innerHTML = `<b>BSI MASK</b> <span class="muted">BSI-E &lt; ${esc(r.legend.threshold)}</span>
      <div><span class="sw on" style="background:#fff;outline:1px solid #999"></span>below threshold (pond signal)</div>
      <div><span class="sw off" style="background:rgba(0,0,0,.45)"></span>at or above threshold</div>
      <div class="muted">${pct == null ? "no valid pixels" : `${pct}% of valid pixels below`}; polygons = blobs of 5+ pixels</div>`;
    $("#legend").classList.remove("hidden");
  } else if (r.legend && r.legend.css) {
    const thr = S.cur.op.props["bsi:threshold"];
    $("#legend").innerHTML = `<b>${layer.toUpperCase()}</b> ${layer === "bsi-e" && thr != null ? `<span class="muted">threshold ${thr}</span>` : ""}
      <div class="bar" style="background:${r.legend.css}"></div>
      <div class="ends"><span>${r.legend.min}</span><span class="muted">data ${r.legend.data_min} to ${r.legend.data_max}</span><span>${r.legend.max}</span></div>`;
    $("#legend").classList.remove("hidden");
  }
}
function prefetch(key, layers) {
  const stretch = $("#stretch").checked ? 1 : 0;
  layers.forEach((l) => {
    const ck = `${key}|${l}|${stretch}`;
    if (S.rasterCache[ck]) return;
    api(`/api/op/${enc(key)}/layer/${l}?stretch=${stretch}`).then((r) => { S.rasterCache[ck] = r; }).catch(() => {});
  });
}

// ------------------------------------------------------------------ queue
async function loadQueue(keepScroll = true) {
  const q = await api("/api/queue");
  S.queue = q;
  const regions = [...new Set(q.map((r) => r.region))];
  const sel = $("#fRegion");
  if (sel.options.length <= 1) {
    regions.forEach((r) => sel.add(new Option(r, r)));
    sel.value = lsGet("bsiqc.region", "");
  }
  renderQueue(keepScroll);
}
function renderQueue(keepScroll) {
  const reg = $("#fRegion").value, st = $("#fState").value, cl = $("#fClass").value, s = $("#fSearch").value.trim().toLowerCase();
  S.view = S.queue.filter((r) => (!reg || r.region === reg) && (!cl || r.sample_class === cl)
    && (!st || (st === "todo" ? r.state !== "done" : r.state === st)) && (!s || r.op_id.toLowerCase().includes(s)));
  const ul = $("#queue"), top = ul.scrollTop;
  const curKey = S.cur && S.cur.op.op_key;
  ul.innerHTML = S.view.map((r, i) => {
    const total = r.n_bsi + r.n_missed;
    const pol = r.sample_class === "pond" || total ? `${Math.min(r.n_poly_done, total)}/${total || "?"} poly` : "";
    const who = [r.qc_reviewer && !String(r.qc_reviewer).startsWith("legacy") ? r.qc_reviewer : "", r.poly_reviewers || ""].filter(Boolean).join(",");
    return `<li data-i="${i}" class="${r.op_key === curKey ? "on" : ""}" title="${esc(STATE_LABEL[r.state])}">
      <span class="dot ${r.state}"></span>
      <div><div class="id">${esc(r.op_id)}</div>
        <div class="sub">${esc(r.region)} · ${pol} ${r.qc_result ? "· " + esc(r.qc_result) : ""} ${who ? "· " + esc([...new Set(who.split(","))].join(",")) : ""}</div></div>
      <span class="tag ${r.sample_class === "pond" ? "pond" : ""}">${r.sample_class === "pond" ? "pond" : "no-pond"}</span></li>`;
  }).join("");
  const done = S.view.filter((r) => r.state === "done").length;
  $("#queueCount").textContent = `${S.view.length} operations shown`;
  if (keepScroll) ul.scrollTop = top;
  $$("#queue li").forEach((li) => li.addEventListener("click", () => openOp(S.view[+li.dataset.i].op_key)));
}
function stepOp(dir) {
  if (!S.view.length) return;
  const i = S.cur ? S.view.findIndex((r) => r.op_key === S.cur.op.op_key) : -1;
  const n = S.view[Math.max(0, Math.min(S.view.length - 1, i + dir))];
  if (n) openOp(n.op_key);
}

// ------------------------------------------------------------------ operation
async function openOp(key, keepSel = false) {
  flushNotes();
  S.openKey = key;
  try {
    let d = await api(`/api/op/${enc(key)}`);
    if (S.openKey !== key) { applyOp(d); return; }  // user moved on while this loaded
    if (d.op.bsi_detected && !d.op.ponds_loaded) {
      setSaving("fetching pond polygons...");
      try { d = await api(`/api/op/${enc(key)}/load_ponds`, { method: "POST", body: {} }); }
      catch (e) { toast("Could not fetch pond polygons: " + e.message, true, 5000); }
      setSaving("");
      if (S.openKey !== key) { applyOp(d); return; }
    }
    const changed = !S.cur || S.cur.op.op_key !== key;
    if (changed) { S.unlock = false; S.draft = null; if (!keepSel) S.sel = null; }
    applyOp(d);
    if (changed) {
      if (!S.sel && d.polys.length) S.sel = d.polys[0].pid;
      zoomOp();
      showRaster();
      drawOp();
      renderDetail();
      $$("#queue li").forEach((li) => li.classList.toggle("on", S.view[+li.dataset.i] && S.view[+li.dataset.i].op_key === key));
      const li = $("#queue li.on"); if (li) li.scrollIntoView({ block: "nearest" });
      prefetch(key, ["bsi-e", "tci", "bsi-mask"]);
      const i = S.view.findIndex((r) => r.op_key === key);
      if (S.view[i + 1]) prefetch(S.view[i + 1].op_key, [S.layer || "tci"]);
    }
  } catch (e) {
    if (S.openKey === key) S.openKey = S.cur ? S.cur.op.op_key : null;
    toast(e.message, true);
  }
}
// Every server response with an operation goes through here. Only the open operation is drawn; a late
// response for another operation (a note saved on the way out, a slow fetch) just refreshes its queue row.
function applyOp(d) {
  remember(d);
  updateRow(d);
  if (d.op.op_key !== S.openKey) { renderQueue(true); return false; }  // show the other op's refreshed row
  S.cur = d;
  if (S.sel && !d.polys.some((p) => p.pid === S.sel)) S.sel = d.polys.length ? d.polys[0].pid : null;
  drawOp();
  renderDetail();
  return true;
}
function updateRow(d) {
  const row = S.queue.find((r) => r.op_key === d.op.op_key);
  if (row) {
    const total = d.polys.length;
    row.n_bsi = d.polys.filter((p) => p.origin === "bsi").length;
    row.n_missed = total - row.n_bsi;
    row.n_poly_done = d.polys.filter((p) => ["reviewed", "needs_followup"].includes(p.qc_status)).length;
    row.n_poly_follow = d.polys.filter((p) => p.qc_status === "needs_followup").length;
    row.ponds_loaded = d.op.ponds_loaded;
    Object.assign(row, { qc_status: d.qc.qc_status, qc_result: d.qc.qc_result, qc_reviewer: d.qc.qc_reviewer, origin: d.qc.origin, qc_site_valid: d.qc.qc_site_valid });
    row.state = stateOf(row);
  }
}
function stateOf(d) {
  const total = d.n_bsi + d.n_missed;
  const polysDone = d.n_poly_done >= total && (d.ponds_loaded || !d.bsi_detected);
  const opDone = ["reviewed", "needs_followup"].includes(d.qc_status);
  if ((d.qc_status === "needs_followup" || d.n_poly_follow) && opDone && polysDone) return "needs_followup";
  if (opDone && polysDone) return "done";
  if (opDone && d.origin && d.origin.startsWith("legacy")) return "polys_pending";
  if (opDone || d.n_poly_done || d.qc_reviewer) return "in_progress";
  return "not_started";
}

function chips(list, value, opts = {}) {
  const { field, keyed, suggested, propose = true, restrict } = opts;
  let items = restrict ? list.filter((x) => restrict.includes(x.value)) : list;
  let h = items.map((x) => {
    const on = x.value === value, sg = !on && suggested === x.value;
    return `<button type="button" class="chip ${on ? "on" : ""} ${sg ? "sugg" : ""}" data-f="${field}" data-v="${esc(x.value)}" title="${esc(x.help || "")}">${keyed && x.key ? `<span class="k">${x.key}</span>` : ""}${esc(x.label)}</button>`;
  }).join("");
  if (value && String(value).startsWith(PROPOSED)) {
    h += `<button type="button" class="chip prop on" data-f="${field}" data-v="${esc(value)}">${esc(value.slice(PROPOSED.length))}</button>`;
  }
  if (propose) h += `<button type="button" class="chip prop" data-f="${field}" data-propose="1" title="Propose a new label">other...</button>`;
  return `<div class="chips">${h}</div>`;
}

function renderDetail() {
  const d = S.cur;
  $("#empty").classList.toggle("hidden", !!d);
  $("#detail").classList.toggle("hidden", !d);
  if (!d) return;
  const V = S.vocab, p = d.op.props, q = d.qc;
  const ctr = L.geoJSON(d.op.geom).getBounds().getCenter();
  const lat = p["operation:latitude"] ?? p["centroid_lat"] ?? ctr.lat.toFixed(6), lon = p["operation:longitude"] ?? p["centroid_lon"] ?? ctr.lng.toFixed(6);
  const i = S.view.findIndex((r) => r.op_key === d.op.op_key);
  const mapUrl = p.map_url || (p.stac_item_href ? "https://developmentseed.org/stac-map/?href=" + p.stac_item_href : null);
  let h = `<div class="ophead">
    <div class="navrow"><button data-act="prev" title="[">&larr; Prev</button><span class="muted small">${i + 1} of ${S.view.length}</span><button data-act="next" title="]">Next &rarr;</button></div>
    <h2 style="margin-top:8px">${esc(d.op.op_id)}</h2>
    <div class="tags"><span class="tag">${esc(d.op.region)}</span><span class="tag">${esc(d.op.season)}</span>
      <span class="tag ${d.op.sample_class === "pond" ? "pond" : ""}">BSI ${esc(d.op.sample_class)}</span>
      ${q.origin && q.origin.startsWith("legacy") ? `<span class="tag legacy">verdict imported</span>` : ""}
      ${p["bsi:aoi_clipped"] ? `<span class="tag">AOI clipped</span>` : ""}
      ${d.polys.some((x) => x.extra && x.extra.count_mismatch) ? `<span class="tag warn" title="parts in operation differ from reported pond count">count mismatch</span>` : ""}</div>
    <div class="kv">
      <span>BSI ponds</span><span>${p["bsi:pond_count"] ?? "?"} (${p["bsi:retention_ponds_area_ha"] ?? "?"} ha)</span>
      <span>Threshold</span><span>${p["bsi:threshold"] ?? "?"}</span>
      <span>Footprint mean</span><span>${p["bsi:footprint_mean"] ?? "?"}</span>
      <span>Valid px</span><span>${p["bsi:valid_pixel_fraction"] ?? "?"}</span>
      <span>Source</span><span>${esc(p["operation:source"] ?? "")}</span>
      <span>Composite</span><span>${esc((p.start_datetime || "").slice(0, 10))} to ${esc((p.end_datetime || "").slice(5, 10))}</span>
    </div>
    <div class="links" style="margin-top:6px">
      ${lat != null ? `<a target="_blank" href="https://www.google.com/maps/@${lat},${lon},300m/data=!3m1!1e3">Google Maps</a>` : ""}
      ${mapUrl ? `<a target="_blank" href="${esc(mapUrl)}">STAC map</a>` : ""}
      ${p.stac_item_href ? `<a target="_blank" href="${esc(p.stac_item_href)}">item.json</a>` : ""}
    </div></div>`;

  // polygons
  const bsi = d.polys.filter((x) => x.origin === "bsi"), missed = d.polys.filter((x) => x.origin === "missed");
  const doneN = d.polys.filter((x) => x.qc_class).length;
  h += `<div class="section-title"><span>Candidate polygons</span><span>${doneN}/${d.polys.length} labelled</span></div>`;
  if (!d.polys.length) {
    h += `<div class="muted small">${d.op.bsi_detected ? (d.op.ponds_loaded ? "BSI reported a pond but returned no polygons." : "Pond polygons not loaded (network?).") : "BSI found no pond here. If you can see one it missed, use <b>+ Mark missed pond</b> on the map."}</div>`;
  }
  [...bsi, ...missed].forEach((x) => {
    const sel = x.pid === S.sel;
    const assocSug = x.qc_facility_association ? null : assocGuess(x);
    h += `<div class="card ${sel ? "sel" : ""} ${x.origin === "missed" ? "missed" : ""}" data-pid="${esc(x.pid)}">
      <div class="top" data-act="sel"><span class="pid">${x.origin === "missed" ? "missed m" : "p"}${x.part} · ${x.area_ha ?? "?"} ha</span>
        <span class="small muted">${x.qc_status === "needs_followup" ? `<span class="tag warn">follow-up</span>` : x.qc_class ? "✓ " + esc(x.qc_reviewer || "") : "not reviewed"}</span></div>`;
    if (sel) {
      h += `<div><div class="flabel">Class <span class="muted">(keys 1 to 8)</span></div>${chips(V.polygon.qc_class, x.qc_class, { field: "qc_class", keyed: true, restrict: x.origin === "missed" ? V.missed_class : null })}</div>
        <div><div class="flabel">Issue</div>${chips(V.polygon.qc_issue, x.qc_issue, { field: "qc_issue", suggested: x.qc_issue ? null : "none" })}</div>
        <div style="display:grid;grid-template-columns:auto 1fr;gap:10px">
          <div><div class="flabel">Confidence</div>${chips(V.polygon.qc_confidence, x.qc_confidence, { field: "qc_confidence", propose: false })}</div>
          <div><div class="flabel">Facility association</div>${chips(V.polygon.qc_facility_association, x.qc_facility_association, { field: "qc_facility_association", propose: false, suggested: assocSug })}</div>
        </div>
        <textarea data-notes="${esc(x.pid)}" placeholder="Notes (short visual justification)">${esc(S.pendingNotes[x.pid] ?? S.notesSending[x.pid] ?? x.qc_notes ?? "")}</textarea>
        <div class="status"><label><input type="checkbox" data-follow="${esc(x.pid)}" ${x.qc_status === "needs_followup" ? "checked" : ""}> Needs follow-up <kbd>f</kbd></label>
          <span style="flex:1"></span>
          ${x.origin === "missed" ? `<button data-act="delmissed" class="small">Delete</button>` : ""}
          <button data-act="nextpoly" class="small">Next polygon <kbd>n</kbd></button></div>`;
    } else if (x.qc_class) {
      h += `<div class="small">${esc(vlabel(V.polygon.qc_class, x.qc_class))}${x.qc_issue && x.qc_issue !== "none" ? " · " + esc(vlabel(V.polygon.qc_issue, x.qc_issue)) : ""}${x.qc_confidence ? " · " + esc(x.qc_confidence) : ""}</div>`;
    }
    h += `</div>`;
  });

  // operation verdict
  const sg = d.suggestion || {};
  const locked = q.locked && !S.unlock;
  const Vo = V.operation;
  const draft = S.draft || (S.draft = { qc_result: q.qc_result, qc_feature: q.qc_feature, qc_issue: q.qc_issue, qc_site_valid: q.qc_site_valid, qc_notes: q.qc_notes });
  h += `<div class="section-title"><span>Operation verdict (Climate TRACE)</span><span>${q.qc_status === "reviewed" ? "✓ saved" : q.qc_status === "needs_followup" ? "follow-up" : "not saved"}</span></div>
    <div class="card verdict">`;
  if (locked) {
    h += `<div class="locked">Imported from completed QC (${esc(q.qc_reviewer || "")}). Polygon labels you add here do not change it.<br>
      <b>${esc(vlabel(Vo.qc_result, q.qc_result))}</b> · ${esc(vlabel(Vo.qc_feature, q.qc_feature))} · issue ${esc(vlabel(Vo.qc_issue, q.qc_issue))}
      · site valid: ${q.qc_site_valid ? esc(q.qc_site_valid) : `<span class="muted">not assessed</span>`}${q.qc_notes ? `<br><span class="muted">${esc(q.qc_notes)}</span>` : ""}</div>
      ${sg.qc_result && (sg.qc_result !== q.qc_result || sg.qc_feature !== q.qc_feature) && sg.complete ? `<div class="suggest">Polygon labels now suggest <b>${esc(sg.qc_result)}</b> / ${esc(sg.qc_feature)}. Unlock if the original verdict should change.</div>` : ""}
      <div><button data-act="unlock" class="small">Unlock to edit</button></div>`;
  } else {
    h += sg.why ? `<div class="suggest">Suggested from polygons: <b>${esc(sg.qc_result || "?")}</b> / ${esc(sg.qc_feature || "?")} / issue ${esc(sg.qc_issue || "none")}<br><span class="muted">${esc(sg.why)}</span>
      <div style="margin-top:5px"><button data-act="accept" class="small">Use suggestion</button></div></div>` : "";
    h += `<div><div class="flabel">Result</div>${chips(Vo.qc_result, draft.qc_result, { field: "op:qc_result", suggested: sg.qc_result })}</div>
      <div><div class="flabel">Feature</div>${chips(Vo.qc_feature, draft.qc_feature, { field: "op:qc_feature", suggested: sg.qc_feature })}</div>
      <div><div class="flabel">Issue</div>${chips(Vo.qc_issue, draft.qc_issue, { field: "op:qc_issue", suggested: sg.qc_issue })}</div>
      <div><div class="flabel">Is this a cattle operation? <span class="muted">(required)</span></div>${chips(Vo.qc_site_valid, draft.qc_site_valid, { field: "op:qc_site_valid", propose: false })}</div>
      <textarea id="opNotes" placeholder="Operation notes (incorrect, uncertain or unusual cases)">${esc(draft.qc_notes || "")}</textarea>
      <div class="status"><button data-act="saveop" class="primary">Save verdict</button><button data-act="saveopnext">Save &amp; next</button>
        <button data-act="saveopfollow" class="small">Save as follow-up</button></div>`;
  }
  h += `</div>`;
  if (d.history && d.history.length) {
    h += `<div class="hist">Recent: ${d.history.slice(0, 5).map((x) => `${esc(x.reviewer)} ${esc(x.ts.slice(5, 16).replace("T", " "))} (${x.tbl === "op_qc" ? "verdict" : esc(x.rec.split("_").pop())})`).join(" · ")}</div>`;
  }
  const el = $("#detail");
  const st = $("#detailPane").scrollTop;
  el.innerHTML = h;
  $("#detailPane").scrollTop = st;
  const sc = $(".card.sel", el); if (sc) { const r = sc.getBoundingClientRect(), pr = $("#detailPane").getBoundingClientRect(); if (r.top < pr.top || r.bottom > pr.bottom) sc.scrollIntoView({ block: "nearest" }); }
  bindDetail(el);
}

function assocGuess(p) {
  // centroid of polygon bbox inside the operation footprint?
  try {
    const b = L.geoJSON(p.geom).getBounds().getCenter();
    const f = S.cur.op.geom;
    const rings = f.type === "Polygon" ? [f.coordinates[0]] : f.coordinates.map((c) => c[0]);
    const inside = rings.some((r) => pip([b.lng, b.lat], r));
    return inside ? "inside_operation" : "adjacent";
  } catch (e) { return null; }
}
function pip(pt, ring) {
  let c = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i], [xj, yj] = ring[j];
    if ((yi > pt[1]) !== (yj > pt[1]) && pt[0] < ((xj - xi) * (pt[1] - yi)) / (yj - yi) + xi) c = !c;
  }
  return c;
}

function bindDetail(el) {
  $$("[data-act]", el).forEach((b) => b.addEventListener("click", (e) => {
    const a = b.dataset.act, card = b.closest(".card");
    if (a === "prev") stepOp(-1);
    else if (a === "next") stepOp(1);
    else if (a === "sel") selectPoly(card.dataset.pid, false, true);
    else if (a === "nextpoly") stepPoly(1);
    else if (a === "delmissed") delMissed(card.dataset.pid);
    else if (a === "unlock") { S.unlock = true; renderDetail(); }
    else if (a === "accept") acceptSuggestion();
    else if (a === "saveop") saveOp("reviewed");
    else if (a === "saveopnext") saveOp("reviewed", true);
    else if (a === "saveopfollow") saveOp("needs_followup");
    e.stopPropagation();
  }));
  $$(".chip", el).forEach((b) => b.addEventListener("click", () => {
    const f = b.dataset.f;
    if (b.dataset.propose) return propose(f, b.closest(".card"));
    const v = b.dataset.v;
    if (f.startsWith("op:")) { S.draft[f.slice(3)] = S.draft[f.slice(3)] === v ? null : v; renderDetail(); }
    else setPoly(b.closest(".card").dataset.pid, f, v);
  }));
  $$("textarea[data-notes]", el).forEach((t) => {
    t.addEventListener("input", () => { S.pendingNotes[t.dataset.notes] = t.value; setSaving("unsaved note"); });
    t.addEventListener("blur", flushNotes);
  });
  const on = $("#opNotes", el);
  if (on) on.addEventListener("input", () => { S.draft.qc_notes = on.value; });
  $$("input[data-follow]", el).forEach((c) => c.addEventListener("change", () => {
    const want = c.checked;
    flushNotes();
    savePoly(c.dataset.follow, (p) => ({ qc_status: want ? "needs_followup" : (p.qc_class ? "reviewed" : "not_reviewed") }));
  }));
}

function selectPoly(pid, fromMap, toggle) {
  flushNotes();
  S.sel = toggle && S.sel === pid ? null : pid;
  drawOp(); renderDetail();
  if (fromMap) { const c = $(`.card[data-pid="${CSS.escape(pid)}"]`); if (c) c.scrollIntoView({ block: "nearest" }); }
}
function stepPoly(dir) {
  const ps = S.cur ? S.cur.polys : [];
  if (!ps.length) return;
  const i = ps.findIndex((p) => p.pid === S.sel);
  const n = ps[i + dir];
  if (n) selectPoly(n.pid);
  else if (dir > 0) { S.sel = null; drawOp(); renderDetail(); const v = $(".verdict"); if (v) v.scrollIntoView({ block: "nearest" }); }
}

function setPoly(pid, field, value) {
  if (!S.polyById[pid]) return;
  flushNotes();  // a note typed on this polygon is queued first, so the chip save follows it
  // built when the save runs, from the newest copy, so toggles and defaults see earlier queued saves
  savePoly(pid, (p) => {
    const data = { [field]: p[field] === value && field !== "qc_class" ? null : value };
    if (field === "qc_class") {
      if (p.qc_status !== "needs_followup") data.qc_status = "reviewed";
      if (!p.qc_facility_association) data.qc_facility_association = assocGuess(p);
      if (!p.qc_issue) data.qc_issue = "none";
    }
    return data;
  });
}
// data: an object, or a function (latest polygon) -> object|null, evaluated when the save actually runs
function savePoly(pid, data) {
  const rec = "poly:" + pid;
  return enqueue(rec, async () => {
    const p = S.polyById[pid];
    if (!p) return false;
    const body = typeof data === "function" ? data(p) : data;
    if (!body) return false;
    setSaving("saving...");
    try {
      const d = await api(`/api/poly/${enc(pid)}/qc`, { method: "POST", body: { data: body, version: p.version } });
      applyOp(d);
      setSaving("saved " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
      return true;
    } catch (e) {
      setSaving("");
      if (e.status === 409) {
        bumpEpoch(rec);
        const c = e.data.current;
        toast(`${c.qc_reviewer || "Someone"} changed this polygon at ${(c.qc_date || "").slice(11, 16)}. Showing their version; re-apply if needed.`, true, 6000);
        try { applyOp(await api(`/api/op/${enc(p.op_key)}`)); } catch (_) { /* keep what we have */ }
      } else toast(e.message, true);
      return false;
    }
  });
}
function flushNotes() {
  const pend = S.pendingNotes; S.pendingNotes = {};
  Object.entries(pend).forEach(([pid, v]) => {
    S.notesSending[pid] = v;
    savePoly(pid, (p) => ((p.qc_notes || "") !== v ? { qc_notes: v } : null))
      .finally(() => { if (S.notesSending[pid] === v) delete S.notesSending[pid]; });
  });
}
async function delMissed(pid) {
  if (!confirm("Delete this reviewer-drawn polygon?")) return;
  try { applyOp(await api(`/api/poly/${enc(pid)}/delete`, { method: "POST", body: {} })); } catch (e) { toast(e.message, true); }
}

function acceptSuggestion() {
  const s = S.cur.suggestion;
  // the site question is never filled from the suggestion: the reviewer must answer it
  ["qc_result", "qc_feature", "qc_issue"].forEach((k) => { if (s[k]) S.draft[k] = s[k]; });
  renderDetail();
}
function saveOp(status, next = false) {
  flushNotes();
  const d = S.draft, key = S.cur.op.op_key;
  if (!d.qc_result || !d.qc_feature) return toast("Pick a result and a feature first (or use the suggestion).", true);
  if (!d.qc_site_valid) return toast("Answer \"Is this a cattle operation?\" before saving.", true);
  const data = { ...d, qc_issue: d.qc_issue || "none", qc_status: status, ...(S.cur.qc.locked ? { _unlock: true } : {}) };
  const rec = "op:" + key;
  return enqueue(rec, async () => {
    setSaving("saving...");
    try {
      const r = await api(`/api/op/${enc(key)}/qc`, { method: "POST", body: { data, version: S.opVer[key] } });
      const isCur = S.openKey === key;
      // pick the next operation before the queue re-filters (a "done" op drops out of the To do view)
      const i = S.view.findIndex((x) => x.op_key === key);
      const nextKey = i >= 0 && S.view[i + 1] ? S.view[i + 1].op_key : null;
      if (isCur) { S.unlock = false; S.draft = null; }
      applyOp(r);
      renderQueue(true);
      setSaving("verdict saved");
      if (next && isCur && S.openKey === key) { if (nextKey) openOp(nextKey); else stepOp(1); }
    } catch (e) {
      setSaving("");
      if (e.status === 409) {
        bumpEpoch(rec);
        const c = e.data.current;
        toast(`${c.qc_reviewer || "Someone"} saved this verdict at ${(c.qc_date || "").slice(11, 16)}. Showing their version.`, true, 6000);
        if (S.openKey === key) S.draft = null;
        try { applyOp(await api(`/api/op/${enc(key)}`)); } catch (_) { /* keep what we have */ }
      } else toast(e.message, true);
    }
  });
}

function propose(field, card) {
  const dlg = $("#dlgPropose");
  $("#proposeInput").value = "";
  $("#proposeFor").textContent = "Field: " + field.replace("op:", "operation ");
  dlg.showModal();
  dlg.onclose = () => {
    if (dlg.returnValue !== "ok") return;
    const v = $("#proposeInput").value.trim().toLowerCase().replace(/[^a-z0-9_ ]+/g, "").replace(/\s+/g, "_");
    if (!v) return;
    const val = PROPOSED + v;
    if (field.startsWith("op:")) { S.draft[field.slice(3)] = val; renderDetail(); toast("Proposal will be recorded when you save the verdict."); }
    else setPoly(card.dataset.pid, field, val);
  };
}

// ------------------------------------------------------------------ progress / proposals / export / help
async function renderProgress() {
  const s = await api("/api/stats");
  const regs = Object.entries(s.regions);
  const tot = (k) => regs.reduce((a, [, v]) => a + (v[k] || 0), 0);
  let h = `<h2>Progress</h2>
    <p class="muted">Operations count as done when the verdict is saved and every candidate polygon is labelled. Imported verdicts show as "polygons pending" until their polygons are labelled.</p>
    <table class="grid"><thead><tr><th>Region</th><th>Ops</th><th>Done</th><th>Polygons pending</th><th>In progress</th><th>Follow-up</th><th>Ops progress</th>
      <th>Polygons labelled</th><th>Polygon progress</th><th>Missed added</th><th>Not a cattle op</th><th>Correct / Incorrect / Uncertain</th></tr></thead><tbody>`;
  regs.forEach(([r, v]) => {
    const pp = v.polys + (v.missed || 0);
    h += `<tr><td>${esc(r)}</td><td>${v.ops}</td><td>${v.done || 0}</td><td>${v.polys_pending || 0}</td><td>${v.in_progress || 0}</td><td>${v.needs_followup || 0}</td>
      <td><div class="prog"><div style="width:${(100 * (v.done || 0)) / v.ops}%"></div></div></td>
      <td>${v.polys_done || 0} / ${pp}</td><td><div class="prog"><div class="b" style="width:${pp ? (100 * (v.polys_done || 0)) / pp : 0}%"></div></div></td>
      <td>${v.missed || 0}</td><td>${v.site_invalid || 0}</td>
      <td>${v.result_correct || 0} / ${v.result_incorrect || 0} / ${v.result_uncertain || 0}</td></tr>`;
  });
  h += `<tr><td><b>All</b></td><td><b>${tot("ops")}</b></td><td><b>${tot("done")}</b></td><td>${tot("polys_pending")}</td><td>${tot("in_progress")}</td><td>${tot("needs_followup")}</td><td></td>
    <td><b>${tot("polys_done")} / ${tot("polys") + tot("missed")}</b></td><td></td><td>${tot("missed")}</td><td>${tot("site_invalid")}</td><td></td></tr></tbody></table>`;
  // class distribution
  const byReg = {};
  s.classes.forEach((c) => { (byReg[c.region] = byReg[c.region] || {})[c.qc_class] = c.n; });
  h += `<h3>Polygon labels by region</h3><div class="legendrow">${Object.entries(CLASS_COLORS).map(([k, c]) => `<span><span class="sw" style="background:${c}"></span>${esc(vlabel(S.vocab.polygon.qc_class, k))}</span>`).join("")}</div>
    <table class="grid"><tbody>`;
  Object.entries(byReg).forEach(([r, m]) => {
    const n = Object.values(m).reduce((a, b) => a + b, 0);
    h += `<tr><td style="width:140px">${esc(r)}</td><td style="width:60px">${n}</td><td><div class="classbar">${Object.entries(m).map(([k, v]) => `<div title="${esc(k)}: ${v}" style="width:${(100 * v) / n}%;background:${CLASS_COLORS[k] || "#999"}"></div>`).join("")}</div></td></tr>`;
  });
  if (!Object.keys(byReg).length) h += `<tr><td class="muted">No polygon labels yet.</td></tr>`;
  h += `</tbody></table><h3>Reviewers</h3><table class="grid"><thead><tr><th>Reviewer</th><th>Saves (all time)</th><th>Records touched today</th></tr></thead><tbody>
    ${Object.entries(s.reviewers).map(([r, n]) => `<tr><td>${esc(r)}</td><td>${n}</td><td>${s.today[r] || 0}</td></tr>`).join("") || `<tr><td class="muted">No saves yet.</td><td></td><td></td></tr>`}</tbody></table>`;
  $("#progress").innerHTML = h;
}

async function renderProposals(s) {
  s = s || await api("/api/stats");
  const open = s.proposals.filter((p) => p.status === "open");
  updatePropCount(open.length);
  const groups = {};
  open.forEach((p) => { const k = `${p.scope}|${p.field}|${p.label}`; (groups[k] = groups[k] || []).push(p); });
  let h = `<h2>Proposed labels</h2><p class="muted">Labels reviewers entered with "other...". Promote one to add it to the vocabulary (every record using it keeps it), map it to an existing value, or reject it (clears it on those records).</p>`;
  if (!Object.keys(groups).length) h += `<p>No open proposals.</p>`;
  Object.entries(groups).forEach(([k, ps]) => {
    const [scope, field, label] = k.split("|");
    const list = (scope === "polygon" ? S.vocab.polygon : S.vocab.operation)[field] || [];
    h += `<div class="card" style="margin-bottom:10px" data-id="${ps[0].id}">
      <div><b>${esc(label)}</b> <span class="muted">· ${esc(scope)} ${esc(field)} · used on ${ps.length} record(s) · by ${esc([...new Set(ps.map((p) => p.reviewer))].join(", "))}</span></div>
      <div class="small muted">${ps.slice(0, 6).map((p) => esc(p.rec)).join(", ")}${ps.length > 6 ? " ..." : ""}</div>
      <div class="status"><button data-pa="promote" class="primary small">Promote as new value</button>
        <span>or map to</span><select data-map>${list.map((x) => `<option value="${esc(x.value)}">${esc(x.label)}</option>`).join("")}</select><button data-pa="map" class="small">Map</button>
        <button data-pa="reject" class="small">Reject</button></div></div>`;
  });
  const done = s.proposals.filter((p) => p.status !== "open");
  if (done.length) h += `<h3>Resolved</h3><ul class="plain small">${done.slice(0, 40).map((p) => `<li>${esc(p.label)} → ${esc(p.status)} ${esc(p.resolved_value || "")} (${esc(p.resolved_by || "")})</li>`).join("")}</ul>`;
  $("#proposals").innerHTML = h;
  $$("#proposals [data-pa]").forEach((b) => b.addEventListener("click", async () => {
    const card = b.closest(".card"), act = b.dataset.pa;
    const body = { action: act };
    if (act === "map") body.value = $("[data-map]", card).value;
    if (act === "reject" && !confirm("Reject and clear this label on all records using it?")) return;
    try {
      const st = await api(`/api/proposals/${card.dataset.id}`, { method: "POST", body });
      S.vocab = await api("/api/vocab");
      renderProposals(st);
      toast("Updated.");
    } catch (e) { toast(e.message, true); }
  }));
}
function updatePropCount(n) { const el = $("#propCount"); el.textContent = n; el.classList.toggle("hidden", !n); }

function renderHelp() {
  const V = S.vocab;
  const tbl = (list) => `<table class="grid"><tbody>${list.map((x) => `<tr><td style="width:190px"><code>${esc(x.value)}</code>${x.key ? ` <kbd>${x.key}</kbd>` : ""}</td><td style="text-align:left">${esc(x.help || x.label)}</td></tr>`).join("")}</tbody></table>`;
  $("#help").innerHTML = `<h2>How a review works</h2>
    <ol><li>Pick an operation. The map zooms to the footprint (yellow dashed) and shows the TCI. Detected polygons are outlined; the selected one is pink.</li>
      <li>Label each polygon: class first (keys 1 to 8), then issue, confidence and facility association. Association is pre-filled from whether the polygon sits inside the footprint.</li>
      <li>Toggle BSI-E, the BSI mask (white = BSI-E below this operation's threshold, the pond signal; polygons are blobs of 5+ pixels) and the indices for wet runoff, mixed or uncertain cases. Google Maps opens high-res imagery at the site.</li>
      <li>If BSI missed a pond, click <b>+ Mark missed pond</b>, draw it, and label it. This records false negatives for the research library.</li>
      <li>The operation verdict is suggested from your polygon labels. Check it, answer whether the site is really a cattle operation (required, never pre-filled), and save.</li></ol>
    <h3>Polygon class</h3>${tbl(V.polygon.qc_class)}
    <h3>Issue (geometry / detection)</h3>${tbl(V.polygon.qc_issue)}
    <p class="muted">Keep class and issue separate: a real manure pond clipped by the footprint is <code>manure_pond</code> + <code>footprint_clipping</code>. "Too small to detect" is <code>missed_small_feature</code>.</p>
    <h3>Operation verdict suggestion</h3>
    <ul class="plain"><li>Any manure pond → correct / manure_pond</li><li>Best evidence possible manure or manure-related → correct if medium/high confidence, otherwise uncertain</li>
      <li>All polygons other water, wet runoff or false positive → incorrect / largest-area class</li><li>Only uncertain or mixed → uncertain / unclear</li>
      <li>BSI no-pond with a missed manure feature marked → incorrect; with none → correct / no_visible_candidate</li></ul>
    <p class="muted">"Is this a cattle operation?" = no when the footprint is not a farm (solar panels, river, town). Those cases are reported separately so they don't count against BSI accuracy.</p>
    <h3>Keys</h3><p><kbd>1</kbd>-<kbd>8</kbd> class · <kbd>n</kbd>/<kbd>p</kbd> next/previous polygon · <kbd>]</kbd>/<kbd>[</kbd> next/previous operation · <kbd>t</kbd> TCI · <kbd>b</kbd> BSI-E · <kbd>k</kbd> BSI mask · <kbd>0</kbd> basemap only · <kbd>o</kbd> outlines · <kbd>f</kbd> follow-up · <kbd>m</kbd> mark missed · <kbd>z</kbd> zoom</p>
    <p class="credit muted small">Developed by the Remote Sensing Lab (RSL), Saint Louis University, for Climate TRACE.</p>`;
}

async function renderExportRegions() {
  const box = $("#expRegions");
  try {
    const r = await api("/api/regions");
    const prev = S.expSel || new Set(r.edited);
    box.innerHTML = r.all.map((g) => `<button type="button" class="chip ${prev.has(g) ? "on" : ""}" data-r="${esc(g)}">${esc(g)}${r.edited.includes(g) ? " ✎" : ""}</button>`).join("");
    S.expSel = prev;
    $$("#expRegions .chip").forEach((b) => b.addEventListener("click", () => {
      const g = b.dataset.r;
      if (S.expSel.has(g)) S.expSel.delete(g); else S.expSel.add(g);
      b.classList.toggle("on", S.expSel.has(g));
    }));
  } catch (e) { box.textContent = e.message; }
}
async function doExport() {
  const regions = [...(S.expSel || [])];
  if (!regions.length) return toast("Pick at least one region to export.", true);
  $("#btnExport").disabled = true;
  $("#expResult").innerHTML = `<p class="muted">Exporting ${regions.length} region(s)...</p>`;
  try {
    const r = await api("/api/export", { method: "POST", body: { initials: $("#expInitials").value, strict: $("#expStrict").checked, regions } });
    lsSet("bsiqc.initials", $("#expInitials").value);
    $("#expResult").innerHTML = `<p>Written to <code>${esc(r.folder)}</code></p><ul class="plain small">${r.files.map((f) => `<li><code>${esc(f.path)}</code> ${esc(f.note)}</li>`).join("")}</ul>`
      + (r.warnings && r.warnings.length ? `<div class="suggest" style="background:var(--warn-soft)"><b style="color:var(--warn)">Check before sending:</b><ul class="plain small">${r.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : "");
  } catch (e) { $("#expResult").innerHTML = `<p style="color:var(--bad)">${esc(e.message)}</p>`; }
  $("#btnExport").disabled = false;
}

// ------------------------------------------------------------------ wiring
function setTab(t) {
  $$("header nav button").forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
  $$(".tab").forEach((m) => m.classList.toggle("on", m.id === "tab-" + t));
  if (t === "review") setTimeout(() => map.invalidateSize(), 50);
  if (t === "progress") renderProgress();
  if (t === "proposals") renderProposals();
  if (t === "help") renderHelp();
  if (t === "export") renderExportRegions();
}
function keys(e) {
  if (e.target.matches("input, textarea, select") || document.querySelector("dialog[open]")) return;
  if (!$("#tab-review").classList.contains("on")) return;
  const k = e.key;
  const p = S.cur && S.cur.polys.find((x) => x.pid === S.sel);
  if (/^[1-8]$/.test(k) && p) {
    const c = S.vocab.polygon.qc_class.find((x) => x.key === k);
    if (c && (p.origin !== "missed" || S.vocab.missed_class.includes(c.value))) setPoly(p.pid, "qc_class", c.value);
  } else if (k === "n") stepPoly(1);
  else if (k === "p") stepPoly(-1);
  else if (k === "]") stepOp(1);
  else if (k === "[") stepOp(-1);
  else if (k === "t") { S.layer = "tci"; showRaster(); }
  else if (k === "b") { S.layer = "bsi-e"; showRaster(); }
  else if (k === "k") { S.layer = "bsi-mask"; showRaster(); }
  else if (k === "0") { S.layer = ""; showRaster(); }
  else if (k === "o") { $("#showPolys").checked = !$("#showPolys").checked; drawOp(); }
  else if (k === "z") zoomOp();
  else if (k === "m") startMissed();
  else if (k === "f" && p) savePoly(p.pid, (x) => ({ qc_status: x.qc_status === "needs_followup" ? (x.qc_class ? "reviewed" : "not_reviewed") : "needs_followup" }));
  else return;
  e.preventDefault();
}
function startMissed() {
  if (!S.cur) return toast("Open an operation first.");
  if (drawer) drawer.disable();
  drawer = new L.Draw.Polygon(map, { shapeOptions: { color: "#fb923c", weight: 2 }, showArea: true, metric: true });
  drawer.enable();
  toast("Click to draw the missed pond; click the first point to finish. Esc cancels.", false, 4000);
}

async function boot() {
  S.vocab = await api("/api/vocab");
  S.reviewer = lsGet("bsiqc.reviewer", "");
  $("#whoName").textContent = S.reviewer || "?";
  $("#expInitials").value = lsGet("bsiqc.initials", "RSL");
  initMap();
  $$("header nav button").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
  $("#who").addEventListener("click", askReviewer);
  ["fRegion", "fState", "fClass"].forEach((id) => $("#" + id).addEventListener("change", () => { if (id === "fRegion") lsSet("bsiqc.region", $("#fRegion").value); renderQueue(false); }));
  $("#fSearch").addEventListener("input", () => renderQueue(false));
  $$("#rasterSeg button").forEach((b) => b.addEventListener("click", () => { S.layer = b.dataset.l; showRaster(); }));
  $("#opacity").addEventListener("input", () => rasterLayer && rasterLayer.setOpacity($("#opacity").value / 100));
  $("#stretch").addEventListener("change", showRaster);
  $("#showPolys").addEventListener("change", drawOp);
  $("#basemap").addEventListener("change", () => setBasemap($("#basemap").value));
  $("#btnMissed").addEventListener("click", startMissed);
  $("#btnZoom").addEventListener("click", zoomOp);
  $("#btnExport").addEventListener("click", doExport);
  $("#expAll").addEventListener("click", (e) => { e.preventDefault(); S.expSel = new Set($$("#expRegions .chip").map((b) => b.dataset.r)); renderExportRegions(); });
  $("#expNone").addEventListener("click", (e) => { e.preventDefault(); S.expSel = new Set(); renderExportRegions(); });
  document.addEventListener("keydown", keys);
  await loadQueue(false);
  api("/api/stats").then((s) => updatePropCount(s.proposals.filter((p) => p.status === "open").length)).catch(() => {});
  if (!S.reviewer) askReviewer();
  // keep the queue fresh so a labmate's progress shows up
  setInterval(() => { if ($("#tab-review").classList.contains("on")) loadQueue(true).catch(() => {}); }, 30000);
}
boot().catch((e) => toast("Could not start: " + e.message, true, 10000));
})();
