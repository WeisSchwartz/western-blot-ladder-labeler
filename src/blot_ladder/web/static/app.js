"use strict";

// ─────────────────────────── state ───────────────────────────
const S = {
  ladders: [], ladderId: null,
  files: { blot: null, marker: null },
  session: null,           // {id, name, width, height, has_marker, auto_font_size}
  images: {},              // view → HTMLImageElement
  view: "blot", tab: "detect",
  zoom: 1, fit: true,
  lanes: [],               // {id, x, width, bands:[{id,y,kda,show}], predicted, extraPeaks, warnings}
  active: null,            // active lane id
  yRange: null,            // [top, bottom] detection limits (original px), null = auto
  autoLimits: false,
  userLimits: false,       // true once the user has dragged a limit line
  crop: null,              // {x, y, w, h}
  sides: { left: null, right: null }, sidesTouched: false,
  selected: null, drag: null, addingLane: false, nextId: 1,
  warnings: [], notes: [], error: null,
  busy: false,
  // v3
  size0: null,             // {w, h} of the original (unrotated) image
  geom: { angle: 0 },
  imgVersion: 0,
  merge: { enabled: false, mode: "auto", opacity: 1, color: "marker", lanes_only: true },
  mergedUrl: null, mergedDirty: true,
  laneLabels: [],          // {id, x, name, numbered}
  llSelected: null, lanesFound: false,
};

const $ = (id) => document.getElementById(id);
const canvas = $("canvas"), ctx = canvas.getContext("2d");
const ladder = () => S.ladders.find((l) => l.id === S.ladderId);
const fmt = (k) => (k == null ? "—" : String(+k));
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const lane = (id = S.active) => S.lanes.find((l) => l.id === id) || null;
const laneName = (ln) => `Ladder ${S.lanes.indexOf(ln) + 1}`;
const limits = () => S.yRange || [0, S.session.height - 1];
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

// ─────────────────────────── api ───────────────────────────
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).error || msg; } catch (_) { /* not JSON */ }
    throw new Error(msg);
  }
  return res;
}
const postJSON = (path, body) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});
const sessionUrl = (p) => `/api/session/${S.session.id}/${p}`;

function setBusy(on, text = "") {
  S.busy = on;
  $("status").textContent = text;
  refreshControls();
}

function showMessages() {
  const box = $("messages");
  box.innerHTML = "";
  const add = (text, cls) => {
    const d = document.createElement("div");
    d.className = "msg " + cls;
    d.textContent = text;
    box.appendChild(d);
  };
  S.notes.forEach((t) => add(t, "info"));
  S.warnings.forEach((t) => add(t, ""));
  for (const ln of S.lanes) {
    for (const w of ln.warnings || []) add(S.lanes.length > 1 ? `${laneName(ln)}: ${w}` : w, "");
  }
  if (S.error) add(S.error, "error");
}

function fail(err) {
  S.error = err.message || String(err);
  setBusy(false);
  afterChange(false);
}

// ─────────────────────────── init ───────────────────────────
async function init() {
  const data = await (await api("/api/ladders")).json();
  S.ladders = data.ladders;
  S.ladderId = data.default;
  const sel = $("ladder");
  for (const l of S.ladders) {
    const o = document.createElement("option");
    o.value = l.id; o.textContent = l.name;
    sel.appendChild(o);
  }
  sel.value = S.ladderId;
  sel.onchange = () => { S.ladderId = sel.value; if (S.session) detect(); };
  $("polarity").onchange = () => { if (S.session) detect(); };
  $("n-ladders").onchange = () => { if (S.session) detect(); };

  setupDrop("blot"); setupDrop("marker");
  $("clear-marker").onclick = (e) => {
    e.preventDefault();
    S.files.marker = null;
    $("file-marker").value = "";
    updateDropLabels();
    if (S.files.blot) upload();
  };
  $("btn-detect").onclick = () => detect();
  $("btn-reset-limits").onclick = () => { S.yRange = null; S.userLimits = false; detect(); };
  $("btn-shift-up").onclick = () => shiftLabels(-1);
  $("btn-shift-down").onclick = () => shiftLabels(+1);
  $("btn-assign").onclick = () => autoAssign();
  $("btn-delete-lane").onclick = () => deleteLane(S.active);
  $("btn-add-lane").onclick = () => { S.addingLane = !S.addingLane; setTab("detect"); refreshControls(); };
  $("btn-download").onclick = () => download();
  $("btn-reset-crop").onclick = () => { resetCrop(); afterChange(); };
  for (const k of ["x", "y", "w", "h"]) {
    $("crop-" + k).addEventListener("change", () => {
      if (!S.session) return;
      const c = { ...S.crop, [k]: +$("crop-" + k).value || 0 };
      S.crop = clampCrop(c);
      afterChange();
    });
  }
  for (const side of ["left", "right"]) {
    $("side-" + side).onchange = () => {
      const v = $("side-" + side).value;
      S.sides[side] = v === "" ? null : +v;
      S.sidesTouched = true;
      afterChange();
    };
  }

  setupGeometryControls();
  setupMergeControls();
  setupLaneLabelControls();
  for (const b of $("tabs").querySelectorAll("button")) b.onclick = () => setTab(b.dataset.tab);
  for (const b of $("views").querySelectorAll("button")) b.onclick = () => setView(b.dataset.view);
  for (const b of $("zoom").querySelectorAll("button")) b.onclick = () => zoom(b.dataset.zoom);
  for (const id of ["opt-font", "opt-header", "opt-ticks"]) {
    $(id).addEventListener("change", () => { if (S.tab === "preview") refreshPreview(); });
  }
  window.addEventListener("resize", () => { if (S.fit) layout(); });
  document.addEventListener("keydown", onKey);
  setupCanvas();
  refreshControls();
}

function setupDrop(which) {
  const input = $("file-" + which), zone = $("drop-" + which);
  const take = (f) => { S.files[which] = f; updateDropLabels(); if (S.files.blot) upload(); };
  input.onchange = () => { if (input.files[0]) take(input.files[0]); };
  zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("over"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("over"));
  zone.addEventListener("drop", (e) => {
    e.preventDefault(); zone.classList.remove("over");
    if (e.dataTransfer.files[0]) take(e.dataTransfer.files[0]);
  });
}

function updateDropLabels() {
  for (const w of ["blot", "marker"]) {
    const f = S.files[w];
    $("drop-" + w).classList.toggle("has-file", !!f);
    $("name-" + w).textContent = f ? f.name
      : (w === "blot" ? "Drop or click to choose" : "Separate colorimetric / white-light image");
  }
  $("clear-marker").hidden = !S.files.marker;
}

// ─────────────────────────── upload & detection ───────────────────────────
async function upload() {
  S.error = null; S.warnings = []; S.notes = [];
  const fd = new FormData();
  fd.append("blot", S.files.blot);
  if (S.files.marker) fd.append("marker", S.files.marker);
  setBusy(true, "Loading image…");
  try {
    S.session = await (await api("/api/session", { method: "POST", body: fd })).json();
    $("opt-font").placeholder = `auto (${S.session.auto_font_size})`;
    S.size0 = { w: S.session.width, h: S.session.height };
    S.geom = { angle: 0 };
    S.laneLabels = []; S.llSelected = null; S.lanesFound = false;
    S.merge.enabled = S.session.has_marker;  // the usual reason for adding a marker image
    S.mergedDirty = true;
    syncGeometryInputs(); syncMergeInputs();
    await loadImages();
    S.view = S.session.has_marker ? "marker" : "blot";
    S.fit = true;
    S.lanes = []; S.active = null; S.yRange = null; S.userLimits = false;
    S.sides = { left: null, right: null }; S.sidesTouched = false;
    resetCrop();
    $("preview").removeAttribute("src");
    $("empty").hidden = true;
    await detect();
  } catch (err) { fail(err); }
}

async function loadImages() {
  S.imgVersion++;
  const views = ["blot"].concat(S.session.has_marker ? ["marker"] : []);
  const loaded = {};
  await Promise.all(views.map((v) => new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => { loaded[v] = img; resolve(); };
    img.onerror = () => reject(new Error("Could not display the " + v + " image."));
    img.src = sessionUrl(`image/${v}?v=${S.imgVersion}`);
  })));
  S.images = loaded;
  S.mergedDirty = true;
  if (S.view === "merged") await refreshMerged();
}

function makeLane(r, keepId = null) {
  return {
    id: keepId ?? S.nextId++, x: r.lane_x, width: r.lane_width,
    bands: r.bands.map((b) => ({ id: S.nextId++, y: b.y, kda: b.kda, show: true })),
    predicted: r.predicted || [], extraPeaks: r.extra_peaks || [], warnings: r.warnings || [],
  };
}

function detectBody(extra = {}) {
  return { ladder: S.ladderId, polarity: $("polarity").value, y_range: S.yRange, ...extra };
}

function applyLimits(res) {
  S.yRange = res.y_range;
  S.autoLimits = res.auto_limits;
  S.notes = (S.session.notes || []).concat(res.notes || []);
}

// Full detection: find all ladder lanes.
async function detect() {
  if (!S.session) return;
  S.error = null;
  setBusy(true, "Detecting ladders…");
  try {
    const n = $("n-ladders").value;
    const res = await (await postJSON(sessionUrl("detect"), detectBody({ n_ladders: n }))).json();
    applyLimits(res);
    S.warnings = res.warnings || [];
    S.lanes = res.lanes.map((r) => makeLane(r));
    S.active = S.lanes.length ? S.lanes[0].id : null;
    S.selected = null;
    if (!S.sidesTouched) autoSides();
    const nb = S.lanes.reduce((a, l) => a + l.bands.length, 0);
    setBusy(false, `${S.lanes.length} ladder lane(s) · ${nb} bands`);
  } catch (err) { fail(err); return; }
  afterChange();
}

// Re-detect bands in existing lanes (after moving/resizing a lane or the limits).
async function redetect(ids) {
  const targets = ids.map((id) => lane(id)).filter(Boolean);
  if (!targets.length) return;
  S.error = null;
  setBusy(true, "Re-detecting…");
  try {
    const res = await (await postJSON(sessionUrl("detect"), detectBody({
      lanes: targets.map((l) => ({ x: l.x, width: l.width })),
    }))).json();
    applyLimits(res);
    res.lanes.forEach((r, i) => {
      const old = targets[i];
      const fresh = makeLane(r, old.id);
      Object.assign(old, fresh);
    });
    S.selected = null;
    setBusy(false, "Re-detected");
  } catch (err) { fail(err); return; }
  sortLanes();
  afterChange();
}

async function addLaneAt(x) {
  S.addingLane = false;
  S.error = null;
  setBusy(true, "Detecting new ladder lane…");
  try {
    const res = await (await postJSON(sessionUrl("detect"), detectBody({ lanes: [{ x }] }))).json();
    applyLimits(res);
    const ln = makeLane(res.lanes[0]);
    S.lanes.push(ln);
    S.active = ln.id;
    sortLanes();
    if (!S.sidesTouched) autoSides();
    setBusy(false, `Added ${laneName(ln)}`);
  } catch (err) { fail(err); return; }
  afterChange();
}

async function autoAssign() {
  const ln = lane();
  if (!ln || ln.bands.length < 2) return;
  S.error = null;
  setBusy(true, "Assigning…");
  try {
    const r = await (await postJSON(sessionUrl("assign"), detectBody({
      lane_x: ln.x, lane_width: ln.width, bands: ln.bands.map((b) => b.y),
    }))).json();
    Object.assign(ln, makeLane(r, ln.id), { x: ln.x, width: ln.width });
    S.selected = null;
    setBusy(false, "Re-assigned");
  } catch (err) { fail(err); return; }
  afterChange();
}

// ─────────────────────────── lanes, sides, crop ───────────────────────────
function sortLanes() { S.lanes.sort((a, b) => a.x - b.x); S.mergedDirty = true; }

function autoSides() {
  const W = S.session.width;
  if (!S.lanes.length) { S.sides = { left: null, right: null }; return; }
  const sorted = [...S.lanes].sort((a, b) => a.x - b.x);
  if (sorted.length === 1) {
    const only = sorted[0];
    S.sides = only.x > W / 2 ? { left: null, right: only.id } : { left: only.id, right: null };
  } else {
    S.sides = { left: sorted[0].id, right: sorted[sorted.length - 1].id };
  }
}

function deleteLane(id) {
  const ln = lane(id);
  if (!ln) return;
  S.lanes = S.lanes.filter((l) => l.id !== id);
  S.active = S.lanes.length ? S.lanes[0].id : null;
  for (const side of ["left", "right"]) if (S.sides[side] === id) S.sides[side] = null;
  if (!S.sidesTouched) autoSides();
  S.selected = null;
  afterChange();
}

function resetCrop() {
  S.crop = { x: 0, y: 0, w: S.session.width, h: S.session.height };
}

function clampCrop(c) {
  const W = S.session.width, H = S.session.height;
  const x = clamp(Math.round(c.x), 0, W - 1), y = clamp(Math.round(c.y), 0, H - 1);
  return { x, y, w: clamp(Math.round(c.w), 1, W - x), h: clamp(Math.round(c.h), 1, H - y) };
}

const cropIsFull = () => S.crop.x === 0 && S.crop.y === 0 &&
  S.crop.w === S.session.width && S.crop.h === S.session.height;

// ─────────────────────────── band editing ───────────────────────────
function shiftLabels(dir) {
  const ln = lane();
  if (!ln) return;
  const ks = ladder().bands_kda;
  const idx = ln.bands.filter((b) => b.kda != null).map((b) => ks.indexOf(b.kda));
  if (!idx.length) return;
  if (Math.min(...idx) + dir < 0 || Math.max(...idx) + dir >= ks.length) {
    S.error = "Can't shift further. The ladder has no band beyond the end.";
    showMessages();
    return;
  }
  S.error = null;
  for (const b of ln.bands) if (b.kda != null) b.kda = ks[ks.indexOf(b.kda) + dir];
  ln.predicted = [];  // the fit no longer matches the labels
  afterChange();
}

function guessKda(ln, y) {
  // Prefer the nearest predicted position of an unused ladder band.
  const used = new Set(ln.bands.map((b) => b.kda));
  let best = null, bestD = Infinity;
  for (const p of ln.predicted) {
    if (p.y == null || used.has(p.kda)) continue;
    const d = Math.abs(p.y - y);
    if (d < bestD) { bestD = d; best = p.kda; }
  }
  return bestD < S.session.height * 0.04 ? best : null;
}

function addBand(ln, y, kda) {
  S.error = null;
  const b = { id: S.nextId++, y, kda: kda === undefined ? guessKda(ln, y) : kda, show: true };
  ln.bands.push(b);
  S.active = ln.id;
  S.selected = b.id;
  afterChange();
}

function deleteBand(ln, id) {
  S.error = null;
  ln.bands = ln.bands.filter((b) => b.id !== id);
  if (S.selected === id) S.selected = null;
  afterChange();
}

function onKey(e) {
  if (e.key === "Escape" && S.addingLane) { S.addingLane = false; refreshControls(); draw(); return; }
  const ln = lane();
  if (S.selected == null || !ln || e.target.closest("input, select, textarea")) return;
  const b = ln.bands.find((x) => x.id === S.selected);
  if (!b) return;
  if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); deleteBand(ln, b.id); }
  if (e.key === "ArrowUp" || e.key === "ArrowDown") {
    e.preventDefault();
    b.y = clamp(b.y + (e.key === "ArrowUp" ? -1 : 1) * (e.shiftKey ? 5 : 1), 0, S.session.height - 1);
    afterChange();
  }
}

// ─────────────────────────── sidebar rendering ───────────────────────────
function renderLaneChips() {
  const box = $("lane-chips");
  box.innerHTML = "";
  for (const ln of S.lanes) {
    const b = document.createElement("button");
    b.textContent = `${laneName(ln)} · ${ln.bands.filter((x) => x.kda != null).length}`;
    b.title = `x ≈ ${Math.round(ln.x)} px`;
    b.className = ln.id === S.active ? "on" : "";
    b.onclick = () => { S.active = ln.id; S.selected = null; afterChange(false); };
    box.appendChild(b);
  }
  if (S.session) {
    const add = document.createElement("button");
    add.className = "add";
    add.textContent = "+ Add";
    add.title = "Then click on the image where the ladder is";
    add.onclick = () => { S.addingLane = true; setTab("detect"); refreshControls(); };
    box.appendChild(add);
  }
  $("btn-delete-lane").hidden = !lane();
}

function renderTable() {
  const tbody = $("band-table").querySelector("tbody");
  tbody.innerHTML = "";
  const ln = lane();
  if (!ln) { $("band-count").textContent = ""; return; }
  const ks = ladder().bands_kda;
  const counts = {};
  for (const b of ln.bands) if (b.kda != null) counts[b.kda] = (counts[b.kda] || 0) + 1;
  for (const b of [...ln.bands].sort((a, c) => a.y - c.y)) {
    const tr = document.createElement("tr");
    if (b.id === S.selected) tr.className = "sel";
    tr.onclick = (e) => { if (!e.target.closest("select,input,button")) { S.selected = b.id; afterChange(false); } };

    const tdY = document.createElement("td");
    tdY.textContent = Math.round(b.y);

    const tdK = document.createElement("td");
    const sel = document.createElement("select");
    sel.innerHTML = `<option value="">—</option>` + ks.map((k) => `<option value="${k}">${fmt(k)}</option>`).join("");
    sel.value = b.kda == null ? "" : String(b.kda);
    if (b.kda != null && counts[b.kda] > 1) { sel.classList.add("dup"); sel.title = "Duplicate kDa value"; }
    sel.onchange = () => { b.kda = sel.value === "" ? null : +sel.value; afterChange(); };
    tdK.appendChild(sel);

    const tdS = document.createElement("td");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = b.show;
    cb.onchange = () => { b.show = cb.checked; afterChange(); };
    tdS.appendChild(cb);

    const tdD = document.createElement("td");
    const del = document.createElement("button");
    del.className = "del"; del.textContent = "×"; del.title = "Delete band";
    del.onclick = () => deleteBand(ln, b.id);
    tdD.appendChild(del);

    tr.append(tdY, tdK, tdS, tdD);
    tbody.appendChild(tr);
  }
  const labeled = ln.bands.filter((b) => b.kda != null && b.show).length;
  $("band-count").textContent = `${laneName(ln)}: ${labeled} labeled / ${ln.bands.length}`;
}

function renderSideSelects() {
  for (const side of ["left", "right"]) {
    const sel = $("side-" + side);
    sel.innerHTML = `<option value="">None</option>` +
      S.lanes.map((ln) => `<option value="${ln.id}">${laneName(ln)}</option>`).join("");
    sel.value = S.sides[side] == null ? "" : String(S.sides[side]);
  }
}

function renderCropInputs() {
  if (!S.crop) return;
  for (const k of ["x", "y", "w", "h"]) $("crop-" + k).value = S.crop[k];
  $("btn-reset-crop").hidden = !S.session || cropIsFull();
}

function renderLimitsText() {
  if (!S.session) return;
  const [t, b] = limits();
  const whole = t <= 0.5 && b >= S.session.height - 1.5;
  $("limits-text").textContent = whole ? "Detection limits: whole image"
    : `Detection limits: rows ${Math.round(t)}–${Math.round(b)}${S.autoLimits ? " (at membrane edges)" : ""}`;
  $("btn-reset-limits").hidden = whole && !S.autoLimits;
}

// ─────────────────────────── canvas: layout & drawing ───────────────────────────
function layout() {
  if (!S.session) return;
  if (S.tab === "preview") { sizePreview(); return; }
  const vp = $("viewport");
  if (S.fit) {
    S.zoom = Math.min((vp.clientWidth - 32) / S.session.width, (vp.clientHeight - 32) / S.session.height, 4);
  }
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(S.session.width * S.zoom), h = Math.round(S.session.height * S.zoom);
  canvas.style.width = w + "px"; canvas.style.height = h + "px";
  canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
  draw();
}

function zoom(kind) {
  if (!S.session) return;
  if (kind === "fit") S.fit = true;
  else {
    S.fit = false;
    if (kind === "1") S.zoom = 1;
    if (kind === "in") S.zoom = Math.min(S.zoom * 1.25, 8);
    if (kind === "out") S.zoom = Math.max(S.zoom / 1.25, 0.05);
  }
  layout();
}

const ACCENT = "#2563eb";

function draw() {
  if (!S.session || S.tab === "preview") return;
  const dpr = window.devicePixelRatio || 1, z = S.zoom * dpr;
  const img = (S.view === "merged" && S.images.merged) || S.images[S.view] || S.images.blot;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.imageSmoothingEnabled = S.zoom < 1;
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
  ctx.setTransform(z, 0, 0, z, 0, 0);
  const u = 1 / S.zoom;  // one CSS pixel in image units
  ctx.font = `600 ${12 * u}px -apple-system, Segoe UI, sans-serif`;
  ctx.textBaseline = "middle";
  if (S.tab === "crop") drawCrop(u);
  else if (S.tab === "lanes") drawLaneLabels(u);
  else drawDetect(u);
}

function chip(text, x, y, align, fill, ink, u) {
  const w = ctx.measureText(text).width + 8 * u, h = 16 * u;
  const cx = align === "left" ? x : x - w;
  ctx.fillStyle = fill;
  ctx.fillRect(cx, y - h / 2, w, h);
  ctx.fillStyle = ink;
  ctx.textAlign = "left";
  ctx.fillText(text, cx + 4 * u, y);
}

function drawDetect(u) {
  const W = S.session.width, H = S.session.height;
  const [top, bottom] = limits();

  // Outside the detection limits: shaded.
  ctx.fillStyle = "rgba(15,23,42,0.35)";
  ctx.fillRect(0, 0, W, top);
  ctx.fillRect(0, bottom, W, H - bottom);
  ctx.strokeStyle = "#e11d48"; ctx.lineWidth = 1.5 * u;
  ctx.setLineDash([8 * u, 5 * u]);
  for (const y of [top, bottom]) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); }
  ctx.setLineDash([]);
  chip("top limit ↕", W / 2, top + 10 * u, "left", "rgba(225,29,72,0.85)", "#fff", u);
  chip("bottom limit ↕", W / 2, bottom - 10 * u, "left", "rgba(225,29,72,0.85)", "#fff", u);

  for (const ln of S.lanes) {
    const active = ln.id === S.active;
    const x0 = ln.x - ln.width / 2, x1 = ln.x + ln.width / 2;
    ctx.fillStyle = active ? "rgba(37,99,235,0.12)" : "rgba(37,99,235,0.05)";
    ctx.fillRect(x0, top, ln.width, bottom - top);
    ctx.strokeStyle = active ? ACCENT : "rgba(37,99,235,0.55)";
    ctx.lineWidth = (active ? 2 : 1.2) * u;
    ctx.setLineDash(active ? [] : [5 * u, 4 * u]);
    ctx.beginPath(); ctx.moveTo(x0, top); ctx.lineTo(x0, bottom); ctx.moveTo(x1, top); ctx.lineTo(x1, bottom); ctx.stroke();
    ctx.setLineDash([]);
    // Edge grips
    if (active) {
      ctx.fillStyle = ACCENT;
      const gy = (top + bottom) / 2;
      for (const gx of [x0, x1]) ctx.fillRect(gx - 3 * u, gy - 12 * u, 6 * u, 24 * u);
    }
    const labelRight = ln.x > W / 2;  // chips go toward the image centre
    const lx = labelRight ? x0 - 6 * u : x1 + 6 * u;
    const align = labelRight ? "right" : "left";
    chip(laneName(ln), ln.x, top + 12 * u, "left", active ? ACCENT : "rgba(37,99,235,0.6)", "#fff", u);

    if (active) {
      const used = new Set(ln.bands.map((b) => b.kda));
      for (const p of ln.predicted) {
        if (p.y == null || used.has(p.kda)) continue;
        ctx.strokeStyle = "rgba(100,116,139,0.9)"; ctx.lineWidth = 1.5 * u;
        ctx.setLineDash([4 * u, 3 * u]);
        ctx.beginPath(); ctx.moveTo(x0, p.y); ctx.lineTo(x1, p.y); ctx.stroke();
        ctx.setLineDash([]);
        chip(`${fmt(p.kda)}?`, lx, p.y, align, "rgba(100,116,139,0.75)", "#fff", u);
      }
      ctx.strokeStyle = "rgba(100,116,139,0.8)"; ctx.lineWidth = u;
      for (const y of ln.extraPeaks) { ctx.beginPath(); ctx.moveTo(x0 - 5 * u, y); ctx.lineTo(x0, y); ctx.stroke(); }
    }
    const counts = {};
    for (const b of ln.bands) if (b.kda != null) counts[b.kda] = (counts[b.kda] || 0) + 1;
    for (const b of ln.bands) {
      const sel = active && b.id === S.selected;
      const problem = b.kda == null || counts[b.kda] > 1;
      const col = !b.show ? "rgba(148,163,184,0.9)" : problem ? "#dc2626" : "#f97316";
      ctx.globalAlpha = active ? 1 : 0.7;
      ctx.strokeStyle = col; ctx.lineWidth = (sel ? 3 : active ? 2 : 1.5) * u;
      ctx.beginPath(); ctx.moveTo(x0 - 3 * u, b.y); ctx.lineTo(x1 + 3 * u, b.y); ctx.stroke();
      chip(fmt(b.kda), lx, b.y, align, sel ? ACCENT : col, "#fff", u);
      ctx.globalAlpha = 1;
    }
  }
  if (S.addingLane) {
    chip("Click on the ladder lane to add it · Esc to cancel", 8 * u, 16 * u, "left", ACCENT, "#fff", u);
  }
}

function drawCrop(u) {
  const W = S.session.width, H = S.session.height, c = S.crop;
  // Alignment grid: bands should run parallel to the horizontal lines.
  ctx.strokeStyle = "rgba(14,165,233,0.45)"; ctx.lineWidth = u;
  const step = Math.max(W, H) / 16;
  for (let y = step; y < H; y += step) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); }
  for (let x = step; x < W; x += step) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke(); }
  ctx.fillStyle = "rgba(15,23,42,0.55)";
  ctx.fillRect(0, 0, W, c.y);
  ctx.fillRect(0, c.y + c.h, W, H - c.y - c.h);
  ctx.fillRect(0, c.y, c.x, c.h);
  ctx.fillRect(c.x + c.w, c.y, W - c.x - c.w, c.h);
  ctx.strokeStyle = "#fff"; ctx.lineWidth = 2 * u;
  ctx.strokeRect(c.x, c.y, c.w, c.h);
  ctx.strokeStyle = ACCENT; ctx.lineWidth = u;
  ctx.setLineDash([6 * u, 4 * u]);
  ctx.strokeRect(c.x, c.y, c.w, c.h);
  ctx.setLineDash([]);
  // Handles
  ctx.fillStyle = "#fff"; ctx.strokeStyle = ACCENT; ctx.lineWidth = 1.5 * u;
  for (const [hx, hy] of cropHandles()) {
    ctx.fillRect(hx - 5 * u, hy - 5 * u, 10 * u, 10 * u);
    ctx.strokeRect(hx - 5 * u, hy - 5 * u, 10 * u, 10 * u);
  }
  // Where the labels will point: band ticks of the ladders feeding each side.
  for (const side of ["left", "right"]) {
    const ln = lane(S.sides[side]);
    if (!ln) continue;
    const ex = side === "left" ? c.x : c.x + c.w;
    ctx.strokeStyle = "#f97316"; ctx.lineWidth = 2 * u;
    for (const b of ln.bands) {
      if (b.kda == null || !b.show || b.y < c.y || b.y > c.y + c.h) continue;
      ctx.beginPath();
      ctx.moveTo(ex + (side === "left" ? -10 : 10) * u, b.y); ctx.lineTo(ex, b.y); ctx.stroke();
    }
  }
  chip(`${c.w} × ${c.h} px`, c.x + 4 * u, c.y + 12 * u, "left", "rgba(15,23,42,0.75)", "#fff", u);
}

function cropHandles() {
  const c = S.crop, mx = c.x + c.w / 2, my = c.y + c.h / 2;
  return [[c.x, c.y], [mx, c.y], [c.x + c.w, c.y], [c.x + c.w, my],
          [c.x + c.w, c.y + c.h], [mx, c.y + c.h], [c.x, c.y + c.h], [c.x, my]];
}

// ─────────────────────────── canvas: interaction ───────────────────────────
function toImage(e) {
  const r = canvas.getBoundingClientRect();
  return { x: (e.clientX - r.left) / S.zoom, y: (e.clientY - r.top) / S.zoom };
}

function hitDetect(p) {
  const tol = 6 / S.zoom, grip = 4 / S.zoom;
  const [top, bottom] = limits();
  const act = lane();
  const near = (ln, pad) => Math.abs(p.x - ln.x) <= ln.width / 2 + pad;
  // 1. Lane edges (resize) win right at the edge, even where a band crosses it.
  if (p.y >= top && p.y <= bottom) {
    for (const ln of S.lanes) {
      if (Math.abs(p.x - (ln.x - ln.width / 2)) < grip) return { kind: "edge", lane: ln, side: -1 };
      if (Math.abs(p.x - (ln.x + ln.width / 2)) < grip) return { kind: "edge", lane: ln, side: +1 };
    }
  }
  // 2. Bands and ghosts of the active lane.
  if (act && near(act, 12 / S.zoom)) {
    let best = null, bd = Infinity;
    for (const b of act.bands) { const d = Math.abs(b.y - p.y); if (d < tol && d < bd) { bd = d; best = b; } }
    if (best) return { kind: "band", lane: act, band: best };
    const used = new Set(act.bands.map((b) => b.kda));
    for (const g of act.predicted) {
      if (g.y != null && !used.has(g.kda) && Math.abs(g.y - p.y) < tol) return { kind: "ghost", lane: act, ghost: g };
    }
  }
  // 3. Bands of other lanes.
  for (const ln of S.lanes) {
    if (ln === act || !near(ln, 4 / S.zoom)) continue;
    const b = ln.bands.find((x) => Math.abs(x.y - p.y) < tol);
    if (b) return { kind: "band", lane: ln, band: b };
  }
  // 4. Detection limit lines.
  if (Math.abs(p.y - top) < tol) return { kind: "limit", which: 0 };
  if (Math.abs(p.y - bottom) < tol) return { kind: "limit", which: 1 };
  // 5. Lane body.
  for (const ln of S.lanes) if (near(ln, 0) && p.y >= top && p.y <= bottom) return { kind: "lane", lane: ln };
  return null;
}

function hitCrop(p) {
  const tol = 8 / S.zoom, c = S.crop;
  const l = Math.abs(p.x - c.x) < tol, r = Math.abs(p.x - (c.x + c.w)) < tol;
  const t = Math.abs(p.y - c.y) < tol, b = Math.abs(p.y - (c.y + c.h)) < tol;
  const inX = p.x > c.x - tol && p.x < c.x + c.w + tol, inY = p.y > c.y - tol && p.y < c.y + c.h + tol;
  if ((l || r || t || b) && inX && inY) return { kind: "crop-edge", l, r, t, b };
  if (p.x > c.x && p.x < c.x + c.w && p.y > c.y && p.y < c.y + c.h) return { kind: "crop-move" };
  return { kind: "crop-new" };
}

function cursorFor(hit) {
  if (!hit) return S.addingLane ? "crosshair" : "default";
  switch (hit.kind) {
    case "band": return "ns-resize";
    case "ghost": return "copy";
    case "edge": return "col-resize";
    case "limit": return "row-resize";
    case "lane": return "ew-resize";
    case "crop-move": return "move";
    case "crop-new": return "crosshair";
    case "crop-edge":
      if ((hit.l && hit.t) || (hit.r && hit.b)) return "nwse-resize";
      if ((hit.r && hit.t) || (hit.l && hit.b)) return "nesw-resize";
      return hit.l || hit.r ? "ew-resize" : "ns-resize";
    default: return "default";
  }
}

function setupCanvas() {
  canvas.addEventListener("mousedown", (e) => {
    if (!S.session || S.busy) return;
    const p = toImage(e);
    if (S.tab === "crop") {
      const hit = hitCrop(p);
      S.drag = { ...hit, start: p, orig: { ...S.crop } };
      return;
    }
    if (S.tab === "lanes") {
      const ll = hitLaneLabel(p);
      if (e.shiftKey || !ll) {
        if (e.shiftKey) addLaneLabel(clamp(p.x, 0, S.session.width - 1));
        else { S.llSelected = null; afterChange(false); }
        return;
      }
      S.llSelected = ll.id;
      S.drag = { kind: "ll", item: ll, dx: p.x - ll.x };
      afterChange(false);
      return;
    }
    if (S.addingLane) { addLaneAt(clamp(p.x, 0, S.session.width - 1)); return; }
    const hit = hitDetect(p);
    const act = lane();
    if (e.shiftKey && act && Math.abs(p.x - act.x) <= act.width / 2 + 12 / S.zoom) {
      addBand(act, clamp(p.y, 0, S.session.height - 1)); return;
    }
    if (!hit) { S.selected = null; afterChange(false); return; }
    switch (hit.kind) {
      case "ghost": addBand(hit.lane, hit.ghost.y, hit.ghost.kda); return;
      case "band":
        S.active = hit.lane.id; S.selected = hit.band.id;
        S.drag = { kind: "band", band: hit.band, dy: p.y - hit.band.y };
        break;
      case "edge":
        S.active = hit.lane.id;
        S.drag = { kind: "edge", lane: hit.lane, side: hit.side, moved: false };
        break;
      case "limit":
        if (!S.yRange) S.yRange = limits();
        S.drag = { kind: "limit", which: hit.which, moved: false };
        break;
      case "lane":
        if (S.active !== hit.lane.id) S.selected = null;
        S.active = hit.lane.id;
        S.drag = { kind: "lane", lane: hit.lane, dx: p.x - hit.lane.x, moved: false };
        break;
    }
    afterChange(false);
  });

  window.addEventListener("mousemove", (e) => {
    if (!S.session || S.tab === "preview") return;
    const p = toImage(e);
    const d = S.drag;
    if (!d) {
      if (e.target === canvas) {
        if (S.tab === "lanes") { canvas.style.cursor = e.shiftKey ? "copy" : hitLaneLabel(p) ? "ew-resize" : "default"; return; }
        const hit = S.tab === "crop" ? hitCrop(p) : (S.addingLane ? null : hitDetect(p));
        canvas.style.cursor = e.shiftKey && hit?.kind === "lane" ? "copy" : cursorFor(hit);
      }
      return;
    }
    const W = S.session.width, H = S.session.height;
    switch (d.kind) {
      case "ll": d.item.x = clamp(p.x - d.dx, 0, W - 1); break;
      case "band": d.band.y = clamp(p.y - d.dy, 0, H - 1); break;
      case "lane": d.lane.x = clamp(p.x - d.dx, 0, W - 1); d.moved = true; break;
      case "edge": {
        const x0 = d.lane.x - d.lane.width / 2, x1 = d.lane.x + d.lane.width / 2;
        const nx0 = d.side < 0 ? clamp(p.x, 0, x1 - 3) : x0;
        const nx1 = d.side > 0 ? clamp(p.x, x0 + 3, W - 1) : x1;
        d.lane.x = (nx0 + nx1) / 2; d.lane.width = nx1 - nx0; d.moved = true;
        break;
      }
      case "limit": {
        const r = [...S.yRange];
        r[d.which] = d.which === 0 ? clamp(p.y, 0, r[1] - 10) : clamp(p.y, r[0] + 10, H - 1);
        S.yRange = r; S.autoLimits = false; d.moved = true;
        break;
      }
      case "crop-move": {
        const o = d.orig;
        S.crop = { ...o, x: clamp(o.x + p.x - d.start.x, 0, W - o.w), y: clamp(o.y + p.y - d.start.y, 0, H - o.h) };
        break;
      }
      case "crop-edge": {
        const o = d.orig;
        let x0 = o.x, y0 = o.y, x1 = o.x + o.w, y1 = o.y + o.h;
        if (d.l) x0 = clamp(p.x, 0, x1 - 10);
        if (d.r) x1 = clamp(p.x, x0 + 10, W);
        if (d.t) y0 = clamp(p.y, 0, y1 - 10);
        if (d.b) y1 = clamp(p.y, y0 + 10, H);
        S.crop = clampCrop({ x: x0, y: y0, w: x1 - x0, h: y1 - y0 });
        break;
      }
      case "crop-new": {
        const x0 = clamp(Math.min(d.start.x, p.x), 0, W), x1 = clamp(Math.max(d.start.x, p.x), 0, W);
        const y0 = clamp(Math.min(d.start.y, p.y), 0, H), y1 = clamp(Math.max(d.start.y, p.y), 0, H);
        if (x1 - x0 >= 10 && y1 - y0 >= 10) S.crop = clampCrop({ x: x0, y: y0, w: x1 - x0, h: y1 - y0 });
        break;
      }
    }
    if (d.kind.startsWith("crop")) S.crop = clampCrop(S.crop);
    draw();
    if (d.kind.startsWith("crop")) renderCropInputs();
    if (d.kind === "limit") renderLimitsText();
  });

  window.addEventListener("mouseup", () => {
    const d = S.drag;
    if (!d) return;
    S.drag = null;
    if (d.kind === "ll") { sortLaneLabels(); afterChange(); }
    else if ((d.kind === "lane" || d.kind === "edge") && d.moved) { sortLanes(); redetect([d.lane.id]); }
    else if (d.kind === "limit" && d.moved) {
      S.userLimits = true;
      if (S.lanes.length) redetect(S.lanes.map((l) => l.id)); else detect();
    } else afterChange();
  });

  canvas.addEventListener("dblclick", (e) => {
    if (S.tab === "lanes") {
      const ll = hitLaneLabel(toImage(e));
      if (ll) deleteLaneLabel(ll.id);
      return;
    }
    if (S.tab !== "detect") return;
    const hit = hitDetect(toImage(e));
    if (hit?.kind === "band") deleteBand(hit.lane, hit.band.id);
  });
}

// ─────────────────────────── tabs, views, preview ───────────────────────────
function setTab(tab) {
  S.tab = tab;
  S.error = null;
  if (tab !== "detect") S.addingLane = false;
  for (const b of $("tabs").querySelectorAll("button")) b.classList.toggle("on", b.dataset.tab === tab);
  if (tab === "lanes" && S.session && !S.lanesFound && !S.laneLabels.length) findLanes();
  afterChange(true);
}

async function setView(v) {
  if (v === "merged") {
    if (!S.session?.has_marker) return;
    S.view = v;
    refreshControls();
    await refreshMerged();
    draw();
    return;
  }
  if (!S.images[v]) return;
  S.view = v;
  refreshControls();
  draw();
}

function sideBands(side) {
  const ln = lane(S.sides[side]);
  return ln ? ln.bands.map((b) => ({ y: b.y, kda: b.kda, show: b.show })) : null;
}

function renderBody(preview) {
  const fs = $("opt-font").value;
  return {
    left: sideBands("left"), right: sideBands("right"), crop: S.crop,
    font_size: fs ? +fs : null, header: $("opt-header").checked, ticks: $("opt-ticks").checked,
    format: $("opt-format").value, preview,
    merge: S.session.has_marker ? S.merge : null,
    ladder_lanes: S.lanes.map((l) => ({ x: l.x, width: l.width })),
    lane_labels: laneLabelsBody(),
  };
}

let previewUrl = null, previewSeq = 0;
async function refreshPreview() {
  if (!S.session) return;
  const seq = ++previewSeq;
  $("status").textContent = "Rendering preview…";
  try {
    const blob = await (await postJSON(sessionUrl("render"), renderBody(true))).blob();
    if (seq !== previewSeq) return;  // a newer preview is on its way
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = URL.createObjectURL(blob);
    const img = $("preview");
    img.onload = () => { sizePreview(); img.hidden = S.tab !== "preview"; $("status").textContent = `Output ${img.naturalWidth} × ${img.naturalHeight} px`; };
    img.src = previewUrl;
  } catch (err) { fail(err); }
}

function sizePreview() {
  const img = $("preview"), vp = $("viewport");
  if (!img.naturalWidth) return;
  const scale = S.fit ? Math.min((vp.clientWidth - 32) / img.naturalWidth, (vp.clientHeight - 32) / img.naturalHeight, 4) : S.zoom;
  img.style.width = Math.round(img.naturalWidth * scale) + "px";
}

function saveBlob(blob, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

async function download() {
  if (!S.session) return;
  const used = ["left", "right"].map((s) => lane(S.sides[s])).filter(Boolean);
  if (!used.length && !confirm("No ladder is selected for the left or right labels. Download the image without labels?")) return;
  for (const ln of used) {
    const counts = {};
    for (const b of ln.bands) if (b.kda != null) counts[b.kda] = (counts[b.kda] || 0) + 1;
    if (Object.values(counts).some((c) => c > 1) &&
        !confirm(`${laneName(ln)} uses some kDa values more than once. Download anyway?`)) return;
  }
  setBusy(true, "Rendering…");
  try {
    const body = renderBody(false);
    const ext = body.format === "tiff" ? "tif" : "png";
    const blob = await (await postJSON(sessionUrl("render"), body)).blob();
    saveBlob(blob, `${S.session.name}_labeled.${ext}`);
    if ($("opt-json").checked) {
      const laneOut = (ln) => ln && {
        name: laneName(ln), x: ln.x, width: ln.width,
        bands: [...ln.bands].sort((a, b) => a.y - b.y).map((b) => ({ y: b.y, kda: b.kda, show: b.show })),
      };
      const sidecar = {
        source: S.files.blot?.name, marker: S.files.marker?.name || null, ladder: ladder(),
        image_size: { width: S.session.width, height: S.session.height },
        detection_limits: limits(), crop: S.crop,
        rotation_degrees_clockwise: S.geom.angle,
        coordinates_note: "All coordinates are in the rotated image.",
        merge: S.session.has_marker ? S.merge : null,
        lane_labels: laneLabelsBody(),
        left_labels_from: lane(S.sides.left) ? laneName(lane(S.sides.left)) : null,
        right_labels_from: lane(S.sides.right) ? laneName(lane(S.sides.right)) : null,
        ladders: S.lanes.map(laneOut),
      };
      saveBlob(new Blob([JSON.stringify(sidecar, null, 2)], { type: "application/json" }),
               `${S.session.name}_labeled.json`);
    }
    setBusy(false, "Saved");
  } catch (err) { fail(err); }
}

// ─────────────────────────── glue ───────────────────────────
function refreshControls() {
  const has = !!S.session;
  $("btn-detect").disabled = !has || S.busy;
  $("btn-download").disabled = !has || S.busy;
  const ln = lane();
  $("btn-assign").disabled = !ln || S.busy || ln.bands.length < 2;
  $("btn-shift-up").disabled = !ln || !ln.bands.length;
  $("btn-shift-down").disabled = !ln || !ln.bands.length;
  $("btn-add-lane").disabled = !has || S.busy;
  $("btn-add-lane").classList.toggle("on", S.addingLane);
  const mBtn = $("views").querySelector('[data-view="marker"]');
  mBtn.disabled = !S.images.marker;
  $("views").querySelector('[data-view="merged"]').disabled = !S.session?.has_marker;
  $("merge-section").hidden = !S.session?.has_marker;
  $("lanes-secnum").textContent = S.session?.has_marker ? "6" : "5";
  $("output-secnum").textContent = S.session?.has_marker ? "7" : "6";
  for (const id of ["btn-straighten", "geom-angle", "geom-angle-range",
                    "btn-find-lanes", "btn-clear-lanes", "btn-apply-names"]) $(id).disabled = !has || S.busy;
  $("btn-reset-geom").hidden = !has || S.geom.angle === 0;
  for (const b of $("views").querySelectorAll("button")) b.classList.toggle("on", b.dataset.view === S.view);
  $("views").style.visibility = S.tab === "preview" ? "hidden" : "";
  for (const id of ["crop-x", "crop-y", "crop-w", "crop-h", "side-left", "side-right"]) $(id).disabled = !has;
}

function afterChange(rerenderPreview = true) {
  renderLaneLabelTable();
  renderLaneChips();
  renderTable();
  renderSideSelects();
  renderCropInputs();
  renderLimitsText();
  refreshControls();
  showMessages();
  if (!S.session) return;
  canvas.hidden = S.tab === "preview";
  const img = $("preview");
  img.hidden = S.tab !== "preview" || !img.getAttribute("src");
  if (S.tab === "preview") { if (rerenderPreview) refreshPreview(); else sizePreview(); }
  else {
    layout();
    if (S.view === "merged" && S.mergedDirty) refreshMergedSoon();
  }
}

// ─────────────────────────── geometry (rotation) ───────────────────────────
// Same conventions as geometry.py: angle in degrees (+ = clockwise) about the image
// centre, with the canvas expanded to fit. Whole-image rotation only: local warps
// (e.g. "smile" straightening) move bands relative to each other and are not offered.
function geomForward(x, y, g, W1, H1) {
  const a = g.angle * Math.PI / 180, { w: W0, h: H0 } = S.size0;
  const dx = x - W0 / 2, dy = y - H0 / 2;
  return [dx * Math.cos(a) - dy * Math.sin(a) + W1 / 2, dx * Math.sin(a) + dy * Math.cos(a) + H1 / 2];
}

function geomInverse(x, y, g, W1, H1) {
  const a = g.angle * Math.PI / 180, { w: W0, h: H0 } = S.size0;
  const dx = x - W1 / 2, dy = y - H1 / 2;
  return [dx * Math.cos(a) + dy * Math.sin(a) + W0 / 2, -dx * Math.sin(a) + dy * Math.cos(a) + H0 / 2];
}

function syncGeometryInputs() {
  $("geom-angle").value = S.geom.angle;
  $("geom-angle-range").value = clamp(S.geom.angle, -15, 15);
}

function setupGeometryControls() {
  const angleRange = $("geom-angle-range");
  // Live feedback while dragging: rotate the canvas with CSS, then apply for real on release.
  angleRange.addEventListener("input", () => {
    $("geom-angle").value = angleRange.value;
    if (S.tab !== "preview") canvas.style.transform = `rotate(${+angleRange.value - S.geom.angle}deg)`;
  });
  angleRange.addEventListener("change", () => applyGeometry(+angleRange.value));
  $("geom-angle").addEventListener("change", () => applyGeometry(+$("geom-angle").value || 0));
  $("btn-reset-geom").onclick = () => applyGeometry(0);
  $("btn-straighten").onclick = async () => {
    if (!S.session) return;
    setBusy(true, "Finding the straightening angle…");
    try {
      const r = await (await postJSON(sessionUrl("straighten"), {})).json();
      setBusy(false);
      await applyGeometry(r.angle);
      $("status").textContent = `Auto-straighten: ${r.angle > 0 ? "+" : ""}${r.angle}°`;
    } catch (err) { fail(err); }
  };
}

async function applyGeometry(angle) {
  if (!S.session) return;
  angle = Math.round(clamp(angle, -45, 45) * 100) / 100;
  if (angle === S.geom.angle) { canvas.style.transform = ""; syncGeometryInputs(); return; }
  const old = { ...S.geom }, oldW = S.session.width, oldH = S.session.height;
  setBusy(true, "Applying rotation…");
  try {
    const r = await (await postJSON(sessionUrl("geometry"), { angle })).json();
    S.geom = { angle: r.angle };
    S.session.width = r.width; S.session.height = r.height;
    S.session.auto_font_size = r.auto_font_size;
    $("opt-font").placeholder = `auto (${r.auto_font_size})`;
    const map = (x, y) => {
      const [x0, y0] = geomInverse(x, y, old, oldW, oldH);
      return geomForward(x0, y0, S.geom, r.width, r.height);
    };
    for (const ln of S.lanes) {
      const mid = ln.bands.length ? ln.bands.map((b) => map(ln.x, b.y)) : [map(ln.x, oldH / 2)];
      for (let i = 0; i < ln.bands.length; i++) ln.bands[i].y = mid[i][1];
      ln.predicted = ln.predicted.map((p) => ({ ...p, y: p.y == null ? null : map(ln.x, p.y)[1] }));
      ln.extraPeaks = ln.extraPeaks.map((y) => map(ln.x, y)[1]);
      ln.x = mid.reduce((a, m) => a + m[0], 0) / mid.length;
    }
    if (!S.userLimits) {
      // Automatic limits are recomputed on the straightened image by the next detection.
      S.yRange = null; S.autoLimits = false;
    } else if (S.yRange) {
      const cx = oldW / 2;
      S.yRange = [clamp(map(cx, S.yRange[0])[1], 0, r.height - 1), clamp(map(cx, S.yRange[1])[1], 0, r.height - 1)];
    }
    for (const ll of S.laneLabels) ll.x = clamp(map(ll.x, oldH / 2)[0], 0, r.width - 1);
    sortLanes(); sortLaneLabels();
    resetCrop();
    await loadImages();
    canvas.style.transform = "";
    syncGeometryInputs();
    setBusy(false, `Rotation ${S.geom.angle}°. Click "Detect ladders" to re-detect on the straightened image.`);
  } catch (err) { canvas.style.transform = ""; syncGeometryInputs(); fail(err); return; }
  afterChange();
}

// ─────────────────────────── merge marker ───────────────────────────
function syncMergeInputs() {
  const m = S.merge;
  $("merge-on").checked = m.enabled;
  $("merge-mode").value = m.mode;
  $("merge-opacity").value = Math.round(m.opacity * 100);
  $("merge-opacity-text").textContent = `${Math.round(m.opacity * 100)}%`;
  $("merge-color-mode").value = m.color === "marker" ? "marker" : "custom";
  $("merge-color").hidden = m.color === "marker";
  if (m.color !== "marker") $("merge-color").value = m.color;
  $("merge-lanes-only").checked = m.lanes_only;
}

function setupMergeControls() {
  const changed = () => {
    S.merge = {
      enabled: $("merge-on").checked, mode: $("merge-mode").value,
      opacity: +$("merge-opacity").value / 100,
      color: $("merge-color-mode").value === "marker" ? "marker" : $("merge-color").value,
      lanes_only: $("merge-lanes-only").checked,
    };
    syncMergeInputs();
    S.mergedDirty = true;
    if (S.merge.enabled && S.view !== "merged" && S.tab !== "preview" && S.session?.has_marker) setView("merged");
    else afterChange();
  };
  for (const id of ["merge-on", "merge-mode", "merge-color-mode", "merge-color", "merge-lanes-only"]) {
    $(id).addEventListener("change", changed);
  }
  $("merge-opacity").addEventListener("input", () => {
    $("merge-opacity-text").textContent = `${$("merge-opacity").value}%`;
  });
  $("merge-opacity").addEventListener("change", changed);
}

let mergedSeq = 0;
async function refreshMerged() {
  if (!S.session?.has_marker) return;
  const seq = ++mergedSeq;
  try {
    const blob = await (await postJSON(sessionUrl("merged"), {
      merge: S.merge, ladder_lanes: S.lanes.map((l) => ({ x: l.x, width: l.width })),
    })).blob();
    if (seq !== mergedSeq) return;
    const url = URL.createObjectURL(blob);
    await new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => { S.images.merged = img; resolve(); };
      img.onerror = () => reject(new Error("Could not display the merged image."));
      img.src = url;
    });
    if (S.mergedUrl) URL.revokeObjectURL(S.mergedUrl);
    S.mergedUrl = url;
    S.mergedDirty = false;
    draw();
  } catch (err) { fail(err); }
}
const refreshMergedSoon = debounce(refreshMerged, 250);

// ─────────────────────────── lane labels ───────────────────────────
function sortLaneLabels() { S.laneLabels.sort((a, b) => a.x - b.x); }

function laneNumbers() {
  const nums = new Map();
  let n = 0;
  for (const ll of S.laneLabels) nums.set(ll.id, ll.numbered ? String(++n) : null);
  return nums;
}

function laneLabelsBody() {
  if (!S.laneLabels.length) return null;
  const nums = laneNumbers();
  return {
    items: S.laneLabels.map((ll) => ({ x: ll.x, number: nums.get(ll.id), name: ll.name })),
    show_numbers: $("ll-numbers").checked, show_names: $("ll-names").checked,
    angle: +$("ll-angle").value, position: $("ll-position").value,
  };
}

function setupLaneLabelControls() {
  $("btn-find-lanes").onclick = () => findLanes();
  $("btn-clear-lanes").onclick = () => { S.laneLabels = []; S.llSelected = null; afterChange(); };
  for (const id of ["ll-numbers", "ll-names", "ll-angle", "ll-position"]) {
    $(id).addEventListener("change", () => afterChange());
  }
  $("btn-apply-names").onclick = () => {
    const raw = $("names-text").value;
    const names = (raw.includes("\n") ? raw.split("\n") : raw.split(",")).map((t) => t.trim()).filter(Boolean);
    const targets = S.laneLabels.filter((ll) => ll.numbered);
    if (!names.length || !targets.length) return;
    targets.forEach((ll, i) => { if (i < names.length) ll.name = names[i]; });
    S.error = names.length > targets.length
      ? `${names.length - targets.length} name(s) left over. Add lanes in the Lanes tab (Shift-click).` : null;
    afterChange();
  };
}

async function findLanes() {
  if (!S.session) return;
  S.error = null;
  setBusy(true, "Finding lanes…");
  try {
    const r = await (await postJSON(sessionUrl("lanes"), {
      polarity: $("polarity").value, y_range: S.yRange, ladder_xs: S.lanes.map((l) => l.x),
    })).json();
    // Keep names already typed, in order, for the numbered lanes.
    const oldNames = S.laneLabels.filter((ll) => ll.numbered).map((ll) => ll.name);
    S.laneLabels = r.lanes.map((ln) => ({
      id: S.nextId++, x: ln.x, name: ln.is_ladder ? "M" : "", numbered: !ln.is_ladder,
    }));
    S.laneLabels.filter((ll) => ll.numbered).forEach((ll, i) => { if (oldNames[i]) ll.name = oldNames[i]; });
    S.lanesFound = true;
    S.llSelected = null;
    setBusy(false, `Found ${S.laneLabels.length} lanes`);
  } catch (err) { fail(err); return; }
  afterChange();
}

function addLaneLabel(x) {
  const ll = { id: S.nextId++, x, name: "", numbered: true };
  S.laneLabels.push(ll);
  sortLaneLabels();
  S.llSelected = ll.id;
  afterChange();
  setTimeout(() => document.querySelector(`[data-ll="${ll.id}"]`)?.focus(), 0);
}

function deleteLaneLabel(id) {
  S.laneLabels = S.laneLabels.filter((ll) => ll.id !== id);
  if (S.llSelected === id) S.llSelected = null;
  afterChange();
}

function hitLaneLabel(p) {
  const tol = 8 / S.zoom;
  let best = null, bd = Infinity;
  for (const ll of S.laneLabels) { const d = Math.abs(ll.x - p.x); if (d < tol && d < bd) { bd = d; best = ll; } }
  return best;
}

function renderLaneLabelTable() {
  const tbody = $("lane-table").querySelector("tbody");
  const focused = document.activeElement?.dataset?.ll;
  tbody.innerHTML = "";
  const nums = laneNumbers();
  for (const ll of S.laneLabels) {
    const tr = document.createElement("tr");
    if (ll.id === S.llSelected) tr.className = "sel";
    tr.onclick = (e) => { if (!e.target.closest("input,button")) { S.llSelected = ll.id; afterChange(false); } };
    const tdN = document.createElement("td");
    tdN.textContent = nums.get(ll.id) ?? "–";
    const tdName = document.createElement("td");
    const inp = document.createElement("input");
    inp.type = "text"; inp.value = ll.name; inp.placeholder = nums.get(ll.id) ? "sample name" : "";
    inp.dataset.ll = ll.id;
    inp.addEventListener("focus", () => { if (S.llSelected !== ll.id) { S.llSelected = ll.id; draw(); } });
    inp.addEventListener("input", () => { ll.name = inp.value; draw(); });
    inp.addEventListener("change", () => { if (S.tab === "preview") refreshPreview(); });
    inp.addEventListener("keydown", (e) => {  // Enter → next lane's name
      if (e.key !== "Enter") return;
      e.preventDefault();
      const next = S.laneLabels[S.laneLabels.indexOf(ll) + 1];
      if (next) document.querySelector(`[data-ll="${next.id}"]`)?.focus(); else inp.blur();
    });
    tdName.appendChild(inp);
    const tdC = document.createElement("td");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = ll.numbered; cb.title = "Include in lane numbering";
    cb.onchange = () => { ll.numbered = cb.checked; afterChange(); };
    tdC.appendChild(cb);
    const tdD = document.createElement("td");
    const del = document.createElement("button");
    del.className = "del"; del.textContent = "×"; del.title = "Delete lane";
    del.onclick = () => deleteLaneLabel(ll.id);
    tdD.appendChild(del);
    tr.append(tdN, tdName, tdC, tdD);
    tbody.appendChild(tr);
  }
  $("lane-label-count").textContent = S.laneLabels.length ? `${S.laneLabels.length} lanes` : "";
  if (focused) document.querySelector(`[data-ll="${focused}"]`)?.focus();
}

function drawLaneLabels(u) {
  const H = S.session.height;
  const nums = laneNumbers();
  const c = S.crop;
  // Dim the area outside the crop so it is clear which lanes will be labelled.
  if (c && !cropIsFull()) {
    ctx.fillStyle = "rgba(15,23,42,0.35)";
    ctx.fillRect(0, 0, c.x, H);
    ctx.fillRect(c.x + c.w, 0, S.session.width - c.x - c.w, H);
  }
  for (const ll of S.laneLabels) {
    const sel = ll.id === S.llSelected;
    ctx.strokeStyle = sel ? ACCENT : "rgba(22,163,74,0.9)";
    ctx.lineWidth = (sel ? 2.5 : 1.5) * u;
    ctx.setLineDash(sel ? [] : [6 * u, 4 * u]);
    ctx.beginPath(); ctx.moveTo(ll.x, 0); ctx.lineTo(ll.x, H); ctx.stroke();
    ctx.setLineDash([]);
    const n = nums.get(ll.id);
    const text = [n, ll.name].filter(Boolean).join(" · ") || "(unnamed)";
    ctx.save();
    ctx.translate(ll.x, 8 * u);
    ctx.rotate(Math.PI / 2);  // vertical chips so neighbouring lanes don't overlap
    chip(text, 0, 0, "left", sel ? ACCENT : "rgba(22,163,74,0.9)", "#fff", u);
    ctx.restore();
  }
  if (!S.laneLabels.length) {
    chip("No lanes yet. Click “Find lanes”, or Shift-click to add one.", 8 * u, 16 * u, "left", ACCENT, "#fff", u);
  }
}

init().catch(fail);
