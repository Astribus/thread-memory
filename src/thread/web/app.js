// Thread web client: map, folder sidebar, note tabs, Home and Me.

const SETTINGS = { center: 0.52, repel: 10, linkStrength: 1, linkDistance: 250,
                   nodeSize: 1.31, lineSize: 1.57, textFade: 0.30 };
// Force-layout constants, fitted so drag motion matches Obsidian's graph view.
// MAP_SCALE scales distances (×s) and repulsion (×s²) without changing the motion.
const OBSIDIAN_FIT = { repel: 413, distance: 258, link: 0.357, center: 0.0433,
                       friction: 0.379, cooling: 0.01275, dragHeat: 0.504, grabHeat: 0.409, releaseHeat: 0.060 };
const MAP_SCALE = 0.777;
// New-note pop: spawn next to the linked note, let repulsion push it out, fade in.
// `heat` is the alpha that produces that burst.
const POP = { spawn: 25 * MAP_SCALE, heat: 0.38, fadeFrom: 0.1, fadeMs: 158, fadeTotalMs: 1200 };
const PHYS = { ...OBSIDIAN_FIT, repel: -OBSIDIAN_FIT.repel * MAP_SCALE ** 2, distance: OBSIDIAN_FIT.distance * MAP_SCALE };
// Same curve as CSS --ease: cubic-bezier(0.16, 1, 0.3, 1).
function cubicBezier(x1, y1, x2, y2) {
  const coord = (t, a, b) => 3 * a * t * (1 - t) ** 2 + 3 * b * t * t * (1 - t) + t ** 3;
  return x => {
    let lo = 0, hi = 1, t = x;
    for (let i = 0; i < 24; i++) {   // solve x(t) = target, return y(t)
      t = (lo + hi) / 2;
      if (coord(t, x1, x2) < x) lo = t; else hi = t;
    }
    return coord(t, y1, y2);
  };
}
const EASE = cubicBezier(0.16, 1, 0.3, 1);
const $ = id => document.getElementById(id);

// Per-viewer UI state in localStorage, namespaced by account (thread_acct cookie) on shared machines.
const ACCOUNT = (document.cookie.match(/(?:^|; )thread_acct=([a-f0-9]+)/) || [])[1];
const PREFIX = ACCOUNT ? `thread.${ACCOUNT}.` : "thread.";
const store = {
  get(key, fallback) { try { return JSON.parse(localStorage.getItem(PREFIX + key)) ?? fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(PREFIX + key, JSON.stringify(value)); } catch {} },
};
async function send(method, url, body) {
  const res = await fetch(url, { method, headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json();
  if (!res.ok) {
    const err = new Error(data.error || "Something went wrong");
    err.reauth = !!data.reauth; err.field = data.field;   // reauth: server wants step-up first
    throw err;
  }
  return data;
}
function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]); }
function typing() { const el = document.activeElement; return el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA"); }

// =====================================================================
// Map
// =====================================================================
const tip = $("tip");
let nodes = [], edges = [], byId = new Map(), neighbors = new Map();
let popping = new Set();   // ids that are fading in
let lastJson = "", focusId = null, searchTerm = "", currentZoom = 1, needsFit = true, userMovedView = false;

// Colour group: one per class, otherwise the top-level folder.
function groupOf(n) {
  if (n.id === "me") return "me";
  if (n.unresolved) return "unwritten";
  const parts = n.folder.split("/");
  if (parts[0] === "classes" && parts.length > 1) return "classes/" + parts[1];
  return parts[0] === "." ? "inbox" : parts[0];
}
// Class colours come from --c0…--c7 in style.css; users can override per class (/api/class-colors).
const SLATE = "var(--knot-me)", SAGE = "var(--knot)";
const PALETTE_SIZE = 8;
let classColorPicks = {};
// Canvas courses without a folder yet, so Home can still colour their deadlines.
const dueClassKeys = new Set();
function classKey(n) {
  if (typeof n === "string") n = byId.get(n);   // unresolved link ids before forceLink runs
  if (!n || n.folder === undefined) return null;
  const g = groupOf(n); return g.startsWith("classes/") ? g.slice(8) : null;
}
function classIndex(key) {
  if (key in classColorPicks) return classColorPicks[key];
  // Unpicked classes take free colours in name order, so assignments are stable.
  const all = [...new Set([...nodes.map(classKey).filter(Boolean), ...dueClassKeys])].sort();
  const taken = new Set(Object.values(classColorPicks));
  const free = [...Array(PALETTE_SIZE).keys()].filter(i => !taken.has(i));
  const unpicked = all.filter(k => !(k in classColorPicks));
  const at = unpicked.indexOf(key);
  return (free.length ? free : [...Array(PALETTE_SIZE).keys()])[Math.max(0, at) % (free.length || PALETTE_SIZE)];
}
const classColor = key => `var(--c${classIndex(key)})`;
function colorOf(n) {
  const g = groupOf(n);
  if (g === "me") return SLATE;
  if (g === "unwritten") return "var(--unwritten)";
  const key = classKey(n);
  return key ? classColor(key) : SAGE;
}
// Edges take the colour of whichever endpoint is in a class.
function edgeColor(e) {
  const k = classKey(e.target) || classKey(e.source);
  return k ? classColor(k) : "var(--thread)";
}
// Edges bend by 14% of their length; the side is fixed per pair so it never flips mid-drag.
function threadPath(e) {
  const { x: x1, y: y1 } = e.source, { x: x2, y: y2 } = e.target;
  const side = hash((e.source.id || "") + ">" + (e.target.id || "")) < 0.5 ? -1 : 1;
  const mx = (x1 + x2) / 2 - (y2 - y1) * 0.14 * side, my = (y1 + y2) / 2 + (x2 - x1) * 0.14 * side;
  return `M${x1},${y1}Q${mx},${my} ${x2},${y2}`;
}
const colorOfId = id => colorOf(byId.get(id) || { id, folder: id.split("/").slice(0, -1).join("/") || ".", unresolved: false });
function labelOf(group) {
  if (group.startsWith("classes/")) {
    const hub = byId.get(group + "/" + group.split("/")[1]);
    return hub ? hub.title : group.split("/")[1];
  }
  return group;
}
// Disambiguate duplicate titles (e.g. "Upcoming deadlines") with the class.
let displayTitle = n => n.title;
function computeDisplayTitles() {
  const counts = d3.rollup(nodes, v => v.length, n => n.title);
  displayTitle = n => counts.get(n.title) > 1 && groupOf(n).startsWith("classes/")
    ? `${n.title} · ${labelOf(groupOf(n)).split(" ")[0]}` : n.title;
}
const titleOf = id => byId.has(id) ? displayTitle(byId.get(id)) : id.split("/").pop();
// Deterministic 0..1 hash (FNV-1a) so layouts are reproducible.
function hash(text) {
  let h = 2166136261;
  for (const c of text) h = Math.imul(h ^ c.charCodeAt(0), 16777619);
  return (h >>> 0) / 4294967296;
}
function savePositions() {
  const pos = {};
  // Unresolved nodes are saved too; otherwise every load re-settles the whole layout.
  for (const n of nodes) pos[n.id] = [Math.round(n.x), Math.round(n.y)];
  store.set("positions.v3", pos);
}

const simulation = d3.forceSimulation()
  .velocityDecay(PHYS.friction)
  .alphaDecay(PHYS.cooling)
  .force("charge", d3.forceManyBody().strength(PHYS.repel))
  .force("link", d3.forceLink().id(d => d.id).distance(PHYS.distance)
    .strength(l => PHYS.link / Math.min(l.source.degree || 1, l.target.degree || 1)))
  .force("x", d3.forceX(0).strength(PHYS.center))
  .force("y", d3.forceY(0).strength(PHYS.center))
  .on("tick", draw)
  .on("end", () => {
    savePositions();
    queueCull();
    if (!userMovedView && active === "map" && !localRoot && offScreen()) fit(500);
  });

function setGraph(data) {
  const old = byId;
  byId = new Map();
  nodes = data.nodes.map(n => {
    const node = Object.assign(old.get(n.id) || {}, n);   // keep x/y across updates
    byId.set(n.id, node);
    return node;
  });
  edges = data.edges.map(e => ({ source: e.source, target: e.target }));
  neighbors = new Map(nodes.map(n => [n.id, new Set()]));
  for (const e of edges) { neighbors.get(e.source).add(e.target); neighbors.get(e.target).add(e.source); }
  // Me is pinned to the origin only during the first settle.
  const me = byId.get("me");
  const saved = old.size ? {} : store.get("positions.v3", {});
  let restored = 0;
  for (const n of nodes) {
    if (n.x === undefined && saved[n.id]) { [n.x, n.y] = saved[n.id]; restored++; }
  }
  // New notes spawn beside a linked note, at a hash-derived angle.
  for (const n of nodes) {
    if (n.x === undefined && n.id !== "me") {
      const anchor = [...neighbors.get(n.id)].map(id => byId.get(id)).find(m => m.x !== undefined);
      if (anchor) {
        const angle = hash(n.id) * Math.PI * 2;
        n.x = anchor.x + Math.cos(angle) * POP.spawn;
        n.y = anchor.y + Math.sin(angle) * POP.spawn;
      }   // otherwise d3's phyllotaxis placement
    }
  }
  popping = old.size ? new Set(nodes.filter(n => !old.has(n.id)).map(n => n.id)) : new Set();
  $("empty").hidden = nodes.length > 1;
  computeDisplayTitles();
  renderMap();
  if (localRoot) applyLocal(false);
  simulation.nodes(nodes);
  simulation.force("link").links(edges);
  if (!old.size) {
    // First load: settle fully off-screen, then fit. With saved positions only the new nodes need settling.
    simulation.stop();
    const fresh = restored < nodes.length * 0.6;
    if (fresh) {
      if (me) { me.fx = 0; me.fy = 0; }
      simulation.alpha(1);
      while (simulation.alpha() > simulation.alphaMin()) simulation.tick();
      if (me) { me.fx = null; me.fy = null; }
    } else if (restored < nodes.length - 1) {
      const pinned = nodes.filter(n => saved[n.id]);
      pinned.forEach(n => { n.fx = n.x; n.fy = n.y; });
      simulation.alpha(0.3);
      // Everything else is pinned, so ~150 ticks is enough.
      for (let i = 0; i < 150 && simulation.alpha() > simulation.alphaMin(); i++) simulation.tick();
      pinned.forEach(n => { n.fx = null; n.fy = null; });
    }
    nodes.forEach(n => { n.vx = 0; n.vy = 0; });
    // Start at rest; d3 defaults to alpha 1 and the first drag would scatter the map.
    simulation.alpha(0);
    draw();
    savePositions();
    queueCull();
  } else if (popping.size) {
    simulation.alpha(Math.max(simulation.alpha(), POP.heat)).restart();
  } else {
    // Only titles or links changed: redraw without reheating.
    draw();
  }
  // Fit once the map is actually visible (it starts hidden behind Home).
  if (needsFit && active === "map" && $("map").getBoundingClientRect().width) { needsFit = false; fit(0); }
}

// Per-node render cache (colour, size, curve side), rebuilt on graph changes.
// Computing class colours per frame would be O(n²).
let popStart = 0;
function renderMap() {
  const real = nodes.filter(n => !n.unresolved).length;
  $("count").textContent = `${real} ${real === 1 ? "note" : "notes"} · ${edges.length} ${edges.length === 1 ? "link" : "links"}`;
  for (const n of nodes) {
    n._color = colorOf(n);
    n._size = obsSize(n);
    n._tw = 0;   // remeasured on next draw
  }
  for (const e of edges) {
    const s = e.source.id || e.source, t = e.target.id || e.target;
    e._color = edgeColor(e);
    e._side = hash(s + ">" + t) < 0.5 ? -1 : 1;
  }
  if (popping.size) popStart = performance.now();
  pickLandmarks();
  renderMapList();
  requestDraw();
}
function markActiveNode() { requestDraw(); }

// Canvas renderer. Lines and dots are batched by colour; off-screen items are skipped.
const canvas = $("map"), mapSel = d3.select(canvas), ctx = canvas.getContext("2d");
let transform = d3.zoomIdentity, dpr = window.devicePixelRatio || 1, mapW = 0, mapH = 0;

// Sizing follows Obsidian's graph renderer:
//   radius = nodeSize × clamp(3·√(links+1), 8, 30)
//   on screen = radius × √zoom
//   titles = (14 + size/4) × √zoom, alpha from log2(zoom)
//   lines = constant width
// Obsidian works in device pixels, so its zoom ≈ k × devicePixelRatio × MAP_SCALE.
const OBS = { min: 8, per: 3, max: 30 };
const LABEL_DPR_CAP = 2;   // size titles for ≤2× density so they stay readable on 3× phones
const DIM = 0.1;   // alpha for de-emphasised nodes
const LANDMARK_PX = 12, ME_PX = 16, FOCUS_PX = 11, READABLE_PX = 6.5;   // minimum title sizes, in points
const RING_MIN_PX = 2.5;   // skip the dashed ring below this radius
const obsSize = n => SETTINGS.nodeSize * Math.max(OBS.min, Math.min(OBS.per * Math.sqrt((neighbors.get(n.id)?.size || 0) + 1), OBS.max));
const obsZoom = (k = transform.k) => k * dpr * MAP_SCALE;
const dotPx = (n, k = transform.k) => n._size * Math.sqrt(obsZoom(k)) / dpr;   // on-screen radius
const lineWidthPx = () => SETTINGS.lineSize / dpr;
const titleFade = k => Math.max(0, Math.min(1, Math.log2(obsZoom(k)) + 1 - SETTINGS.textFade));

// Canvas can't read CSS vars; cache them until the theme changes.
let colorCache = new Map();
function cssColor(value) {
  let c = colorCache.get(value);
  if (c === undefined) {
    const m = /^var\((--[\w-]+)\)$/.exec(value);
    c = m ? getComputedStyle(document.documentElement).getPropertyValue(m[1]).trim() || "#888" : value;
    colorCache.set(value, c);
  }
  return c;
}
let FONTS = null;
function themeChanged() { colorCache = new Map(); FONTS = null; requestDraw(); }
document.fonts?.ready.then(() => { for (const n of nodes) n._tw = 0; requestDraw(); });

function resizeCanvas() {
  const box = canvas.getBoundingClientRect();
  dpr = window.devicePixelRatio || 1;
  mapW = box.width; mapH = box.height;
  canvas.width = Math.round(mapW * dpr); canvas.height = Math.round(mapH * dpr);
  draw();
  if (simulation.alpha() < simulation.alphaMin()) queueCull();   // cull labels once the layout is at rest
}
new ResizeObserver(resizeCanvas).observe(canvas);

// Visual state per node; fades ease 10% per frame.
let focusKeep = null, searchKeep = null, replayShown = null;
const isGone = n => (localKeep && !localKeep.has(n.id)) || (replayShown && !replayShown.has(n.id));
const isDim = n => focusId ? !focusKeep.has(n.id) : !!searchKeep && !searchKeep.has(n.id);
// Fade-in: 1 − 0.9·e^(−t/158ms).
function popAlpha(n, now) {
  if (!popping.has(n.id)) return 1;
  const ms = now - popStart;
  if (ms >= POP.fadeTotalMs) { popping.delete(n.id); return 1; }
  const f = (1 - Math.exp(-ms / POP.fadeMs)) / (1 - Math.exp(-POP.fadeTotalMs / POP.fadeMs));
  return POP.fadeFrom + (1 - POP.fadeFrom) * f;
}

let frameQueued = false, lastPaint = 0;
function requestDraw() { if (!frameQueued) { frameQueued = true; requestAnimationFrame(t => { frameQueued = false; draw(t); }); } }
// Draw one frame; keep requesting frames while anything is fading.
function draw(now = performance.now()) {
  if (!mapW || !mapH) return;
  const t = transform, k = t.k, W = mapW, H = mapH, M = 40;
  const step = 1 - Math.pow(0.9, Math.min(100, Math.max(1, now - (lastPaint || now))) / 16.7);
  lastPaint = now;
  let fading = popping.size > 0;
  const toward = (a, b) => {
    if (a === undefined || Math.abs(b - a) < 0.004) return b;
    fading = true;
    return a + (b - a) * step;
  };
  if (!FONTS) {
    const css = getComputedStyle(document.documentElement);
    FONTS = { sans: css.getPropertyValue("--sans").trim() || "sans-serif", serif: css.getPropertyValue("--serif").trim() || "serif",
              bg: cssColor("var(--bg)"), ink: cssColor("var(--ink)"), slate: cssColor("var(--slate)"), unwritten: cssColor("var(--unwritten)") };
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  ctx.globalAlpha = 1;
  for (const n of nodes) {
    if (n.x === undefined) { n._on = false; continue; }
    n._sx = t.x + k * n.x; n._sy = t.y + k * n.y;
    n._on = n._sx > -M && n._sx < W + M && n._sy > -M && n._sy < H + M;
    n._a = toward(n._a, isGone(n) ? 0 : isDim(n) ? DIM : 1);
    n._p = popAlpha(n, now);
    n._r = dotPx(n, k);
  }
  // Edges, batched by colour and alpha.
  const lw = lineWidthPx(), lineBatches = new Map();
  for (const e of edges) {
    const a = e.source, b = e.target;
    if (a.x === undefined || b.x === undefined) continue;
    const touches = focusId && (a.id === focusId || b.id === focusId);
    const target = isGone(a) || isGone(b) ? 0 : focusId ? (touches ? 1 : DIM)
      : searchKeep && !(searchKeep.has(a.id) && searchKeep.has(b.id)) ? DIM : 1;
    e._a = toward(e._a, target);
    const alpha = e._a * Math.min(a._p, b._p);
    if (alpha < 0.01) continue;
    if (Math.max(a._sx, b._sx) < -M || Math.min(a._sx, b._sx) > W + M || Math.max(a._sy, b._sy) < -M || Math.min(a._sy, b._sy) > H + M) continue;
    const key = e._color + "|" + (touches ? 1 : 0) + "|" + Math.round(alpha * 20);
    let list = lineBatches.get(key);
    if (!list) lineBatches.set(key, list = []);
    list.push(e);
  }
  ctx.lineCap = "round";
  for (const [key, list] of lineBatches) {
    const [color, lit, a] = key.split("|");
    ctx.strokeStyle = cssColor(color);
    ctx.globalAlpha = (+a / 20) * (lit === "1" ? 0.9 : 0.55);
    ctx.lineWidth = lit === "1" ? lw * 1.6 : lw;
    ctx.beginPath();
    for (const e of list) {
      const x1 = e.source._sx, y1 = e.source._sy, x2 = e.target._sx, y2 = e.target._sy;
      ctx.moveTo(x1, y1);
      ctx.quadraticCurveTo((x1 + x2) / 2 - (y2 - y1) * 0.14 * e._side, (y1 + y2) / 2 + (x2 - x1) * 0.14 * e._side, x2, y2);
    }
    ctx.stroke();
  }
  // Titles with a background-coloured halo. Halos are drawn before dots and text after, so halos never cover dots.
  const titles = [];
  for (const n of nodes) {
    if (!n._on) continue;
    const la = labelAlpha(n, k) * n._a * n._p;
    if (la < 0.02) continue;
    const g = labelGeom(n, k);
    titles.push({ n, la, g, font: `${n.id === "me" ? "400" : "500"} ${g.px.toFixed(1)}px ${n.id === "me" ? FONTS.serif : FONTS.sans}`, text: displayTitle(n) });
  }
  ctx.textAlign = "center"; ctx.textBaseline = "top"; ctx.lineJoin = "round"; ctx.strokeStyle = FONTS.bg;
  let font = "";
  for (const l of titles) {
    if (l.font !== font) ctx.font = font = l.font;
    ctx.globalAlpha = l.la * 0.92; ctx.lineWidth = l.g.px * 0.5;
    ctx.strokeText(l.text, l.n._sx, l.g.y);
  }
  // Nodes: filled dot plus dashed ring; unresolved nodes are hollow.
  const dotBatches = new Map(), hollow = [], marked = [];
  for (const n of nodes) {
    if (!n._on) continue;
    const alpha = n._a * n._p;
    if (alpha < 0.01) continue;
    if (n.unresolved) { hollow.push(n); continue; }
    const key = n._color + "|" + Math.round(alpha * 20);
    let list = dotBatches.get(key);
    if (!list) dotBatches.set(key, list = []);
    list.push(n);
    if (n.id === lastNote || n.id === focusId) marked.push(n);
  }
  ctx.lineWidth = 1;
  for (const [key, list] of dotBatches) {
    const [color, a] = key.split("|");
    const alpha = +a / 20, fill = cssColor(color);
    ctx.globalAlpha = alpha * 0.45;
    ctx.strokeStyle = fill;
    ctx.setLineDash([2, 2.4]);
    ctx.beginPath();
    for (const n of list) if (n._r >= RING_MIN_PX) { const rr = n._r + Math.max(2, n._r * 0.45); ctx.moveTo(n._sx + rr, n._sy); ctx.arc(n._sx, n._sy, rr, 0, Math.PI * 2); }
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.globalAlpha = alpha;
    ctx.fillStyle = fill;
    ctx.beginPath();
    for (const n of list) { ctx.moveTo(n._sx + n._r, n._sy); ctx.arc(n._sx, n._sy, n._r, 0, Math.PI * 2); }
    ctx.fill();
  }
  if (hollow.length) {
    ctx.setLineDash([2, 2]);
    for (const n of hollow) {
      ctx.globalAlpha = n._a * n._p;
      ctx.beginPath();
      ctx.arc(n._sx, n._sy, n._r, 0, Math.PI * 2);
      ctx.fillStyle = FONTS.bg; ctx.fill();
      ctx.lineWidth = Math.max(0.75, Math.min(1.5, n._r * 0.25)); ctx.strokeStyle = FONTS.unwritten; ctx.stroke();
    }
    ctx.setLineDash([]);
  }
  // Outline the open and hovered notes.
  for (const n of marked) {
    ctx.globalAlpha = n._a * n._p;
    ctx.lineWidth = 1.5; ctx.strokeStyle = FONTS.slate;
    ctx.beginPath(); ctx.arc(n._sx, n._sy, n._r + 0.75, 0, Math.PI * 2); ctx.stroke();
  }
  ctx.fillStyle = FONTS.ink;
  for (const l of titles) {
    if (l.font !== font) ctx.font = font = l.font;
    ctx.globalAlpha = l.la;
    ctx.fillText(l.text, l.n._sx, l.g.y);
  }
  ctx.globalAlpha = 1;
  if (fading) requestDraw();
}

// Title visibility: fade with log2(zoom). Me, hubs, landmarks and the focused neighbourhood always show;
// maps of ≤40 notes show everything.
function labelAlpha(n, k) {
  if (focusId && focusKeep.has(n.id)) return 1;
  if (labelHidden.has(n.id)) return 0;
  if (landmarks.has(n.id)) {
    // Landmarks keep a minimum size but still fade once far too small to read.
    const natural = (14 + n._size / 4) * Math.sqrt(obsZoom(k)) / Math.min(dpr, LABEL_DPR_CAP);
    return Math.max(0, Math.min(1, (natural - READABLE_PX / 2) / 1.5));
  }
  const fade = realCount <= 40 ? 1 : titleFade(k);
  const px = labelGeom(n, k).px;
  return Math.min(fade, Math.max(0, Math.min(1, (px - READABLE_PX) / 1.5)));
}
function labelGeom(n, k) {
  const root = Math.sqrt(obsZoom(k));
  let px = (14 + n._size / 4) * root / Math.min(dpr, LABEL_DPR_CAP);
  if (n.id === "me") px = Math.max(px * 1.4, ME_PX);
  else if (focusId && focusKeep.has(n.id)) px = Math.max(px, FOCUS_PX);
  else if (landmarks.has(n.id)) px = Math.max(px, LANDMARK_PX);
  const r = n._r ?? dotPx(n, k);
  return { px, y: n._sy + r + Math.max(3, 5 * root / dpr) + (n.id === "me" ? 2 : 0) };
}
function labelWidth(n, px) {
  if (!n._tw) {
    ctx.font = `${n.id === "me" ? "400" : "500"} 100px ${n.id === "me" ? FONTS?.serif || "serif" : FONTS?.sans || "sans-serif"}`;
    n._tw = ctx.measureText(displayTitle(n)).width;
  }
  return n._tw * px / 100;
}

// Always-visible titles on large maps (still subject to overlap culling).
const LANDMARKS = 14;
let landmarks = new Set(), realCount = 0;
function pickLandmarks() {
  const real = nodes.filter(n => !n.unresolved);
  realCount = real.length;
  const folders = real.filter(n => { const p = n.id.split("/"); return p.length === 2 && p[0] === p[1]; });
  const top = real.filter(n => n.id !== "me").sort((a, b) => b.degree - a.degree).slice(0, LANDMARKS);
  landmarks = new Set(["me", ...folders.map(n => n.id), ...top.map(n => n.id)]);
}

// Overlap culling: when two titles collide the less-linked one is hidden. Runs only at rest or on zoom,
// using a screen-space bucket grid.
let labelHidden = new Set();
function cullLabels() {
  labelHidden = new Set();
  if (!mapW) return;
  const t = transform, k = t.k;
  for (const n of nodes) {   // fresh screen positions
    if (n.x === undefined) continue;
    n._sx = t.x + k * n.x; n._sy = t.y + k * n.y; n._r = dotPx(n, k);
    n._on = n._sx > -40 && n._sx < mapW + 40 && n._sy > -40 && n._sy < mapH + 40;
  }
  const order = nodes.filter(n => !n.unresolved && n._on && labelAlpha(n, k) * n._a > 0.05)
    .sort((a, b) => (b.id === "me") - (a.id === "me") || landmarks.has(b.id) - landmarks.has(a.id)
      || b.degree - a.degree || displayTitle(a).length - displayTitle(b).length);
  const CELL = 80, grid = new Map();
  for (const n of order) {
    const g = labelGeom(n, k), w = labelWidth(n, g.px);
    const pad = 3 + g.px * 0.25;   // include the halo
    const r = { l: n._sx - w / 2 - pad, r: n._sx + w / 2 + pad, t: g.y - 1, b: g.y + g.px * 1.2 + 1 };
    const cells = [];
    for (let cx = Math.floor(r.l / CELL); cx <= Math.floor(r.r / CELL); cx++)
      for (let cy = Math.floor(r.t / CELL); cy <= Math.floor(r.b / CELL); cy++) cells.push(cx + "," + cy);
    const hit = cells.some(c => (grid.get(c) || []).some(q => r.l < q.r && q.l < r.r && r.t < q.b && q.t < r.b));
    if (hit) { labelHidden.add(n.id); continue; }
    for (const c of cells) { if (!grid.has(c)) grid.set(c, []); grid.get(c).push(r); }
  }
  requestDraw();
}
// Throttle culling to CULL_MS while zooming.
const CULL_MS = 100;
let cullQueued = false, lastCull = 0;
function queueCull() {
  if (cullQueued) return;
  cullQueued = true;
  const wait = Math.max(0, CULL_MS - (performance.now() - lastCull));
  setTimeout(() => requestAnimationFrame(() => { cullQueued = false; lastCull = performance.now(); cullLabels(); }), wait);
}

function focus(id) {
  focusId = id;
  focusKeep = id ? new Set([id, ...(neighbors.get(id) || [])]) : null;
  requestDraw();
}
function showTip(event, n) {
  const count = neighbors.get(n.id).size;
  tip.innerHTML = `<b>${escapeHtml(displayTitle(n))}</b><span>${n.unresolved ? "Not written yet"
    : `Linked to ${count} note${count === 1 ? "" : "s"}`}</span>`;
  moveTip(event);
  tip.classList.add("show");
}
function moveTip(event) { tip.style.left = event.clientX + 14 + "px"; tip.style.top = event.clientY + 16 + "px"; }

// Hit test in screen points; touch targets are ~44pt.
function nodeAt(x, y, finger) {
  const reach = finger ? 22 : 12, k = transform.k;
  let best = null, bestD = Infinity;
  for (const n of nodes) {
    if (n.x === undefined || isGone(n)) continue;
    const sx = transform.x + k * n.x, sy = transform.y + k * n.y;
    const d = Math.hypot(sx - x, sy - y);
    // Cap the touch radius at 4× the dot so zoomed-out taps on empty space still pan.
    const r = dotPx(n, k);
    if (d <= Math.max(r + 2, Math.min(reach, r * 4)) && d < bestD) { best = n; bestD = d; }
  }
  return best;
}
const fingerEvent = e => !!(e && (e.pointerType === "touch" || e.type?.startsWith("touch"))) || phone.matches;

// Mouse hover highlights the node and its neighbours.
let hovered = null;
canvas.addEventListener("pointermove", e => {
  if (e.pointerType !== "mouse" || dragging || e.buttons) return;
  const box = canvas.getBoundingClientRect();
  const n = nodeAt(e.clientX - box.left, e.clientY - box.top, false);
  if (n !== hovered) {
    hovered = n;
    canvas.style.cursor = n ? "pointer" : "";
    focus(n ? n.id : null);
    if (n) showTip(e, n); else tip.classList.remove("show");
  } else if (n) moveTip(e);
});
canvas.addEventListener("pointerleave", () => {
  if (!hovered || dragging) return;
  hovered = null; canvas.style.cursor = ""; focus(null); tip.classList.remove("show");
});

// Drag: the node follows the pointer while the sim is warmed. A tap (no movement) opens the note.
// Presses that miss every node fall through to d3.zoom for panning.
let dragDistance = 0, heated = false, dragging = false;
// A second touch cancels the drag so the pinch goes to zoom.
let grabbed = null, dragCancelled = false;
const drag = d3.drag()
  .container(canvas)
  .filter(event => !event.ctrlKey && !event.button && !(event.touches && event.touches.length > 1))
  .subject(event => {
    const n = nodeAt(event.x, event.y, fingerEvent(event.sourceEvent));
    return n ? { n, x: event.x, y: event.y, dx: n.x - transform.invertX(event.x), dy: n.y - transform.invertY(event.y) } : null;
  })
  .on("start", event => {
    const n = event.subject.n;
    dragDistance = 0;
    heated = false;
    dragCancelled = false;
    grabbed = n;
    n.fx = n.x; n.fy = n.y;
  })
  .on("drag", event => {
    if (dragCancelled) return;
    const { n, dx, dy } = event.subject;
    dragDistance += Math.abs(event.dx) + Math.abs(event.dy);
    if (!heated && dragDistance >= 4) {   // past the tap threshold
      heated = true;
      dragging = true;
      tip.classList.remove("show");
      focus(n.id);
      simulation.alphaTarget(PHYS.dragHeat).alpha(Math.max(simulation.alpha(), PHYS.grabHeat)).restart();
    }
    n.fx = transform.invertX(event.x) + dx; n.fy = transform.invertY(event.y) + dy;
  })
  .on("end", event => {
    const n = event.subject.n;
    grabbed = null;
    if (dragCancelled) { dragCancelled = false; return; }
    n.fx = null;
    n.fy = null;
    if (dragging) { dragging = false; focus(hovered ? hovered.id : null); }
    if (!heated) {
      if (!n.unresolved) { tip.classList.remove("show"); openNote(n.id, { newTab: true }); }
      return;
    }
    simulation.alphaTarget(0).alpha(Math.max(simulation.alpha(), PHYS.releaseHeat)).restart();
  });

// Zoom range 0.001×–5×. A high floor would make the first touch after fit() snap back.
const ZOOM_MIN = 0.001, ZOOM_MAX = 5;
const zoom = d3.zoom().scaleExtent([ZOOM_MIN, ZOOM_MAX]).on("zoom", event => {
  if (event.sourceEvent) userMovedView = true;   // user gesture, not fit()
  transform = event.transform;
  currentZoom = transform.k;
  requestDraw();
  if (simulation.alpha() < simulation.alphaMin()) queueCull();
});
mapSel.call(drag).call(zoom).on("dblclick.zoom", null);   // drag before zoom so pressing a dot never pans
canvas.addEventListener("touchstart", event => {
  if (event.touches.length < 2 || !grabbed) return;
  grabbed.fx = grabbed.fy = null;
  dragCancelled = true;
  if (dragging) { dragging = false; focus(null); }
  if (heated) simulation.alphaTarget(0);
}, true);

// Accessible fallback: a hidden list of notes inside the canvas.
let mapListKey = "";
function renderMapList() {
  const real = nodes.filter(n => !n.unresolved);
  const key = real.map(n => n.id + "\u0000" + displayTitle(n)).join("\u0001");
  canvas.setAttribute("aria-label", `Map of your Thread memory: ${real.length} notes, ${edges.length} links`);
  if (key === mapListKey) return;
  mapListKey = key;
  const list = document.createElement("ul");
  for (const n of [...real].sort((a, b) => displayTitle(a).localeCompare(displayTitle(b)))) {
    const li = document.createElement("li"), b = document.createElement("button");
    b.type = "button"; b.dataset.id = n.id; b.textContent = displayTitle(n);
    li.append(b); list.append(li);
  }
  canvas.replaceChildren(list);
}
canvas.addEventListener("click", e => { const b = e.target.closest?.("button[data-id]"); if (b) openNote(b.dataset.id, { newTab: true }); });

// Any node outside the viewport (with margin)?
function offScreen() {
  const box = $("map").getBoundingClientRect();
  const t = transform;
  return nodes.some(n => {
    const [x, y] = t.apply([n.x, n.y]);
    return x < 30 || y < 30 || x > box.width - 30 || y > box.height - 30;
  });
}
function fit(duration = 650) {
  const shown = localKeep ? nodes.filter(n => localKeep.has(n.id)) : nodes;
  const box = $("map").getBoundingClientRect();
  if (!shown.length || !box.width) return;
  const half = n => Math.max(10, displayTitle(n).length * 3.1);   // approx. half label width
  const x0 = Math.min(...shown.map(n => n.x - half(n))), x1 = Math.max(...shown.map(n => n.x + half(n)));
  const y0 = Math.min(...shown.map(n => n.y - 10)), y1 = Math.max(...shown.map(n => n.y + 32));
  const pad = 64;
  const k = Math.max(ZOOM_MIN, Math.min(1.8, (box.width - pad * 2) / Math.max(x1 - x0, 1), (box.height - pad * 2) / Math.max(y1 - y0, 1)));
  const target = d3.zoomIdentity.translate(box.width / 2, box.height / 2).scale(k).translate(-(x0 + x1) / 2, -(y0 + y1) / 2);
  if (duration === 0) mapSel.call(zoom.transform, target);
  else mapSel.transition().duration(duration).ease(EASE).call(zoom.transform, target);
}

// Local graph: one note and its neighbours up to N hops.
let localRoot = null, localKeep = null;
function applyLocal(refit = true) {
  const bar = $("localbar");
  if (!localRoot) {
    localKeep = null;
    requestDraw();
    bar.classList.add("off");
    return;
  }
  const depth = +$("depth").value;
  localKeep = new Set([localRoot]);
  let frontier = [localRoot];
  for (let d = 0; d < depth; d++) {
    frontier = frontier.flatMap(id => [...(neighbors.get(id) || [])]).filter(id => !localKeep.has(id));
    frontier.forEach(id => localKeep.add(id));
  }
  requestDraw();
  $("local-title").textContent = titleOf(localRoot);
  bar.classList.remove("off");
  if (refit) fit();
  if (refit) recordNav();
}
$("depth").oninput = () => applyLocal();
$("exit-local").onclick = () => { localRoot = null; applyLocal(); fit(); recordNav(); };

// Replay: reveal notes in creation order.
$("replay").onclick = () => {
  activate("map");
  // BFS from Me, folders before their notes, oldest first within a ring.
  const ring = new Map([["me", 0]]);
  const queue = ["me"];
  while (queue.length) {
    const id = queue.shift();
    for (const next of neighbors.get(id) || []) if (!ring.has(next)) { ring.set(next, ring.get(id) + 1); queue.push(next); }
  }
  const order = [...nodes].sort((a, b) => (ring.get(a.id) ?? 99) - (ring.get(b.id) ?? 99)
    || (a.date || "9999").localeCompare(b.date || "9999") || a.id.localeCompare(b.id));
  const shown = new Set();
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const step = reduce ? 0 : Math.max(70, 3200 / order.length);
  replayShown = shown;
  requestDraw();
  order.forEach((n, i) => setTimeout(() => {
    shown.add(n.id);
    if (i === order.length - 1) replayShown = null;
    requestDraw();
  }, i * step + 120));
};
$("fit").onclick = () => { activate("map"); userMovedView = false; fit(); };
$("recenter").onclick = () => { userMovedView = false; fit(); };

// Size the app to the visual viewport (iOS Safari toolbars and keyboard). Freeze the height while typing.
function typing() { return document.activeElement?.matches("input, textarea, select, [contenteditable]"); }
function fitToScreen() {
  if (typing()) return;
  const h = Math.round(window.visualViewport ? window.visualViewport.height : innerHeight);
  document.documentElement.style.setProperty("--app-h", h + "px");
  if (window.scrollY || document.scrollingElement.scrollTop) window.scrollTo(0, 0);
}
fitToScreen();
window.visualViewport?.addEventListener("resize", fitToScreen);
window.addEventListener("resize", fitToScreen);
window.addEventListener("orientationchange", () => setTimeout(fitToScreen, 300));
document.addEventListener("focusout", () => setTimeout(fitToScreen, 60));   // keyboard closed
// ?debug=1 shows layout numbers.
if (new URLSearchParams(location.search).has("debug")) {
  const box = document.createElement("pre");
  box.style.cssText = "position:fixed;z-index:99;left:8px;top:60px;margin:0;padding:8px 10px;font:11px/1.4 ui-monospace,monospace;background:rgba(0,0,0,.78);color:#fff;border-radius:8px;pointer-events:none";
  document.body.appendChild(box);
  const show = () => {
    const vv = window.visualViewport, r = $("ribbon").getBoundingClientRect();
    box.textContent = `inner ${innerWidth}×${innerHeight}\nvisual ${vv ? Math.round(vv.width) + "×" + Math.round(vv.height) + " top " + Math.round(vv.offsetTop) : "n/a"}\n--app-h ${getComputedStyle(document.documentElement).getPropertyValue("--app-h")}\nscrollY ${Math.round(scrollY)}\nbar top ${Math.round(r.top)} bottom ${Math.round(r.bottom)}\nscreen ${screen.width}×${screen.height}`;
  };
  setInterval(show, 300);
}

// =====================================================================
// Sidebar: folder tree, creating folders and notes
// =====================================================================
let tree = null;
const expanded = new Set(store.get("expanded", ["classes"]));
let selectedFolder = "";   // target folder for new items
let pending = null;   // { kind, parent } while naming a new item

const ICONS = {
  chev: '<svg viewBox="0 0 10 10"><path d="M3.5 2l3 3-3 3"/></svg>',
  newNote: '<svg viewBox="0 0 16 16"><path d="M9.5 2.5H4.5a1 1 0 0 0-1 1v9a1 1 0 0 0 1 1h7a1 1 0 0 0 1-1V5.5z"/><path d="M9.5 2.5v3h3M8 8v3.5M6.25 9.75h3.5"/></svg>',
  newFolder: '<svg viewBox="0 0 16 16"><path d="M2.5 4.5a1 1 0 0 1 1-1h3l1.5 1.5h4.5a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1h-9a1 1 0 0 1-1-1z"/><path d="M8 7.25v3.5M6.25 9h3.5"/></svg>',
  close: '<svg viewBox="0 0 12 12"><path d="M3 3l6 6M9 3l-6 6"/></svg>',
  map: '<svg viewBox="0 0 12 12"><circle cx="3" cy="3" r="1.4"/><circle cx="9" cy="4" r="1.4"/><circle cx="5" cy="9" r="1.4"/><path d="M4.2 3.4l3.6.4M8.3 5.2L5.8 8M3.4 4.3l1.2 3.4"/></svg>',
};

async function loadTree() {
  tree = await (await fetch("/api/tree")).json();
  renderTree();
}

function matchesSearch(item) {
  if (!searchTerm) return true;
  const q = searchTerm.toLowerCase();
  if (item.type === "note") return item.name.toLowerCase().includes(q);
  return item.name.toLowerCase().includes(q) || item.children.some(matchesSearch);
}

function renderTree() {
  if (!tree) return;
  const box = $("tree");
  const scroll = box.scrollTop;
  box.innerHTML = "";
  box.append(...renderItems(tree.children, ""));
  if (pending && pending.parent === "") box.prepend(newRow());
  if (searchTerm && !box.querySelector(".row")) box.innerHTML = `<div class="tree-empty">No notes match "${escapeHtml(searchTerm)}"</div>`;
  box.scrollTop = scroll;
  const input = box.querySelector(".new-row input, input.rename");
  if (input) { input.focus(); input.select(); }
}

function renderItems(items, parentPath) {
  const out = [];
  for (const item of items.filter(matchesSearch)) {
    if (item.type === "folder") {
      const open = searchTerm ? true : expanded.has(item.path);
      const row = document.createElement("div");
      row.className = "row folder" + (open ? " open" : "") + (selectedFolder === item.path ? " selected-folder" : "");
      row.style.paddingLeft = "2px";
      row.innerHTML = `<span class="chev">${ICONS.chev}</span><span class="name">${escapeHtml(item.name)}</span>
        <span class="row-actions">
          <button class="icon" data-act="note" title="New note in ${escapeHtml(item.name)}" aria-label="New note">${ICONS.newNote}</button>
          <button class="icon" data-act="folder" title="New folder in ${escapeHtml(item.name)}" aria-label="New folder">${ICONS.newFolder}</button>
        </span>`;
      row.onclick = e => {
        if (e.target.closest("input")) return;
        const act = e.target.closest("[data-act]")?.dataset.act;
        if (act) return startCreate(act, item.path);
        toggleFolder(item.path);
      };
      row.ondblclick = e => { if (!e.target.closest("[data-act]")) openNote(item.note); };
      row.oncontextmenu = e => showMenu(e, [
        ["New note", () => startCreate("note", item.path)],
        ["New folder", () => startCreate("folder", item.path)],
        null,
        ["Open folder note", () => openNote(item.note)],
        ["Show on map", () => showOnMap(item.note)],
        null,
        ["Rename", () => startRename({ kind: "folder", key: item.path, name: item.name })],
        ["Delete", () => remove({ kind: "folder", key: item.path, name: item.name }), "danger"],
      ]);
      if (renaming?.key === item.path) row.querySelector(".name").replaceWith(renameInput());
      const kids = document.createElement("div");
      kids.className = "children" + (open ? "" : " closed");
      const inner = document.createElement("div");
      if (pending && pending.parent === item.path) inner.append(newRow());
      inner.append(...renderItems(item.children, item.path));
      kids.append(inner);
      out.push(row, kids);
    } else {
      const row = document.createElement("div");
      row.className = "row note" + (item.id === active ? " active" : "");
      row.dataset.id = item.id;
      row.innerHTML = `<span class="dot" style="background:${colorOfId(item.id)}"></span><span class="name">${escapeHtml(item.name)}</span>`;
      if (renaming?.key === item.id) row.querySelector(".name").replaceWith(renameInput());
      row.onclick = e => {
        if (e.target.closest("input")) return;
        openNote(item.id, { newTab: e.metaKey || e.ctrlKey });
        closeSidebarOnPhone();
      };
      row.onauxclick = e => { if (e.button === 1) openNote(item.id, { newTab: true, background: true }); };
      row.oncontextmenu = e => showMenu(e, [
        ["Open in new tab", () => openNote(item.id, { newTab: true })],
        ["Show on map", () => showOnMap(item.id)],
        ...(item.id === "me" ? [] : [
          null,
          ["Rename", () => startRename({ kind: "note", key: item.id, name: item.name })],
          ["Delete", () => remove({ kind: "note", key: item.id, name: item.name }), "danger"],
        ]),
      ]);
      out.push(row);
    }
  }
  return out;
}

// Inline rename
let renaming = null;
function startRename(target) {
  renaming = target;
  renderTree();
}
function renameInput() {
  const input = document.createElement("input");
  input.className = "rename";
  input.value = renaming.name;
  input.setAttribute("aria-label", "New name");
  let done = false;
  const finish = async save => {
    if (done) return;
    done = true;
    const target = renaming, name = input.value.trim();
    renaming = null;
    if (!save || !name || name === target.name) return renderTree();
    try {
      const body = target.kind === "folder" ? { kind: "folder", path: target.key, name } : { kind: "note", id: target.key, name };
      const result = await send("POST", "/api/rename", body);
      const newKey = result.path || result.id;
      // Move tabs, history and expanded folders to the new path.
      const remap = id => id === target.key ? newKey
        : target.kind === "folder" && id.startsWith(target.key + "/") ? newKey + id.slice(target.key.length)
        : id;
      if (target.kind === "folder") {
        const oldHub = `${target.key}/${target.key.split("/").pop()}`;
        const newHub = `${newKey}/${newKey.split("/").pop()}`;
        const fixHub = id => id === remap(oldHub) ? newHub : id;
        tabs = tabs.map(t => fixHub(remap(t)));
        active = fixHub(remap(active));
      } else {
        tabs = tabs.map(remap);
        active = remap(active);
      }
      for (const p of [...expanded]) { expanded.delete(p); expanded.add(remap(p)); }
      store.set("expanded", [...expanded]);
      selectedFolder = remap(selectedFolder);
      lastJson = "";
      await refresh();
      if (active !== "map") await activate(active);
    } catch (err) {
      renderTree();
      flash(err.message);
    }
  };
  input.onkeydown = e => {
    e.stopPropagation();
    if (e.key === "Enter") finish(true);
    if (e.key === "Escape") finish(false);
  };
  input.onblur = () => finish(true);
  return input;
}

// Delete to trash, with undo
async function remove(target) {
  try {
    const result = await send("POST", "/api/delete", { target: target.kind === "folder" ? target.key + "/" : target.key });
    const gone = id => id === target.key || (target.kind === "folder" && id.startsWith(target.key + "/"));
    const wasActive = gone(active);
    tabs = tabs.filter(t => !gone(t));
    lastJson = "";
    await refresh();
    if (wasActive) await activate(tabs[tabs.length - 1] || "map");
    else renderTabs();
    const what = target.kind === "folder" ? `folder "${target.name}" (${result.notes} note${result.notes === 1 ? "" : "s"})` : `"${target.name}"`;
    toast(`Deleted ${what}`, "Undo", async () => {
      await send("POST", "/api/restore", { trash: result.trash });
      lastJson = "";
      await refresh();
      if (target.kind === "note") openNote(target.key, { newTab: true });
    });
  } catch (err) {
    flash(err.message);
  }
}
function toast(message, actionLabel, action) {
  const el = $("toast");
  el.innerHTML = `<span>${escapeHtml(message)}</span>`;
  if (actionLabel) {
    const b = document.createElement("button");
    b.textContent = actionLabel;
    b.onclick = async () => { el.classList.remove("show"); await action(); };
    el.append(b);
  }
  el.classList.add("show");
  el.onclick = e => { if (e.target === el || e.target.tagName === "SPAN") el.classList.remove("show"); };
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("show"), 8000);
}

function toggleFolder(path) {
  selectedFolder = path;
  expanded.has(path) ? expanded.delete(path) : expanded.add(path);
  store.set("expanded", [...expanded]);
  renderTree();
}

// New note/folder: inline name field.
function findFolder(path, node = tree) {
  if (!node) return null;
  if (node.path === path) return node;
  for (const child of node.children || []) {
    const found = child.type === "folder" && findFolder(path, child);
    if (found) return found;
  }
  return null;
}
const childrenOf = path => findFolder(path)?.children || [];
function startCreate(kind, parent) {
  const taken = new Set(childrenOf(parent).map(c => c.name.toLowerCase()));
  let name = "Untitled", i = 1;
  while (taken.has(name.toLowerCase())) name = `Untitled ${i++}`;
  pending = { kind, parent, name };
  if (parent) expanded.add(parent);
  if (!document.body.classList.contains("sidebar-open")) toggleSidebar(true);
  renderTree();
}
function newRow() {
  const row = document.createElement("div");
  row.className = "row new-row";
  row.innerHTML = `<span class="chev"></span><input placeholder="${pending.kind === "note" ? "Note name" : "Folder name"}" aria-label="Name">`;
  const input = row.querySelector("input");
  input.value = pending.name;
  let done = false;
  const finish = async save => {
    if (done) return;
    done = true;
    const name = input.value.trim();
    const { kind, parent } = pending;
    pending = null;
    if (!save || !name) return renderTree();
    try {
      if (kind === "folder") {
        const { path } = await send("POST", "/api/folder", { parent, name });
        expanded.add(path); selectedFolder = path;
        store.set("expanded", [...expanded]);
        await refresh();
      } else {
        const { id } = await send("POST", "/api/note", { folder: parent, title: name });
        await refresh();
        closeSidebarOnPhone();
        openNote(id, { newTab: true, edit: true });
      }
    } catch (err) {
      renderTree();
      flash(err.message);
    }
  };
  input.onkeydown = e => {
    if (e.key === "Enter") finish(true);
    if (e.key === "Escape") finish(false);
  };
  input.onblur = () => finish(true);
  return row;
}
// Sidebar errors show as a toast.
function flash(message) {
  toast(message);
}

$("new-note").onclick = () => startCreate("note", selectedFolder);
// Navigation shared by the phone tab bar and the desktop top bar.
const PLACES = ["home", "map", "me"];   // places, not notes
function goNav(where) {
  if (where === "home") return activate("home");
  if (where === "map") return activate("map");
  if (where === "me") return openMe("profile");
  if (where === "search") {
    if (!document.body.classList.contains("sidebar-open")) toggleSidebar(true);
    searchBox.focus();
    searchBox.select();
  }
}
document.addEventListener("click", e => { const b = e.target.closest("[data-go]"); if (b) goNav(b.dataset.go); });
$("fab-new").onclick = () => startCreate("note", selectedFolder);
const online = !["127.0.0.1", "localhost", "[::1]"].includes(location.hostname);

// On phones the sidebar is a slide-over that closes on navigation.
const phone = matchMedia("(max-width: 700px)");
if (phone.matches) document.body.classList.remove("sidebar-open");
function closeSidebarOnPhone() { if (phone.matches) document.body.classList.remove("sidebar-open"); }
$("map").addEventListener("pointerdown", closeSidebarOnPhone);
$("ribbon").addEventListener("click", e => { if (!e.target.closest("[data-go=search]")) closeSidebarOnPhone(); });
$("close-sidebar").onclick = () => toggleSidebar(false);
document.querySelector("main").addEventListener("pointerdown", e => {
  if (phone.matches && document.body.classList.contains("sidebar-open") && !e.target.closest("#toggle-sidebar")) closeSidebarOnPhone();
});
function markRibbon() {
  for (const b of document.querySelectorAll("[data-go]")) {
    const on = b.dataset.go === active;
    b.classList.toggle("on", on);
    if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  }
  // The + button only shows on the map.
  $("fab-new").hidden = active !== "map";
}
$("new-folder").onclick = () => startCreate("folder", selectedFolder);
$("collapse-all").onclick = () => { expanded.clear(); store.set("expanded", []); renderTree(); };
function toggleSidebar(force) {
  if (force === undefined) store.set("sidebar-chosen", true);   // user chose; stop auto-collapsing
  const before = $("map").getBoundingClientRect().width;
  document.body.classList.toggle("sidebar-open", force);
  const after = document.body.classList.contains("sidebar-open") ? before - 264 : before + 264;   // sidebar width
  if (active !== "map" || after === before) return;
  // Pan the map with the sidebar so the centre stays put.
  const t = transform;
  mapSel.transition().duration(320).ease(EASE)
    .call(zoom.transform, d3.zoomIdentity.translate(t.x + (after - before) / 2, t.y).scale(t.k));
}
$("toggle-sidebar").onclick = () => toggleSidebar();

// Context menu
function showMenu(event, items) {
  event.preventDefault();
  document.querySelectorAll(".row.menu-open").forEach(r => r.classList.remove("menu-open"));
  event.currentTarget?.classList?.add("menu-open");
  const menu = $("menu");
  menu.innerHTML = "";
  for (const item of items) {
    if (!item) { menu.append(document.createElement("hr")); continue; }
    const b = document.createElement("button");
    b.textContent = item[0];
    if (item[2]) b.className = item[2];
    b.onclick = () => { hideMenu(); item[1](); };
    menu.append(b);
  }
  menu.hidden = false;
  menu.style.left = Math.min(event.clientX, innerWidth - 200) + "px";
  menu.style.top = Math.min(event.clientY, innerHeight - menu.offsetHeight - 8) + "px";
}
function hideMenu() {
  $("menu").hidden = true;
  document.querySelectorAll(".row.menu-open").forEach(r => r.classList.remove("menu-open"));
}
addEventListener("click", e => { if (!e.target.closest("#menu")) hideMenu(); });

// =====================================================================
// Tabs + note view
// =====================================================================
let tabs = store.get("tabs", ["map"]).filter(t => typeof t === "string");
if (!tabs.includes("map")) tabs.unshift("map");
let active = "home", lastNote = null, editing = false, dirty = false;

function renderTabs() {
  const box = $("tabs");
  box.innerHTML = "";
  // Note tabs only appear once a second note is open.
  const noteTabs = tabs.filter(t => !PLACES.includes(t));
  box.hidden = noteTabs.length < 2;
  for (const id of noteTabs) {
    const tab = document.createElement("div");
    tab.className = "tab" + (id === active ? " active" : "") + (id === "map" ? " map-tab" : "");
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-selected", id === active);
    tab.title = id === "map" ? "Map" : titleOf(id);
    tab.innerHTML = id === "map" ? `${ICONS.map}<span class="label">Map</span>`
      : `<span class="label">${escapeHtml(titleOf(id))}</span><span class="close" title="Close tab">${ICONS.close}</span>`;
    tab.onclick = e => e.target.closest(".close") ? closeTab(id) : activate(id);
    tab.onauxclick = e => { if (e.button === 1 && id !== "map") closeTab(id); };
    box.append(tab);
  }
  box.querySelector(".tab.active")?.scrollIntoView({ block: "nearest", inline: "nearest" });
  store.set("tabs", tabs);
}

// Open a note: reuse its tab, open a new tab from the map or with Cmd/Ctrl, else replace the current tab.
async function openNote(id, { newTab = false, background = false, edit = false } = {}) {
  if (!tabs.includes(id)) {
    const at = tabs.indexOf(active);
    if (newTab || PLACES.includes(active)) tabs.splice(at + 1, 0, id);
    else { await saveIfDirty(); tabs[at] = id; }
  }
  if (background) return renderTabs();
  await activate(id, { edit });
}
// History: every navigation is a browser history entry, so back/forward gestures all work.
let navIndex = 0, navMax = 0, restoring = false, navQueued = false;

function here() {
  return { tab: active, local: active === "map" && localRoot ? { root: localRoot, depth: +$("depth").value } : null };
}
const sameLoc = (a, b) => a && b && a.tab === b.tab && JSON.stringify(a.local) === JSON.stringify(b.local);

// Debounce so compound actions record a single entry.
function recordNav() {
  if (restoring || navQueued) return;
  navQueued = true;
  queueMicrotask(() => setTimeout(() => {
    navQueued = false;
    const loc = here();
    if (sameLoc(window.history.state?.loc, loc)) return;
    navIndex += 1;
    navMax = navIndex;
    window.history.pushState({ loc, i: navIndex }, "", "#" + encodeURIComponent(loc.tab));
    sessionStorage.setItem("thread.navMax", navMax);
    updateNav();
  }, 0));
}

async function goTo(loc, dir) {
  if (!PLACES.includes(loc.tab) && !byId.has(loc.tab)) {   // deleted since; skip
    return window.history.go(dir);
  }
  restoring = true;
  try {
    if (loc.tab === "home") { await activate("home"); return; }
    if (loc.tab !== "map" && !tabs.includes(loc.tab)) {
      // Reopen a closed note in this tab (or a new one from the map).
      const at = tabs.indexOf(active);
      if (PLACES.includes(active)) tabs.splice(at + 1, 0, loc.tab);
      else { await saveIfDirty(); tabs[at] = loc.tab; }
    }
    if (loc.tab === "map") {
      localRoot = loc.local?.root && byId.has(loc.local.root) ? loc.local.root : null;
      if (loc.local) $("depth").value = loc.local.depth;
      await activate("map");
      applyLocal();
      if (!localRoot) fit();
    } else {
      await activate(loc.tab);
    }
  } finally {
    restoring = false;
  }
}
addEventListener("popstate", e => {
  if (!e.state?.loc) return;
  const dir = e.state.i < navIndex ? -1 : 1;
  navIndex = e.state.i;
  updateNav();
  goTo(e.state.loc, dir);
});
function updateNav() {
  $("back").disabled = navIndex <= 0;
  $("forward").disabled = navIndex >= navMax;
}
$("back").onclick = () => { if (navIndex > 0) window.history.back(); };
$("forward").onclick = () => { if (navIndex < navMax) window.history.forward(); };
// Mouse buttons 3/4 = back/forward.
addEventListener("mouseup", e => {
  if (e.button === 3) { e.preventDefault(); $("back").click(); }
  if (e.button === 4) { e.preventDefault(); $("forward").click(); }
});

async function closeTab(id) {
  const at = tabs.indexOf(id);
  if (id === active) await saveIfDirty();
  tabs = tabs.filter(t => t !== id);
  if (id === active) await activate(tabs[Math.max(0, at - 1)]);
  else renderTabs();
}

async function activate(id, { edit = false } = {}) {
  if (active !== id) await saveIfDirty();
  active = id;
  renderTabs();
  const isMap = id === "map", isMe = id === "me", isHome = id === "home";
  $("home-view").hidden = !isHome;
  $("map-view").hidden = !isMap;
  $("note-view").hidden = isMap || isMe || isHome;
  $("me-view").hidden = !isMe;
  document.body.dataset.view = isHome ? "home" : isMap ? "map" : isMe ? "me" : "note";
  if (isHome) {
    renderHome();
  } else if (isMap) {
    if (needsFit) { needsFit = false; fit(0); }
  } else if (isMe) {
    lastNote = id;
    await showMe();
  } else {
    lastNote = id;
    selectedFolder = id.includes("/") ? id.split("/").slice(0, -1).join("/") : "";
    // Expand every ancestor folder in the sidebar.
    const parts = id.split("/");
    for (let i = 1; i < parts.length; i++) expanded.add(parts.slice(0, i).join("/"));
    store.set("expanded", [...expanded]);
    await showNote(id, edit);
  }
  markActiveNode();
  markRibbon();
  renderTree();
  $("tree").querySelector(".row.active")?.scrollIntoView({ block: "nearest" });
  recordNav();
}

let currentText = "";
async function showNote(id, edit = false) {
  const res = await fetch("/api/note?id=" + encodeURIComponent(id));
  if (!res.ok) { $("note-body").innerHTML = `<p class="missing">This note doesn't exist anymore.</p>`; return; }
  const note = await res.json();
  currentText = note.text;
  const parts = id.split("/");
  const isFolderNote = parts.length > 1 && parts.at(-1) === parts.at(-2);
  const folders = parts.slice(0, isFolderNote ? -2 : -1);
  $("note-path").innerHTML = folders.map((part, i) => {
    const hub = parts.slice(0, i + 1).join("/") + "/" + part;
    return `<a class="crumb" data-id="${escapeHtml(hub)}">${escapeHtml(titleOf(hub))}</a><span class="sep">/</span>`;
  }).join("") + `<span class="crumb current">${escapeHtml(titleOf(id))}</span>`;
  $("note-status").textContent = "";
  const date = (note.text.match(/^date:\s*(.+)$/m) || [])[1];
  $("note-body").innerHTML = (date ? `<div class="meta">${escapeHtml(date)}</div>` : "") + renderMarkdown(note.text, note.links);
  const backlinks = edges.filter(e => (e.target.id || e.target) === id).map(e => e.source.id || e.source);
  $("backlinks").innerHTML = backlinks.length ? `<div class="backlinks"><h3>Linked from</h3><ul>${backlinks.map(b =>
    `<li><a class="link" data-id="${escapeHtml(b)}" style="--c:${colorOfId(b)}">${escapeHtml(titleOf(b))}</a></li>`).join("")}</ul></div>` : "";
  // restart the fade-in
  const article = $("note");
  article.style.animation = "none"; void article.offsetWidth; article.style.animation = "";
  setEditing(edit);
}

function renderMarkdown(text, links) {
  const body = text.replace(/^---[\s\S]*?---\n/, "");
  const inline = line => escapeHtml(line)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]/g, (_, target, shown) => {
      const id = links[target.trim()];
      const label = shown || target;
      if (!id) return `<span class="missing" title="Not written yet">${label}</span>`;
      return `<a class="link" data-id="${escapeHtml(id)}" style="--c:${colorOfId(id)}">${label}</a>`;
    });
  let html = "", inList = false;
  for (const raw of body.split("\n")) {
    const line = raw.trimEnd();
    const item = line.match(/^\s*[-*] (.*)/);
    if (item && !inList) { html += "<ul>"; inList = true; }
    if (!item && inList) { html += "</ul>"; inList = false; }
    if (item) html += `<li>${inline(item[1])}</li>`;
    else if (line.startsWith("# ")) html += `<h1>${inline(line.slice(2))}</h1>`;
    else if (line.startsWith("## ")) html += `<h2>${inline(line.slice(3))}</h2>`;
    else if (line.trim()) html += `<p>${inline(line)}</p>`;
  }
  return html + (inList ? "</ul>" : "");
}
// Click opens in this tab; Cmd/Ctrl-click opens a new one.
$("note").addEventListener("click", e => {
  const a = e.target.closest("a.link, a.crumb");
  if (a) openNote(a.dataset.id, { newTab: e.metaKey || e.ctrlKey });
});

// Editor: hides the front-matter header and the trailing "Part of [[folder]]" line, restoring them on save.
const editor = $("editor");
let noteParts = { head: "", body: "", foot: "" };
function splitNote(text) {
  const head = (text.match(/^---\n[\s\S]*?\n---\n+/) || [""])[0];
  let body = text.slice(head.length);
  const foot = (body.match(/\n*Part of \[\[[^\n]*\]\][ \t]*\n?$/) || [""])[0];
  body = body.slice(0, body.length - foot.length);
  return { head, body, foot: foot ? "\n\n" + foot.replace(/^\n+/, "") : "\n" };
}
function setEditing(on) {
  editing = on;
  $("edit").textContent = on ? "Done" : "Edit";
  $("note-body").hidden = on;
  editor.hidden = !on;
  if (on) {
    noteParts = splitNote(currentText);
    editor.value = noteParts.body;
    dirty = false;
    autosize();
    editor.focus();
    editor.setSelectionRange(editor.value.length, editor.value.length);
  }
}
function autosize() { editor.style.height = "auto"; editor.style.height = editor.scrollHeight + "px"; }
editor.addEventListener("input", () => { dirty = true; $("note-status").textContent = "Unsaved"; autosize(); });
async function saveIfDirty() {
  if (!editing || !dirty || active === "map") return;
  await send("PUT", "/api/note", { id: active, text: noteParts.head + editor.value.replace(/\s+$/, "") + noteParts.foot });
  dirty = false;
  $("note-status").textContent = "Saved";
  lastJson = "";   // force the next poll to refresh links
  refresh();
}
$("edit").onclick = async () => {
  if (editing) { await saveIfDirty(); await showNote(active, false); }
  else setEditing(true);
};
$("show-on-map").onclick = () => showOnMap(active);
function showOnMap(id) {
  localRoot = id;
  $("depth").value = 1;
  activate("map").then(() => applyLocal());
}

// =====================================================================
// Me: profile, settings, help
// =====================================================================
let mePanel = "profile", meData = null, meSaveTimer = null;

async function openMe(panel = mePanel) {
  mePanel = panel;
  if (!tabs.includes("me")) tabs.splice(tabs.indexOf(active) + 1, 0, "me");
  if (active === "me") { showPanel(); markRibbon(); }
  else await activate("me");
  $("me-view").scrollTop = 0;
}

function showPanel() {
  for (const b of document.querySelectorAll(".me-tabs button")) {
    b.setAttribute("aria-selected", b.dataset.panel === mePanel);
    b.classList.toggle("on", b.dataset.panel === mePanel);
  }
  for (const p of ["profile", "chats", "settings", "help"]) $("panel-" + p).hidden = p !== mePanel;
  if (mePanel === "profile") requestAnimationFrame(fitAllRows);
  if (mePanel === "settings") loadGoogle();
  if (mePanel === "chats") loadChats();
}
for (const b of document.querySelectorAll(".me-tabs button")) b.onclick = () => { mePanel = b.dataset.panel; showPanel(); markRibbon(); $("me-view").scrollTop = 0; };

async function showMe() {
  const res = await fetch("/api/profile");
  if (!res.ok) return;
  meData = await res.json();
  renderProfile();
  renderStarter(profileIsEmpty(meData.profile));
  $("profile-edit").hidden = true; $("me-summary").hidden = false;   // open on the summary, not the form
  await loadSetup();
  try { classColorPicks = await (await fetch("/api/class-colors")).json(); } catch {}
  renderMeSummary();
  renderSettings();
  renderFaq();
  showPanel();
  const params = new URLSearchParams(location.search);
  const google = params.get("google");
  if (google) {
    const said = { connected: "Google is connected.", cancelled: "Google sign-in was cancelled. Nothing changed.",
      failed: "Couldn't finish connecting Google. Try again.", unavailable: "Google isn't set up on this Thread yet." };
    toast(said[google] || "");
    window.history.replaceState(window.history.state, "", location.pathname + location.hash);
  }
}

// Profile form
function renderProfile() {
  const { profile, schema } = meData;
  $("about-fields").innerHTML = schema.about.map(f => f.kind === "timezone"
    ? `<div class="field tz-field"><span>${escapeHtml(f.label)}</span>
        ${zonePicker("me-tz", profile.about.timezone || "", 'data-about="timezone" aria-label="Time zone"')}
        <p class="tz-note">Every deadline and calendar time is shown in this zone. Your AI sees it too.</p></div>`
    : `<label class="field"><span>${escapeHtml(f.label)}</span>
      <input data-about="${f.key}" value="${escapeHtml(profile.about[f.key] || "")}" placeholder="${escapeHtml(f.placeholder)}"></label>`).join("");
  $("style-fields").innerHTML = schema.style.map(f => `
    <div class="choice-row"><span>${escapeHtml(f.label)}</span>
      <div class="seg" role="radiogroup" aria-label="${escapeHtml(f.label)}">${f.choices.map(c =>
        `<button type="button" role="radio" data-style="${f.key}" data-value="${escapeHtml(c)}" aria-checked="${profile.style[f.key] === c}">${escapeHtml(c)}</button>`).join("")}</div>
    </div>`).join("");
  renderList("goals", profile.goals);
  renderList("rules", profile.rules);
  $("extra").value = profile.extra || "";
  for (const el of $("panel-profile").querySelectorAll("[data-about], .list-edit textarea, #extra")) checkLength(el);
  checkItems("goals"); checkItems("rules");
  renderLearned();
}

// Soft length limits: show a counter near the guide. The profile is sent with every request.
const NEAR = 0.8;   // show the counter from 80%
function noteAfter(anchor, cls) {
  let note = anchor.nextElementSibling;
  if (!note || !note.classList.contains(cls)) {
    note = document.createElement("p");
    note.className = cls;
    anchor.after(note);
  }
  return note;
}
function checkLength(el) {
  const limit = el.id === "extra" ? meData.schema.limits.extra : meData.schema.limits.line;
  const row = el.closest(".list-row");
  const len = el.value.length;
  const existing = row ? row.querySelector(".len-note")
    : (el.nextElementSibling?.classList.contains("len-note") ? el.nextElementSibling : null);
  if (len < limit * NEAR) { existing?.remove(); return; }
  let note = existing;
  if (!note && row) { note = document.createElement("p"); note.className = "len-note"; row.append(note); }
  note = note || noteAfter(el, "len-note");
  note.classList.toggle("over", len > limit);
  note.textContent = len > limit
    ? `${len} / ${limit} characters. That's okay, nothing gets cut. Long answers just make every chat a bit slower.`
    : `${len} / ${limit}`;
}
function checkItems(id) {
  const limit = meData.schema.limits.items, n = listValues(id).length;
  const box = $(id);
  const existing = box.nextElementSibling?.classList.contains("len-note") ? box.nextElementSibling : null;
  if (n <= limit) { existing?.remove(); return; }
  const note = existing || noteAfter(box, "len-note");
  note.classList.add("over");
  note.textContent = `${n} items, more than the ${limit} we suggest. That's okay, nothing gets cut. A shorter list keeps chats fast.`;
}

// Chat backup
let chatMarkdown = "";
function ago(seconds) {
  const s = Date.now() / 1000 - seconds;
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
async function loadChats() {
  $("chat-read").hidden = true;
  let data;
  try { data = await (await fetch("/api/chats")).json(); } catch { return; }
  $("chats-unavailable").hidden = data.available;
  $("chats-unavailable").textContent = data.error || "";
  $("chat-setup").hidden = !data.available || (data.has_key && data.chats.length > 0);
  if (data.days) $("chat-days").textContent = data.days;
  const list = $("chat-list");
  list.hidden = false;
  if (!data.available) { list.innerHTML = ""; return; }
  list.innerHTML = data.chats.length ? data.chats.map(c => `
    <button class="chat-item" type="button" data-key="${escapeHtml(c.key)}">
      <span class="chat-top"><b>${escapeHtml(c.title)}</b><span class="app">${escapeHtml(c.source)}</span></span>
      <span class="chat-preview">${escapeHtml(c.preview)}</span>
      <span class="chat-when">${c.messages} messages · updated ${ago(c.updated)} · deletes in ${c.deletes_in_days < 1 ? "under a day" : Math.floor(c.deletes_in_days) + " days"}</span>
    </button>`).join("")
    : `<p class="hint empty-chats">No chats backed up yet. Once the extension is on, chats show up here a few seconds after you use them.</p>`;
}
$("chat-list").addEventListener("click", async e => {
  const b = e.target.closest(".chat-item"); if (!b) return;
  const chat = await (await fetch("/api/chats/" + encodeURIComponent(b.dataset.key))).json();
  if (chat.error) return toast(chat.error);
  $("chat-list").hidden = true; $("chat-setup").hidden = true;
  $("chat-read").hidden = false;
  $("chat-title").textContent = chat.title;
  $("chat-meta").textContent = `${chat.source_name} · ${chat.messages.length} messages`;
  $("chat-download").href = "/api/chats/" + encodeURIComponent(b.dataset.key) + "?format=md";
  $("chat-open").href = chat.url; $("chat-open").hidden = !chat.url;
  $("chat-messages").innerHTML = chat.messages.map(m => `
    <div class="msg ${m.role}"><span class="who">${m.role === "user" ? "You" : escapeHtml(chat.source_name)}</span>
      <div class="msg-text">${escapeHtml(m.text)}</div></div>`).join("");
  chatMarkdown = chat.messages.map(m => `${m.role === "user" ? "You" : chat.source_name}:\n${m.text}`).join("\n\n");
  $("me-view").scrollTop = 0;
});
$("chat-back").onclick = () => loadChats();
$("chat-copy").onclick = async () => {
  try { await navigator.clipboard.writeText(chatMarkdown); toast("Copied. Paste it into a new chat to pick up where you left off."); }
  catch { toast("Couldn't copy. Use Download instead."); }
};
$("make-key").onclick = async () => {
  try {
    const made = await confirmedFirst(() => send("POST", "/api/backup-key", {}));
    if (!made) return;
    const { key } = made;
    $("backup-key").textContent = key;
    $("backup-url").textContent = location.origin;
    $("key-box").hidden = false;
  } catch (err) { toast(err.message); }
};
for (const [btn, src] of [["copy-key", "backup-key"], ["copy-url", "backup-url"]]) {
  $(btn).onclick = async () => { try { await navigator.clipboard.writeText($(src).textContent); toast("Copied."); } catch {} };
}

// Profile templates
let starterMajor = null;
const starterUses = new Set();
function profileIsEmpty(p) {
  return !p.goals.length && !p.rules.length && !Object.values(p.style).some(Boolean) && !p.extra;
}
function renderStarter(show) {
  const { majors, uses } = meData.schema.templates;
  $("starter-majors").innerHTML = majors.map(t =>
    `<button type="button" role="radio" data-key="${t.key}" aria-checked="${starterMajor === t.key}">${escapeHtml(t.label)}</button>`).join("");
  $("starter-uses").innerHTML = uses.map(t =>
    `<button type="button" data-key="${t.key}" aria-pressed="${starterUses.has(t.key)}">${escapeHtml(t.label)}</button>`).join("");
  $("starter-apply").disabled = !starterMajor;
  $("starter").hidden = !show;
  $("starter-link").hidden = show;
}
$("starter-majors").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  starterMajor = b.dataset.key; renderStarter(true);
});
$("starter-uses").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  starterUses.has(b.dataset.key) ? starterUses.delete(b.dataset.key) : starterUses.add(b.dataset.key);
  renderStarter(true);
});
$("starter-skip").onclick = () => renderStarter(false);
$("starter-link").querySelector("button").onclick = () => renderStarter(true);
$("starter-apply").onclick = async () => {
  const { majors, uses } = meData.schema.templates;
  const picked = [majors.find(t => t.key === starterMajor), ...uses.filter(t => starterUses.has(t.key))];
  const before = structuredClone(meData.profile);
  const p = structuredClone(meData.profile);
  const add = (list, items) => { for (const x of items) if (!list.includes(x)) list.push(x); };
  for (const t of picked) { add(p.goals, t.goals); add(p.rules, t.rules); }
  Object.assign(p.style, picked[0].style);   // the major sets the answer style
  try {
    meData.profile = await send("PUT", "/api/profile", p);
    renderProfile(); renderStarter(false);
    toast("Filled in. Change anything you like.", "Undo", async () => {
      meData.profile = await send("PUT", "/api/profile", before); renderProfile(); renderStarter(true);
    });
  } catch (err) { toast(err.message); }
};

// Line-per-item list: Enter adds, Backspace on empty removes.
function renderList(id, items) {
  const box = $(id);
  box.innerHTML = "";
  for (const text of [...items, ""]) addListRow(box, text);
}
// Autosize textareas. Hidden panels measure 0px, so skip them and re-measure when shown.
function fitRow(input) {
  if (!input.offsetParent) return;
  input.style.height = "auto";
  input.style.height = input.scrollHeight + "px";
}
function fitAllRows() { for (const t of document.querySelectorAll("#me-page .list-row textarea")) fitRow(t); }
addEventListener("resize", () => requestAnimationFrame(fitAllRows));

function addListRow(box, text, focus = false, after = null) {
  const row = document.createElement("div");
  row.className = "list-row";
  // Wrapping single-line textarea.
  row.innerHTML = `<span class="bullet"></span><textarea rows="1" placeholder="${escapeHtml(box.dataset.placeholder)}"></textarea>
    <button class="icon remove" type="button" aria-label="Remove" title="Remove">${ICONS.close}</button>`;
  const input = row.querySelector("textarea");
  input.value = text;
  const fit = () => fitRow(input);
  requestAnimationFrame(fit);
  input.addEventListener("input", () => {
    if (input.value.includes("\n")) input.value = input.value.replace(/\n+/g, " ");   // strip pasted newlines
    fit();
  });
  input.addEventListener("input", () => {
    // Keep one empty row at the end.
    if (box.lastElementChild === row && input.value) addListRow(box, "");
    checkLength(input); checkItems(box.id);
    queueSave();
  });
  input.addEventListener("keydown", e => {
    if (e.key === "Enter") {
      e.preventDefault();
      const next = row.nextElementSibling?.querySelector("textarea");
      if (next && !next.value) next.focus(); else addListRow(box, "", true, row);
    }
    if (e.key === "Backspace" && !input.value && box.children.length > 1) {
      e.preventDefault();
      const prev = row.previousElementSibling?.querySelector("textarea");
      row.remove(); prev?.focus(); queueSave();
    }
  });
  row.querySelector(".remove").onclick = () => {
    if (box.children.length > 1) row.remove(); else input.value = "";
    if (box.lastElementChild.querySelector("textarea").value) addListRow(box, "");
    queueSave();
  };
  if (after) after.after(row); else box.append(row);
  if (focus) input.focus();
  return input;
}
const listValues = id => [...$(id).querySelectorAll("textarea")].map(i => i.value.trim()).filter(Boolean);

$("panel-profile").addEventListener("input", e => {
  if (e.target.matches("[data-about], #extra")) { checkLength(e.target); queueSave(); }
});
$("style-fields").addEventListener("click", e => {
  const b = e.target.closest("button[data-style]");
  if (!b) return;
  const on = b.getAttribute("aria-checked") === "true";
  for (const other of b.parentElement.children) other.setAttribute("aria-checked", "false");
  b.setAttribute("aria-checked", String(!on));   // tap again to clear
  queueSave(0);
});

function queueSave(delay = 700) {
  $("me-status").textContent = "Saving…";
  clearTimeout(meSaveTimer);
  meSaveTimer = setTimeout(saveProfile, delay);
}
async function saveProfile() {
  const about = {}, style = {};
  for (const i of document.querySelectorAll("[data-about]")) about[i.dataset.about] = i.value.trim();
  for (const b of document.querySelectorAll("button[data-style][aria-checked=true]")) style[b.dataset.style] = b.dataset.value;
  try {
    meData.profile = await send("PUT", "/api/profile", { about, style, goals: listValues("goals"), rules: listValues("rules"), extra: $("extra").value });
    dueChanged();   // time zone may have changed
    $("me-status").textContent = "Saved";
    lastJson = "";
  } catch (err) {
    $("me-status").textContent = "Not saved: " + err.message;
  }
}

function renderLearned() {
  const list = $("learned-list");
  if (!meData.learned.length) {
    list.innerHTML = `<li class="empty-learned">Nothing yet. As you chat, your AI will notice how you like things done and save it here.</li>`;
    return;
  }
  list.innerHTML = meData.learned.map((o, i) => `
    <li><div><span class="cat">${escapeHtml(o.category)}</span>${escapeHtml(o.text)}
      <span class="seen">${o.times > 1 ? `noticed ${o.times} times · ` : ""}last ${escapeHtml(o.last)}</span></div>
      <button class="icon remove" data-i="${i}" aria-label="Forget this" title="Forget this">${ICONS.close}</button></li>`).join("");
}
$("learned-list").addEventListener("click", async e => {
  const b = e.target.closest("button[data-i]");
  if (!b) return;
  const item = meData.learned[+b.dataset.i];
  try {
    const gone = await send("POST", "/api/forget-learned", { text: item.text });
    meData.learned = meData.learned.filter(o => o !== item);
    renderLearned();
    // No confirm dialog: moves to trash with undo.
    toast("Forgotten", "Undo", async () => {
      try {
        await send("POST", "/api/restore-learned", { trash: gone.trash });
        meData = await (await fetch("/api/profile")).json();
        renderLearned();
      } catch (err) { toast(err.message); }
    });
  } catch (err) { toast(err.message); }
});
document.querySelector("#panel-profile .jump").onclick = e => { e.preventDefault(); $("learned").scrollIntoView({ behavior: "smooth" }); };

// Settings
// Prefilled claude.ai "add connector" link.
function claudeInstallUrl(mcp) {
  return "https://claude.ai/customize/connectors?modal=add-custom-connector&connectorName=Thread&connectorUrl=" + encodeURIComponent(mcp);
}
const CANVAS_CALENDAR = "https://canvas.cmu.edu/calendar";

function renderSettings() {
  $("account-online").hidden = !meData.online;
  $("account-local").hidden = meData.online;
  $("sealed-note").hidden = !meData.online;   // only the hosted server encrypts tokens at rest
  $("mcp-online").hidden = !meData.online;
  $("mcp-local").hidden = meData.online;
  if (meData.mcp_url) { $("mcp-url").textContent = meData.mcp_url; $("add-claude").href = claudeInstallUrl(meData.mcp_url); }
  if (meData.email) { $("account-email").textContent = meData.email; $("pw-username").value = meData.email; }
  document.querySelectorAll(".online-only").forEach(el => el.hidden = !meData.online);
  if (meData.online) loadAccount().then(renderSecurity);
  renderCanvas();
  loadSetup().then(renderGuide);
  for (const r of document.querySelectorAll("#privacy input")) r.checked = r.value === meData.privacy;
  for (const r of document.querySelectorAll("#saving input")) r.checked = r.value === (meData.saving || "everything");
  markTheme();
}

// Canvas: calendar feed by default; an access token (advanced) adds submission status and syllabus files.
function renderCanvas() {
  const token = meData.canvas, feed = meData.canvas_feed;
  $("canvas-state").textContent = token ? "Connected with an access token: deadlines, what you've submitted, and syllabuses."
    : feed ? "Connected through your calendar feed: your deadlines come in from Canvas."
    : meData.online ? "Not connected. Bring in your deadlines with one link from Canvas."
    : "Not connected. Your AI can walk you through adding a Canvas access token.";
  $("canvas-btns").innerHTML = !meData.online ? ""
    : token ? `<button class="text-btn" id="canvas-new">Use a new token</button><button class="text-btn danger" id="canvas-off">Disconnect</button>`
    : feed ? `<button class="text-btn" id="canvas-new">Add more detail</button><button class="text-btn danger" id="feed-off">Disconnect</button>`
    : `<button class="text-btn" id="feed-new">Bring in deadlines</button>`;
  $("canvas-form").hidden = true; $("feed-form").hidden = true; $("canvas-btns").hidden = false;
  const showToken = () => {
    $("feed-form").hidden = true; $("canvas-form").hidden = false; $("canvas-btns").hidden = true;
    $("canvas-msg").textContent = ""; $("canvas-token").focus();
  };
  $("canvas-new")?.addEventListener("click", showToken);
  $("feed-advanced").onclick = showToken;
  $("feed-new")?.addEventListener("click", () => {
    $("feed-form").hidden = false; $("canvas-btns").hidden = true; $("feed-msg").textContent = ""; $("feed-url").focus();
  });
  $("canvas-off")?.addEventListener("click", async () => {
    try { await send("POST", "/api/canvas/disconnect", {}); meData.canvas = false; dueChanged(); renderCanvas(); toast("Canvas disconnected. Nothing in Canvas was changed."); }
    catch (err) { toast(err.message); }
  });
  $("feed-off")?.addEventListener("click", async () => {
    try { await send("POST", "/api/canvas-feed/disconnect", {}); meData.canvas_feed = false; dueChanged(); renderCanvas(); toast("Canvas disconnected. Nothing in Canvas was changed."); }
    catch (err) { toast(err.message); }
  });
}
$("feed-cancel").onclick = () => { $("feed-form").hidden = true; $("canvas-btns").hidden = false; $("feed-url").value = ""; };
$("feed-form").onsubmit = async e => {
  e.preventDefault();
  const msg = $("feed-msg"), button = e.target.querySelector("button.primary");
  msg.className = "form-msg"; msg.textContent = "Reading your Canvas calendar…"; button.disabled = true;
  try {
    await send("POST", "/api/canvas-feed", { url: $("feed-url").value });
    $("feed-url").value = ""; meData.canvas_feed = true; dueChanged(); renderCanvas();
    toast("Your Canvas deadlines are in.");
  } catch (err) { msg.textContent = err.message; msg.classList.add("bad"); }
  finally { button.disabled = false; }
};
$("canvas-cancel").onclick = () => { $("canvas-form").hidden = true; $("canvas-btns").hidden = false; $("canvas-token").value = ""; };
$("canvas-form").onsubmit = async e => {
  e.preventDefault();
  const msg = $("canvas-msg"), button = e.target.querySelector("button.primary");
  msg.className = "form-msg"; msg.textContent = "Checking with Canvas…"; button.disabled = true;
  try {
    await send("POST", "/api/canvas", { token: $("canvas-token").value });
    dueChanged();
    $("canvas-token").value = ""; meData.canvas = true; renderCanvas();
    toast("Canvas is connected.");
  } catch (err) { msg.textContent = err.message; msg.classList.add("bad"); }
  finally { button.disabled = false; }
};

$("feedback-form").onsubmit = async e => {
  e.preventDefault();
  const msg = $("feedback-msg"), button = e.target.querySelector("button.primary"), text = $("feedback-text");
  msg.className = "form-msg"; msg.textContent = "Sending…"; button.disabled = true;
  try {
    // Context for bug reports; never note contents.
    const context = { tab: active === "me" ? `Me → ${mePanel}` : (active === "map" ? "Map" : "a note"),
                      screen: `${screen.width}×${screen.height}`, viewport: `${innerWidth}×${innerHeight}`,
                      browser: navigator.userAgent };
    await send("POST", "/api/feedback", { message: text.value, context });
    text.value = "";
    msg.textContent = "Sent, thank you. It went straight to the person building Thread.";
    msg.classList.add("good");
  } catch (err) { msg.textContent = err.message; msg.classList.add("bad"); }
  finally { button.disabled = false; }
};

// First-run setup: one step per screen, all skippable. Progress is read from the server.
const SETUP_POLL_MS = 4000;   // poll interval for "Connect your AI"
let setupState = null, setupSteps = [], setupAt = 0, setupTimer = null, setupAi = "claude";
// Three steps: a short question, then the two connections. The rest is filled in over time.
const SETUP_ALL = ["about", "ai", "canvas", "done"];
const GLYPH = {
  about: '<circle cx="9" cy="10" r="4.5"/><circle cx="16.5" cy="15.5" r="3"/><path d="M12.3 12.6l2 1.6"/>',
  ai: '<path d="M8 12h8M12 8v8"/><rect x="3.5" y="3.5" width="17" height="17" rx="5"/>',
  canvas: '<rect x="4" y="5" width="16" height="15" rx="2.5"/><path d="M4 9.5h16M8.5 3v4M15.5 3v4"/>',
  done: '<path d="M6 12.5l4 4 8-9"/>',
};
const glyph = key => `<div class="setup-glyph"><svg viewBox="0 0 24 24">${GLYPH[key]}</svg></div>`;
// Reuse the Settings copy so the two stay in sync.
const settingsSteps = (selector, pick) => [...document.querySelectorAll(selector + " li")]
  .filter((_, i) => !pick || pick.includes(i)).map(li => `<li>${li.innerHTML}</li>`).join("");

async function loadSetup() {
  try { setupState = await (await fetch("/api/setup")).json(); } catch { setupState = null; }
  return setupState;
}
async function setupSay(body) {
  try { setupState = await send("POST", "/api/setup", body); } catch {}
  renderGuide();
}
function stepDone(key) { return !!setupState?.done?.[key]; }

let setupBefore = 0;   // sign-up screens before setup (?welcome=<n>), for the step counter
async function openSetup(startAt = "about", before = 0) {
  setupBefore = before;
  if (!meData) { const r = await fetch("/api/profile"); if (r.ok) meData = await r.json(); }
  await loadSetup();
  setupSteps = SETUP_ALL;
  setupAt = Math.max(0, setupSteps.indexOf(startAt));
  $("setup").hidden = false;
  setupSay({ seen: true });
  renderSetupStep();
}
function closeSetup() {
  clearInterval(setupTimer); setupTimer = null;
  $("setup").hidden = true;
  renderGuide();
  dueChanged();   // Canvas may have just connected; drop Home's cache
  if (typeof renderHome === "function") renderHome();
  if (zoneAfterSetup) { zoneAfterSetup = false; checkZone(); }
}
function setupGo(delta) {
  const key = setupSteps[setupAt];
  if (delta > 0 && ["about", "ai", "canvas"].includes(key)) setupSay(stepDone(key) ? { unskip: key } : { skipped: key });
  setupAt = Math.min(setupSteps.length - 1, Math.max(0, setupAt + delta));
  renderSetupStep();
}

function renderSetupStep() {
  clearInterval(setupTimer); setupTimer = null;
  const key = setupSteps[setupAt], body = $("setup-body"), next = $("setup-next"), skip = $("setup-skip");
  // Step counter, counting only screens this user sees.
  const asks = setupSteps.filter(k => k !== "done");
  $("setup-count").textContent = key === "done" ? "" : `${setupBefore + asks.indexOf(key) + 1} of ${setupBefore + asks.length}`;
  $("setup-back").classList.toggle("gone", setupAt === 0);
  skip.hidden = key === "done";
  skip.textContent = key === "about" ? "Skip" : "Set up later";
  next.textContent = "Continue";
  next.disabled = false;
  next.classList.remove("quiet");   // one primary button per screen
  const online = setupState?.online;
  if (key === "about") {
    const a = meData?.profile?.about || {};
    body.innerHTML = `${glyph("about")}<h1 id="setup-title">Welcome to Thread</h1>
      <p class="lede">One memory you own, for every AI you use. Thread fills in the rest as you chat.</p>
      <div class="block">
        <label class="field">What should your AI call you?<input type="text" id="su-name" autocomplete="given-name"
          autocapitalize="words" enterkeyhint="next" value="${escapeHtml(a.name || meData?.name_guess || "")}"></label>
      </div>`;
  } else if (key === "ai") {
    const url = meData?.mcp_url || "";
    body.innerHTML = `${glyph("ai")}<h1 id="setup-title">Add Thread to your AI</h1>
      <p class="lede">Then every chat knows you, on your laptop and your phone.</p>
      ${online ? `<div class="block" id="su-ai"></div>
        <p class="setup-status" id="su-ai-status" aria-live="polite"></p>`
      : `<p class="lede">${escapeHtml($("mcp-local").textContent.trim())}</p>`}`;
    if (online) {
      const show = () => {
        $("su-ai").innerHTML = setupAi === "claude"
          ? `<a class="primary big" id="su-add-claude" href="${escapeHtml(claudeInstallUrl(url))}" target="_blank" rel="noopener">Add to Claude</a>
             <p class="hint center">It opens Claude with Thread filled in. Press <b>Add</b>, then <b>Connect</b>, then <b>Continue as you</b>.</p>
             <button type="button" class="text-btn quiet" id="su-other">I use ChatGPT</button>`
          : `<div class="copy-row"><code>${escapeHtml(url)}</code><button class="text-btn" type="button" id="su-copy">Copy</button></div>
             <ol class="steps">${settingsSteps("#mcp-online .steps", [1, 2])}</ol>
             <button type="button" class="text-btn quiet" id="su-other">I use Claude</button>`;
        $("su-other").onclick = () => { setupAi = setupAi === "claude" ? "chatgpt" : "claude"; show(); };
        $("su-copy")?.addEventListener("click", async () => {
          try { await navigator.clipboard.writeText(url); toast("Copied."); }
          catch { toast("Couldn't copy. Select the address and copy it."); }
        });
      };
      show();
      const status = () => {
        const apps = setupState?.ai_apps || [], el = $("su-ai-status");
        if (!el) return;
        if (apps.length) { el.className = "setup-status good"; el.textContent = `✓ ${apps.join(" and ")} connected`; skip.hidden = true; next.classList.remove("quiet"); }
        else { el.className = "setup-status"; el.innerHTML = `<span class="pulse"></span> Waiting for your AI to connect…`; next.classList.add("quiet"); }
      };
      status();
      setupTimer = setInterval(async () => { await loadSetup(); status(); }, SETUP_POLL_MS);
    }
  } else if (key === "canvas") {
    const on = stepDone("canvas");
    body.innerHTML = `${glyph("canvas")}<h1 id="setup-title">Bring in your deadlines</h1>
      <p class="lede">One link from Canvas, so your AI knows what's due. Thread only reads it.</p>
      ${on ? `<p class="setup-status good">✓ Your Canvas deadlines are in</p>`
      : online ? `<div class="block">
          <a class="primary big" href="${CANVAS_CALENDAR}" target="_blank" rel="noopener">Open Canvas Calendar</a>
          <p class="hint center">${$("#feed-steps".slice(1)) ? $("feed-steps").children[1].innerHTML : ""}</p>
          <div class="paste-row"><input type="url" id="su-feed" autocomplete="off" spellcheck="false" aria-label="Calendar Feed link"
            placeholder="Paste the Calendar Feed link"><button type="button" class="text-btn" id="su-paste">Paste</button></div>
          <p class="form-msg" id="su-feed-msg" role="alert"></p></div>`
      : `<p class="lede">On this laptop, Canvas uses the Keychain (see the README).</p>`}`;
    if (on) skip.hidden = true; else if (online) next.classList.add("quiet");
    $("su-paste")?.addEventListener("click", async () => {
      try { $("su-feed").value = (await navigator.clipboard.readText()).trim(); }
      catch { $("su-feed").focus(); toast("Paste the link into the box."); }
    });
  } else if (key === "done") {
    const left = ["ai", "canvas"].filter(k => !stepDone(k));
    body.innerHTML = `${glyph("done")}<h1 id="setup-title">You're all set</h1>
      <p class="lede">${left.length ? "Anything you skipped is on your Home screen, one tap away." : "Everything's connected."}</p>
      <ul class="setup-try">
        <li>Ask your AI: “What's due this week?”</li>
        <li>Tell it: “Remember I have office hours Thursday at 3.”<span>Watch it pop onto your map.</span></li>
      </ul>`;
    next.textContent = "Go to Home";
  }
  body.scrollTop = 0;
}

$("setup-next").onclick = async () => {
  const key = setupSteps[setupAt];
  if (key === "about") {
    const name = $("su-name").value.trim();
    if (name && name !== (meData.profile.about.name || "")) {
      const p = structuredClone(meData.profile);
      p.about.name = name;
      try { meData.profile = await send("PUT", "/api/profile", p); await loadSetup(); renderProfile(); }
      catch (err) { toast(err.message); return; }
    }
  } else if (key === "canvas" && !stepDone("canvas") && setupState?.online) {
    const url = $("su-feed").value.trim(), msg = $("su-feed-msg");
    if (!url) { setupGo(1); return; }
    msg.className = "form-msg"; msg.textContent = "Reading your Canvas calendar…"; $("setup-next").disabled = true;
    try { await send("POST", "/api/canvas-feed", { url }); meData.canvas_feed = true; dueChanged(); await loadSetup(); renderCanvas(); }
    catch (err) { msg.textContent = err.message; msg.classList.add("bad"); $("setup-next").disabled = false; return; }
  } else if (key === "done") {
    closeSetup(); goHome(); return;
  }
  setupGo(1);
};
$("setup-skip").onclick = () => setupGo(1);
$("setup-back").onclick = () => setupGo(-1);
$("setup-close").onclick = closeSetup;
$("setup").addEventListener("keydown", e => {
  if (e.key === "Escape") return closeSetup();
  // Enter advances (except on buttons and links).
  if (e.key === "Enter" && !e.target.closest("button, a, textarea") && !$("setup-next").disabled) { e.preventDefault(); $("setup-next").click(); }
});

// Settings → Setup guide: same steps with real status.
const GUIDE = [
  ["ai", "Add Thread to your AI", "Claude or ChatGPT, on laptop and phone"],
  ["canvas", "Canvas deadlines", "One link from Canvas Calendar"],
  ["backup", "Chat backup", "Chrome on a computer"],
];
function renderGuide() {
  const rows = $("guide-rows");
  if (!rows) return;
  rows.innerHTML = GUIDE.map(([key, title, what]) => {
    const done = stepDone(key), here = key !== "backup" || !phone.matches;
    if (!here) what = "Copies your ChatGPT and Claude chats";
    return `<div class="guide-row"><div class="what"><b>${title}</b><span>${what}</span></div>
      ${done ? `<span class="state good">✓ Done</span>`
        : key === "backup" && here ? `<button class="text-btn" type="button" data-chats>Set up</button>`
        : here ? `<button class="text-btn" type="button" data-step="${key}">Set up</button>`
        : `<span class="state">On a computer</span>`}</div>`;
  }).join("");
}
$("guide-rows").addEventListener("click", e => {
  const b = e.target.closest("button[data-step]"); if (b) return openSetup(b.dataset.step);
  if (e.target.closest("button[data-chats]")) openMe("chats");
});
$("setup-again").onclick = () => openSetup("about");
function goHome() { activate("home"); }

// Home
// New accounts get one next action and a self-ticking checklist. With Canvas connected:
// the next exam, the week's deadlines, and recent saves.
const DAY = 86400000;
// only exams make the headline
const EXAMISH = /\b(exams?|midterms?|finals?|tests?)\b/i;
let homeDue = null, homeDueAt = 0;
// Invalidate Home's cache when anything affecting due dates changes.
function dueChanged() { homeDue = null; homeDueAt = 0; }
function localDate(iso) { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d); }
function daysLeft(iso) { const t = new Date(); t.setHours(0, 0, 0, 0); return Math.round((localDate(iso) - t) / DAY); }
function leftChip(n) { return n <= 0 ? "Today" : n === 1 ? "Tomorrow" : `In ${n} days`; }
const fmtDay = iso => localDate(iso).toLocaleDateString(undefined, { weekday: "short" });
const fmtDate = iso => localDate(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
// "21241 Matrices…" → "21-241"
const courseShort = c => {
  const m = c.match(/^\s*([0-9]{2})-?([0-9]{3})((-[A-Z0-9]+)?)/);
  if (m && m[3]) return c.trim().split(/\s+/)[0];   // keep section codes like 99101-DC
  // No course number: use the full name.
  return m ? `${m[1]}-${m[2]}` : c.trim();
};

async function renderHome() {
  const box = $("home");
  if (!meData) { const r = await fetch("/api/profile"); if (r.ok) meData = await r.json(); }
  await loadSetup();
  if (!homeDue || Date.now() - homeDueAt > 5 * 60000) {
    try { homeDue = await (await fetch("/api/due")).json(); homeDueAt = Date.now(); } catch { homeDue = { items: [] }; }
  }
  try { classColorPicks = await (await fetch("/api/class-colors")).json(); } catch {}
  const name = meData?.profile?.about?.name, aiOn = stepDone("ai"), canvasOn = stepDone("canvas");
  const isOnline = !!(setupState?.online ?? meData?.online);   // the server knows; the URL can't
  const today = new Date().toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
  const items = homeDue?.items || [];
  let html = `<p class="eyebrow">${escapeHtml(today)}</p>`;

  const big = items.filter(i => EXAMISH.test(i.title));
  if (big.length) {
    const day = big[0].date, same = big.filter(i => i.date === day), n = daysLeft(day);
    const count = same.length === 1 ? "One exam" : `${["", "", "Two", "Three", "Four"][same.length] || same.length} exams`;
    html += `<h1 class="home-title">Big day on ${fmtDay(day)} ${localDate(day).getDate()}. ${count}.</h1>
      <p class="home-sub">${same.map(i => escapeHtml(courseShort(i.course) + " " + i.title)).join(" and ")}, ${n <= 0 ? "today" : n === 1 ? "tomorrow" : n + " days out"}.</p>`;
  } else {
    html += `<h1 class="home-title">${name ? `Hi, ${escapeHtml(name)}.` : "Welcome to Thread."}</h1>
      <p class="home-sub">${items.length ? "Here's what's coming up." : "Thread fills in as you chat."}</p>`;
  }

  const next = !aiOn ? ["Add Thread to Claude", "About a minute. Then ask Claude “what's due this week?”",
      `<a class="primary big" href="${escapeHtml(claudeInstallUrl(meData?.mcp_url || ""))}" target="_blank" rel="noopener">Add to Claude</a>
       <button type="button" class="text-btn quiet" data-setup="ai">I use ChatGPT</button>`]
    : !canvasOn && isOnline ? ["Bring in your deadlines", "One link from Canvas, so your AI knows what's due.",
      `<button type="button" class="primary big" data-setup="canvas">Bring in deadlines</button>`] : null;
  if (next && isOnline) html += `<section class="home-card next"><p class="kicker">Next</p><h2>${next[0]}</h2>
      <p class="muted">${next[1]}</p>${next[2]}</section>`;
  // Two-step sign-in suggestion, from the 3rd sign-in.
  if (!next && isOnline) {
    if (!account) await loadAccount();
    if (account?.nudge) html += `<section class="home-card next" id="nudge-2fa"><p class="kicker">Suggested</p>
      <h2>Lock your account</h2><p class="muted">Turn on two-step sign-in, so a stolen password isn't enough to read your memory.</p>
      <button type="button" class="primary big" id="nudge-on">Turn on</button>
      <button type="button" class="text-btn quiet" id="nudge-later">Not now</button></section>`;
  }

  items.forEach(i => i.class_key && dueClassKeys.add(i.class_key));
  if (items.length) {
    html += `<section class="home-card week" aria-label="Coming up">${items.slice(0, 8).map(i => {
      const n = daysLeft(i.date);
      return `<a class="due-row" ${i.url ? `href="${escapeHtml(i.url)}" target="_blank" rel="noopener"` : ""} style="--c:${classColor(i.class_key)}">
        <span class="thread-bar" aria-hidden="true"></span>
        <span class="due-what"><b>${escapeHtml(courseShort(i.course))} · ${escapeHtml(i.title)}</b>
          <span>${escapeHtml([i.type === "assignment" ? "" : i.type, i.time].filter(Boolean).join(" · ") || "Due")}</span></span>
        <span class="due-when"><b>${fmtDay(i.date)}</b><span>${fmtDate(i.date)}</span>
          <span class="chip${n <= 1 ? " soon" : ""}">${leftChip(n)}</span></span></a>`;
    }).join("")}</section>`;
    if (homeDue.source === "feed") html += `<p class="home-note">From your Canvas calendar. Already-submitted work may still show.</p>`;
  } else if (canvasOn) {
    html += `<section class="home-card"><p class="muted">Nothing due on Canvas in the next two weeks.</p></section>`;
  }

  const learned = meData?.learned || [];
  if (learned.length) html += `<h3 class="home-h3">Your AI learned</h3><section class="home-card learned-card">
      <p>“${escapeHtml(learned[0].text)}”</p><p class="muted small">${learned.length === 1 ? "1 thing so far" : learned.length + " things so far"} ·
      <button type="button" class="link" data-go="me">See all</button></p></section>`;

  const left = [["account", "Account", true], ["ai", "Your AI", aiOn, "Claude or ChatGPT"],
                ["canvas", "Canvas deadlines", canvasOn, "One link"],
                ...(phone.matches ? [] : [["backup", "Chat backup", stepDone("backup"), "Chrome on this computer"]])];
  if (isOnline && left.some(r => !r[2])) html += `<section class="home-card checklist">${left.map(([k, label, done, hint]) =>
      `<button type="button" class="check-row${done ? " done" : ""}" ${done ? "disabled" : k === "backup" ? 'data-go-chats' : `data-setup="${k}"`}>
        <span class="tick" aria-hidden="true">${done ? "✓" : ""}</span><span class="what"><b>${label}</b>${hint && !done ? `<span>${hint}</span>` : ""}</span>
        ${done ? `<span class="sr">done</span>` : `<span class="chev" aria-hidden="true">›</span>`}</button>`).join("")}</section>`;
  box.innerHTML = html;
  box.querySelectorAll("[data-setup]").forEach(b => b.onclick = () => openSetup(b.dataset.setup));
  box.querySelector("[data-go-chats]")?.addEventListener("click", () => openMe("chats"));
  $("nudge-on")?.addEventListener("click", startTwoStep);
  $("nudge-later")?.addEventListener("click", async () => {
    try { await send("POST", "/api/nudge-2fa", {}); account.nudge = false; $("nudge-2fa")?.remove(); } catch (err) { toast(err.message); }
  });
}

// Me summary
const ABOUT_ORDER = ["name", "school", "year", "major", "classes", "pronouns", "location", "timezone", "work"];
function renderMeSummary() {
  const box = $("me-summary");
  if (!box || !meData) return;
  const a = meData.profile.about, labels = Object.fromEntries(meData.schema.about.map(f => [f.key, f.label]));
  const zone = () => (meData.schema.timezones || []).flatMap(g => g.zones).find(z => z.id === a.timezone)?.label || a.timezone;
  const rows = ABOUT_ORDER.filter(k => a[k]).map(k => `<button type="button" class="me-row" data-edit="${k}">
      <span class="label">${escapeHtml(labels[k] || k)}</span><span class="value">${escapeHtml(k === "timezone" ? zone() : a[k])}</span>
      <span class="chev" aria-hidden="true">›</span></button>`).join("");
  const classes = [...new Set(nodes.map(classKey).filter(Boolean))].sort();
  const learned = meData.learned || [];
  const apps = setupState?.ai_apps || [];
  const conn = meData.online ? `<h3 class="me-h3">Connected</h3><section class="me-group">
      <div class="me-row static"><span class="label">Your AI</span><span class="value">${apps.length ? "✓ " + escapeHtml(apps.join(", ")) : ""}</span>
        ${apps.length ? "" : `<button type="button" class="text-btn" data-setup="ai">Add</button>`}</div>
      <div class="me-row static"><span class="label">Canvas</span><span class="value">${meData.canvas ? "✓ Access token" : meData.canvas_feed ? "✓ Calendar feed" : ""}</span>
        ${meData.canvas || meData.canvas_feed ? "" : `<button type="button" class="text-btn" data-setup="canvas">Add</button>`}</div>
    </section>` : "";
  box.innerHTML = `
    <h3 class="me-h3">You</h3>
    <section class="me-group">${rows || `<p class="me-empty">Thread fills this in as you chat.</p>`}</section>
    ${classes.length ? `<h3 class="me-h3">Classes</h3><section class="me-group">${classes.map(k => {
      const hub = byId.get(`classes/${k}/${k}`);
      return `<div class="me-row static"><span class="label">${escapeHtml((hub ? hub.title : k).replace(/^(\d{2})-?(\d{3})\b/, "$1-$2"))}</span>
        <button type="button" class="swatch" data-class="${escapeHtml(k)}" style="--c:${classColor(k)}" aria-label="Change this class's colour"></button></div>`;
    }).join("")}</section>` : ""}
    <h3 class="me-h3">What your AI learned</h3>
    <section class="me-group">${learned.length ? learned.slice(0, 3).map(o => `<div class="me-row static"><span class="value left">${escapeHtml(o.text)}</span></div>`).join("")
      + `<button type="button" class="me-row" data-edit="learned"><span class="label">${learned.length === 1 ? "1 thing so far" : learned.length + " things so far"}</span><span class="chev">›</span></button>`
      : `<p class="me-empty">Nothing yet. As you chat, your AI saves how you like to work, and it shows up here.</p>`}</section>
    ${conn}
    <p class="me-edit-link"><button type="button" class="text-btn" id="me-edit-all">Edit your profile</button></p>`;
  box.querySelectorAll("[data-edit]").forEach(b => b.onclick = () => openProfileEdit(b.dataset.edit));
  box.querySelectorAll("[data-setup]").forEach(b => b.onclick = () => openSetup(b.dataset.setup));
  $("me-edit-all").onclick = () => openProfileEdit();
  box.querySelectorAll(".swatch").forEach(b => b.onclick = async () => {
    const key = b.dataset.class, nextIndex = (classIndex(key) + 1) % PALETTE_SIZE;
    try {
      classColorPicks = await send("POST", "/api/class-colors", { key, index: nextIndex });
      b.style.setProperty("--c", classColor(key));
      renderMap(); draw();
    } catch (err) { toast(err.message); }
  });
}
function openProfileEdit(field) {
  $("profile-edit").hidden = false;
  $("me-summary").hidden = true;
  const input = field && field !== "learned" ? document.querySelector(`#about-fields [data-about="${field}"]`) : null;
  const target = field === "learned" ? $("learned") : input?.closest(".field") || null;
  (target || $("profile-edit")).scrollIntoView({ block: "start" });
  input?.focus();
}


async function loadGoogle() {
  const state = $("google-state"), btns = $("google-btns");
  state.textContent = "Checking…";
  btns.innerHTML = "";
  let g;
  try { g = await (await fetch("/api/google")).json(); } catch { g = { state: "unknown" }; }
  const connect = label => `<a class="text-btn" href="/google/connect">${label}</a>`;
  const said = {
    connected: g.account ? `Connected as ${escapeHtml(g.account)}` : "Connected",
    disconnected: "Not connected. Connect it so your AI can see your schedule and add due dates.",
    expired: "The sign-in expired. Reconnect to keep your calendar in sync.",
    unavailable: "Not set up on this Thread yet.",
    unknown: "Couldn't check right now.",
  };
  // Hide the Google button until OAuth is configured.
  $("google-row").hidden = g.state === "unavailable";
  state.innerHTML = said[g.state] || said.unknown;
  if (g.state === "connected") btns.innerHTML = connect("Switch account") + `<button class="text-btn danger" id="google-off">Disconnect</button>`;
  else if (g.state === "disconnected" || g.state === "expired") btns.innerHTML = connect(g.state === "expired" ? "Reconnect" : "Connect Google");
  $("google-off")?.addEventListener("click", async () => {
    try { await send("POST", "/api/google/disconnect", {}); toast("Google disconnected. Your calendar itself wasn't changed."); loadGoogle(); }
    catch (err) { toast(err.message); }
  });
}

$("password-form").onsubmit = async e => {
  e.preventDefault();
  const msg = $("pw-msg");
  msg.className = "form-msg";
  if ($("pw-new").value !== $("pw-again").value) { msg.textContent = "The new passwords don't match."; msg.classList.add("bad"); return; }
  try {
    const done = await confirmedFirst(() => send("POST", "/api/password", { current: $("pw-current").value, new: $("pw-new").value }));
    if (!done) return;
    e.target.reset();
    msg.textContent = "Password changed. Other browsers and AI apps were signed out; sign in there again with the new one.";
    msg.classList.add("good");
  } catch (err) { msg.textContent = err.message; msg.classList.add("bad"); }
};

// Account security: two-step sign-in, step-up, recent sign-ins. Uses the setup sheet's step layout.
let account = null;
async function loadAccount() {
  try { const r = await fetch("/api/account"); account = r.ok ? await r.json() : null; } catch { account = null; }
  return account;
}
const fmtWhen = at => new Date(at * 1000).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
function renderSecurity() {
  if (!account?.online) return;
  const t = account.twofa, btns = $("twofa-btns");
  $("twofa-state").textContent = t.on
    ? `On · ${t.recovery_left} recovery code${t.recovery_left === 1 ? "" : "s"} left`
    : "Off";
  $("twofa-state").classList.toggle("warn", t.on && t.recovery_left <= 3);
  btns.innerHTML = t.on
    ? `<button class="text-btn danger" type="button" id="twofa-off">Turn off</button>`
    : `<button class="primary" type="button" id="twofa-on">Turn on</button>`;
  $("twofa-more").hidden = !t.on;
  $("twofa-more").innerHTML = t.on ? `<button class="text-btn" type="button" id="twofa-codes">New recovery codes</button>
    <span class="hint">On since ${new Date(t.since * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}</span>` : "";
  $("twofa-on")?.addEventListener("click", startTwoStep);
  $("twofa-off")?.addEventListener("click", stopTwoStep);
  $("twofa-codes")?.addEventListener("click", newRecoveryCodes);
  const rows = account.signins || [];
  $("signins").innerHTML = rows.length ? rows.slice(0, 8).map(r => `<li><b>${escapeHtml(r.device || "Unknown browser")}</b>
      <span>${escapeHtml(fmtWhen(r.at))} · ${escapeHtml(r.how)}</span></li>`).join("")
    : `<li><span>No sign-ins recorded yet. New ones show up here.</span></li>`;
  $("signout-others").disabled = !account.other_browsers && !(account.ai_apps || []).length;
}

const sec = { steps: [], at: 0, resolve: null, data: {} };
function openSheet(steps, data = {}) {
  sec.steps = steps; sec.at = 0; sec.data = data;
  $("secure").hidden = false;
  renderSheet();
  return new Promise(resolve => { sec.resolve = resolve; });
}
function closeSheet(ok = false) {
  $("secure").hidden = true;
  const done = sec.resolve; sec.resolve = null;
  done?.(ok);
  if (meData?.online) loadAccount().then(renderSecurity);
}
function renderSheet() {
  const step = sec.steps[sec.at], body = $("sec-body"), next = $("sec-next");
  const counted = sec.steps.filter(s => !s.uncounted);
  $("sec-count").textContent = step.uncounted ? "" : `${counted.indexOf(step) + 1} of ${counted.length}`;
  $("sec-back").classList.toggle("gone", sec.at === 0 || !!step.noBack);
  next.textContent = step.next || "Continue";
  next.disabled = false;
  body.innerHTML = step.html(sec.data);
  step.after?.(sec.data);
  body.scrollTop = 0;
  const field = body.querySelector("input:not([type=checkbox])");
  if (field && !phone.matches) field.focus();
}
function sheetSay(msg) {
  let p = $("sec-body").querySelector(".form-msg");
  if (!p) { p = document.createElement("p"); p.className = "form-msg"; p.setAttribute("role", "alert"); $("sec-body").appendChild(p); }
  p.textContent = msg || ""; p.className = "form-msg" + (msg ? " bad" : "");
}
$("sec-next").onclick = async () => {
  const step = sec.steps[sec.at];
  $("sec-next").disabled = true;
  try {
    const go = step.go ? await step.go(sec.data) : true;
    if (go === "close") return closeSheet(true);
    if (go === false) return;
    if (sec.at === sec.steps.length - 1) return closeSheet(true);
    sec.at += typeof go === "number" ? go : 1;
    renderSheet();
  } catch (err) { sheetSay(err.message); }
  finally { $("sec-next").disabled = false; }
};
$("sec-back").onclick = () => { if (sec.at > 0) { sec.at--; renderSheet(); } };
$("sec-close").onclick = () => closeSheet(false);
$("sec-body").addEventListener("submit", e => { e.preventDefault(); $("sec-next").click(); });
$("secure").addEventListener("keydown", e => {
  if (e.key === "Escape") return closeSheet(false);
  if (e.key === "Enter" && e.target.tagName === "INPUT" && e.target.type !== "checkbox") { e.preventDefault(); $("sec-next").click(); }
});

const SEC_GLYPH = {
  lock: '<rect x="5" y="10.5" width="14" height="10" rx="2.5"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/>',
  phone: '<rect x="7" y="3" width="10" height="18" rx="2.5"/><path d="M11 17.5h2"/>',
  keys: '<circle cx="8" cy="12" r="3.5"/><path d="M11.5 12H20M17 12v3M20 12v2"/>',
};
const secGlyph = k => `<div class="setup-glyph"><svg viewBox="0 0 24 24">${SEC_GLYPH[k]}</svg></div>`;
const codeInput = (recovery) => recovery
  ? `<input type="text" id="sec-code" class="code-box" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="abcde-fghjk" aria-label="Recovery code">`
  : `<input type="text" id="sec-code" class="code-box" inputmode="numeric" autocomplete="one-time-code" maxlength="7" placeholder="123456" aria-label="6-digit code">`;
function codeStep(title, lede, go, opts = {}) {
  return { ...opts, html: d => `${secGlyph("phone")}<h1 id="sec-title">${title}</h1>
      <p class="lede">${d.recovery ? "Type one of your recovery codes. Each one works once." : lede}</p>
      <div class="block">${codeInput(d.recovery)}</div>
      ${opts.allowRecovery === false ? "" : `<p class="center"><button type="button" class="link" id="sec-swap">${d.recovery ? "Use my authenticator app instead" : "Lost your phone? Use a recovery code"}</button></p>`}`,
    after: d => $("sec-swap")?.addEventListener("click", () => { d.recovery = !d.recovery; renderSheet(); }),
    go: d => go(d, $("sec-code").value.trim()) };
}
const passwordStep = (title, lede, go) => ({
  html: () => `${secGlyph("lock")}<h1 id="sec-title">${title}</h1><p class="lede">${lede}</p>
    <div class="block"><input type="text" name="username" autocomplete="username" value="${escapeHtml(account?.email || "")}" hidden>
      <input type="password" id="sec-pw" autocomplete="current-password" aria-label="Password" placeholder="Password"></div>`,
  go: d => go(d, $("sec-pw").value) });

// Step-up: password, then the code if two-step is on.
function reauthSteps(after = []) {
  const twoStep = !!account?.twofa?.on;
  const steps = [passwordStep("Confirm it's you", "Enter your Thread password to keep going.", async (d, pw) => {
    d.password = pw;
    await send("POST", "/api/reauth", { password: pw });   // replies need_code when two-step is on
    return true;
  })];
  if (twoStep) steps.push(codeStep("Enter your code", "From your authenticator app.", async (d, code) => {
    await send("POST", "/api/reauth", { password: d.password, code });
    return true;
  }));
  return steps.concat(after);
}
// Run fn, handling a reauth error by prompting and retrying. Resolves to undefined if cancelled.
async function confirmedFirst(fn) {
  try { return await fn(); }
  catch (err) {
    if (!err.reauth) throw err;
    if (!account) await loadAccount();
    if (!(await openSheet(reauthSteps()))) return undefined;
    return await fn();
  }
}

async function startTwoStep() {
  await loadAccount();
  const setup = [
    { html: d => `${secGlyph("phone")}<h1 id="sec-title">Add Thread to your authenticator</h1>
        <p class="lede">${phone.matches ? "Tap the button to add Thread to Apple Passwords or your authenticator app." : "Scan this with your phone's camera or authenticator app (Apple Passwords, Google Authenticator, 1Password)."}</p>
        <div class="block center">
          ${phone.matches ? `<a class="primary big" href="${escapeHtml(d.uri)}">Add to authenticator app</a>` : ""}
          <img class="qr" src="${d.qr}" alt="QR code for your authenticator app" width="188" height="188">
          <p class="hint">Can't scan it? Type this key in your app:</p>
          <div class="copy-row key-row"><code id="sec-key">${escapeHtml(d.key)}</code><button class="text-btn" type="button" id="sec-copy-key">Copy</button></div>
        </div>`,
      after: () => $("sec-copy-key").onclick = async () => {
        try { await navigator.clipboard.writeText($("sec-key").textContent.replace(/ /g, "")); toast("Key copied."); } catch {}
      } },
    codeStep("Enter the 6-digit code", "The one your app shows for Thread, to check it's set up right.", async (d, code) => {
      const r = await send("POST", "/api/2fa/confirm", { code });
      d.codes = r.codes; account = { online: true, ...r }; return true;
    }, { allowRecovery: false }),
    recoveryCodesStep(),
  ];
  const start = async d => Object.assign(d, await send("POST", "/api/2fa/start", {}));
  const data = {};
  try { await start(data); }
  catch (err) {
    if (!err.reauth) return toast(err.message);
    // Step-up first, then the setup screens in the same sheet.
    const steps = reauthSteps();
    const last = steps[steps.length - 1], go = last.go;
    last.go = async d => { await go(d); await start(d); return true; };
    if (await openSheet(steps.concat(setup), data)) toast("Two-step sign-in is on.");
    return;
  }
  if (await openSheet(setup, data)) toast("Two-step sign-in is on.");
}
function recoveryCodesStep() {
  return { noBack: true, next: "Done",
    html: d => `${secGlyph("keys")}<h1 id="sec-title">Save your recovery codes</h1>
      <p class="lede">If you lose your phone, each code gets you in once. Keep them in your password manager or somewhere safe. You won't see them again.</p>
      <div class="block"><ol class="codes" id="sec-codes">${d.codes.map(c => `<li><code>${escapeHtml(c)}</code></li>`).join("")}</ol>
        <div class="row-btns center"><button class="text-btn" type="button" id="sec-copy-codes">Copy</button>
          <button class="text-btn" type="button" id="sec-save-codes">Download .txt</button></div>
        <label class="check"><input type="checkbox" id="sec-saved"> I saved them</label></div>`,
    after: d => {
      const text = `Thread recovery codes for ${account?.email || "your account"}\nEach works once.\n\n${d.codes.join("\n")}\n`;
      $("sec-copy-codes").onclick = async () => { try { await navigator.clipboard.writeText(text); toast("Codes copied."); } catch {} };
      $("sec-save-codes").onclick = () => {
        const a = document.createElement("a");
        a.href = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
        a.download = "thread-recovery-codes.txt"; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
      };
    },
    go: () => { if (!$("sec-saved").checked) throw new Error("Save them first, then tick “I saved them”."); return true; } };
}
async function stopTwoStep() {
  await loadAccount();
  const steps = [
    passwordStep("Turn off two-step sign-in?", "Then only your password protects Thread. Enter it to continue.", async (d, pw) => { d.password = pw; return true; }),
    codeStep("Enter your code", "One last check: the code from your authenticator app.", async (d, code) => {
      try { account = { online: true, ...(await send("POST", "/api/2fa/off", { password: d.password, code })) }; }
      catch (err) { if (err.field === "password") { sec.at = 0; renderSheet(); } throw err; }
      return true;
    }, { next: "Turn off" }),
  ];
  if (await openSheet(steps)) toast("Two-step sign-in is off.");
}
async function newRecoveryCodes() {
  const data = {};
  const make = async d => { const r = await send("POST", "/api/2fa/recovery", {}); d.codes = r.codes; account = { online: true, ...r }; };
  try { await make(data); }
  catch (err) {
    if (!err.reauth) return toast(err.message);
    const steps = reauthSteps(), last = steps[steps.length - 1], go = last.go;
    last.go = async d => { await go(d); await make(d); return true; };
    await openSheet(steps.concat([recoveryCodesStep()]), data);
    return;
  }
  await openSheet([recoveryCodesStep()], data);
}
$("signout-others").onclick = async () => {
  try {
    account = { online: true, ...(await send("POST", "/api/signout-others", { apps: true })) };
    renderSecurity();
    toast("Signed out everywhere else. Reconnect Claude or ChatGPT when you next use them.");
  } catch (err) { toast(err.message); }
};
$("export-link").addEventListener("click", async e => {
  if (!meData?.online) return;
  e.preventDefault();
  await loadAccount();
  if (!account?.fresh && !(await openSheet(reauthSteps()))) return;
  location.href = "/api/export";
});

$("copy-mcp").onclick = async () => {
  try { await navigator.clipboard.writeText($("mcp-url").textContent); toast("Copied."); }
  catch { toast("Couldn't copy. Select the address and copy it by hand."); }
};

$("saving").addEventListener("change", async e => {
  try {
    meData.saving = (await send("POST", "/api/saving", { mode: e.target.value })).saving;
    toast({ everything: "Thread will save everything meaningful.", important: "Thread will save only important things.",
            ask: "Thread will save only when you ask." }[meData.saving]);
  } catch (err) { toast(err.message); renderSettings(); }
});

$("privacy").addEventListener("change", async e => {
  try {
    meData.privacy = (await send("POST", "/api/privacy", { level: e.target.value })).privacy;
    toast(meData.privacy === "light" ? "Privacy set to Light." : "Privacy set to Strict.");
  } catch (err) { toast(err.message); renderSettings(); }
});

// Theme is per browser.
function savedTheme() { try { return localStorage.getItem("thread.theme") || "system"; } catch { return "system"; } }
function applyTheme(theme) {
  if (theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  const bg = getComputedStyle(document.body).getPropertyValue("--bg").trim();
  themeChanged();   // canvas re-reads colours
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", bg);
}
function markTheme() {
  const t = savedTheme();
  for (const b of $("theme").children) b.setAttribute("aria-checked", b.dataset.theme === t);
}
$("theme").addEventListener("click", e => {
  const b = e.target.closest("button[data-theme]");
  if (!b) return;
  try { localStorage.setItem("thread.theme", b.dataset.theme); } catch {}
  applyTheme(b.dataset.theme);
  markTheme();
});
applyTheme(savedTheme());
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme(savedTheme()));

// Help
const FAQ = [
  ["What is Thread?", "Thread is a memory you own. Your AI (Claude, ChatGPT, and others) saves what matters into plain notes here, so every chat starts already knowing you, and you never have to repeat yourself."],
  ["How does my AI learn about me?", "When you connect Thread, your AI saves important things you tell it (deadlines, goals, preferences) as notes. It also notices how you like to work and how you talk, and keeps that under \"What your AI has learned\" on your Profile."],
  ["Can I see and change everything it saved?", "Yes, all of it. Every note is on the Map and in the sidebar. Open any note and press Edit. On your Profile, remove anything under \"What your AI has learned\" that's wrong."],
  ["What does Thread save?", "By default, everything meaningful from your chats: what you're working on, plans, deadlines, and things you figured out with your AI, so every AI you use remembers it. The first time you chat, your AI asks how much you want saved. Change it anytime in Settings → What Thread saves: Everything, Only important things, or Only when I say."],
  ["What's the difference between Strict and Light privacy?", "Strict: your AI won't save money, health, family details, grades, or other people's names unless you ask. Light: it can save anything lasting that helps it know you. On both, passwords, keys, and card numbers are never saved."],
  ["How do I connect Claude or ChatGPT?", "Go to Settings → Connect your AI and copy the address. In Claude (any plan, even free), add it as a custom connector. In ChatGPT (Plus or higher), turn on Developer mode, add it as a new app, and pick Thread from + → Developer mode in a chat. It asks for your Thread email and password once."],
  ["Where is my data stored?", "In plain Markdown files, in your own folder on the Thread server that no other account can open. Thread doesn't sell or share it. Like any website, the people who run the server could technically open the files, so download it all anytime from Settings → Your data."],
  ["I deleted a ChatGPT or Claude chat. Can I get it back?", "Yes, for 10 days after Thread first saved it, if the Thread Backup extension was on. Open Me → Chats, find it, and press Copy all to paste it into a new chat (or Download to keep it). After 10 days the copy deletes itself for good."],
  ["I deleted a note by accident. Can I get it back?", "Yes. Deleted notes go to the trash first. Press Undo on the message that pops up right after deleting."],
  ["How do I move around the map?", "Drag a dot to move it (its neighbors follow). Scroll or pinch to zoom, and drag empty space to pan. Click a dot to open it, or hover to see what it links to."],
  ["Keyboard shortcuts", "/ search · N new note · [ show or hide the sidebar · Alt ← / Alt → back and forward · Cmd/Ctrl+S save · Esc close"],
  ["How do I report a bug or suggest an idea?", "Go to Settings → Report a bug or idea, or just tell your AI (\"report a Thread bug: …\"); it writes it up, checks it with you, and sends it. Reports go straight to the person building Thread. They include your screen size and browser, never your notes."],
  ["I forgot my password.", "Message the person who invited you. They'll send you a temporary password; sign in with it, then change it under Settings → Account."],
];
function renderFaq() {
  if ($("faq").childElementCount) return;
  $("faq").innerHTML = FAQ.map(([q, a]) => `<details><summary>${escapeHtml(q)}</summary><p>${escapeHtml(a)}</p></details>`).join("");
}

// =====================================================================
// Search (filters map and sidebar)
// =====================================================================
function applySearch() {
  const q = searchTerm.toLowerCase();
  const keep = q ? new Set(nodes.filter(n => displayTitle(n).toLowerCase().includes(q) || n.id.toLowerCase().includes(q)).map(n => n.id)) : null;
  searchKeep = keep;
  requestDraw();
}
const searchBox = $("search");
searchBox.addEventListener("input", () => { searchTerm = searchBox.value.trim(); applySearch(); renderTree(); });

// =====================================================================
// Keyboard
// =====================================================================
addEventListener("keydown", async e => {
  if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); await saveIfDirty(); return; }
  if (e.key === "Escape") {
    hideMenu();
    if (document.activeElement === searchBox) { searchBox.value = ""; searchTerm = ""; applySearch(); renderTree(); searchBox.blur(); }
    else if (editing) { await saveIfDirty(); await showNote(active, false); }
    else if (localRoot && active === "map") { localRoot = null; applyLocal(); fit(); recordNav(); }
    return;
  }
  if (typing()) return;
  if (e.key === "/") { e.preventDefault(); if (!document.body.classList.contains("sidebar-open")) toggleSidebar(true); searchBox.focus(); }
  if (e.key === "[") toggleSidebar();
  if (e.altKey && e.key === "ArrowLeft") $("back").click();
  if (e.altKey && e.key === "ArrowRight") $("forward").click();
  if (e.key === "n") { e.preventDefault(); startCreate("note", selectedFolder); }
});
// Warn before closing with unsaved edits.
addEventListener("beforeunload", e => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });

// =====================================================================
// Live updates
// =====================================================================
async function refresh() {
  const json = await (await fetch("/api/graph")).text();
  if (json === lastJson) return;
  lastJson = json;
  setGraph(JSON.parse(json));
  await loadTree();
  renderTabs();
  if (!PLACES.includes(active) && !editing) showNote(active);
}

// Time zone
// Never change the saved zone silently; ask if the device is elsewhere.
let tzInfo = null, zoneAfterSetup = false;
function deviceZone() { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch { return ""; } }
async function checkZone() {
  const tz = deviceZone();
  if (!tz) return;
  try { tzInfo = await (await fetch("/api/timezone?browser=" + encodeURIComponent(tz))).json(); } catch { return; }
  if (!tzInfo.differs && !tzInfo.missing) return;
  const key = `tzkeep.${tzInfo.timezone || "none"}|${tzInfo.browser}`;   // "Keep" is remembered per pair
  if (store.get(key, false) || document.querySelector(".tz-notice")) return;
  if (!$("setup")?.hidden) { zoneAfterSetup = true; return; }   // ask after setup
  const there = tzInfo.browser_city, here = tzInfo.city;
  const box = document.createElement("div");
  box.className = "tz-notice"; box.setAttribute("role", "status");
  box.innerHTML = tzInfo.differs
    ? `<p>Your device says <b>${escapeHtml(there)}</b> time, but Thread uses <b>${escapeHtml(here)}</b> time.</p>
       <div class="tz-btns"><button type="button" class="primary" data-act="switch">Switch to ${escapeHtml(there)}</button>
       <button type="button" data-act="keep">Keep ${escapeHtml(here)}</button></div>`
    : `<p>Thread doesn't know your time zone yet. Use <b>${escapeHtml(there)}</b> time?</p>
       <div class="tz-btns"><button type="button" class="primary" data-act="switch">Use ${escapeHtml(there)}</button>
       <button type="button" data-act="keep">Not now</button></div>`;
  box.onclick = async e => {
    const act = e.target.closest("button")?.dataset.act;
    if (act === "keep") { store.set(key, true); box.remove(); send("POST", "/api/timezone", { keep: tzInfo.browser }).catch(() => {}); }
    if (act === "switch") {
      try {
        await send("POST", "/api/timezone", { timezone: tzInfo.browser });
        box.remove(); dueChanged(); if (active === "home") renderHome();
        toast(`Thread now uses ${there} time. You can change it in your profile.`);
        if (meData) { meData = await (await fetch("/api/profile")).json(); renderProfile(); }
      } catch (err) { toast(err.message); }
    }
  };
  document.body.appendChild(box);
}

// Time zone picker grouped by region, with search.
function zonePicker(id, selected, attrs = "") {
  const groups = meData?.schema?.timezones || [];
  const laptop = setupState ? !setupState.online : !tzInfo?.missing && !tzInfo?.timezone;
  const blank = `<option value="" ${laptop ? "" : "disabled"} ${selected ? "" : "selected"}>${laptop ? "This computer's time zone" : "Choose your time zone"}</option>`;
  return `<div class="tz-pick">
      <input type="search" class="tz-search" data-for="${id}" placeholder="Search a city or country" autocomplete="off" aria-label="Search time zones">
      <select id="${id}" ${attrs}>${blank}${groups.map(g => `<optgroup label="${escapeHtml(g.region)}">${g.zones.map(z =>
        `<option value="${escapeHtml(z.id)}" ${z.id === selected ? "selected" : ""}>${escapeHtml(z.label)}</option>`).join("")}</optgroup>`).join("")}</select>
    </div>`;
}
document.addEventListener("input", e => {
  if (!e.target.matches(".tz-search")) return;
  const sel = $(e.target.dataset.for), q = e.target.value.trim().toLowerCase();
  let first = null;
  for (const o of sel.querySelectorAll("option")) {
    if (!o.value) continue;
    const hit = !q || o.textContent.toLowerCase().includes(q) || o.value.toLowerCase().includes(q);
    o.hidden = !hit; if (hit && !first) first = o;
  }
  for (const g of sel.querySelectorAll("optgroup")) g.hidden = ![...g.children].some(o => !o.hidden);
  if (q && first && sel.value !== first.value) { sel.value = first.value; sel.dispatchEvent(new Event("input", { bubbles: true })); }
});

(async () => {
  try { classColorPicks = await (await fetch("/api/class-colors")).json(); } catch {}
  await refresh();
  // restore the last tab if it still exists
  tabs = tabs.filter(t => t === "map" || byId.has(t));
  // New accounts (<10 notes) start with the sidebar closed.
  if (!phone.matches && !store.get("sidebar-chosen", false) && nodes.filter(n => !n.unresolved).length < 10)
    document.body.classList.remove("sidebar-open");
  restoring = true;
  await activate("home");
  restoring = false;
  // Keep earlier history entries from this session.
  navMax = +sessionStorage.getItem("thread.navMax") || 0;
  const startOnMe = location.hash.startsWith("#me");   // e.g. returning from Google OAuth
  if (window.history.state?.loc) navIndex = window.history.state.i;
  else window.history.replaceState({ loc: here(), i: 0 }, "", "#home");
  const lowCodes = new URLSearchParams(location.search).has("recovery-low");
  if (startOnMe) { mePanel = new URLSearchParams(location.search).has("google") || lowCodes ? "settings" : mePanel; await openMe(mePanel); }
  if (lowCodes) {
    window.history.replaceState(window.history.state, "", location.pathname + location.hash);
    toast("You're running low on recovery codes. Make new ones under Two-step sign-in.");
    $("twofa-card")?.scrollIntoView({ block: "center" });
  }
  if (window.history.state.loc.tab !== "home") await goTo(window.history.state.loc, -1);
  updateNav();
  setInterval(refresh, 4000);
  const welcome = new URLSearchParams(location.search).get("welcome");
  if (welcome) {
    window.history.replaceState(window.history.state, "", location.pathname + location.hash);
    openSetup("about", +welcome || 0);   // continue sign-up's step count
  } else if (!startOnMe) loadSetup().then(s => { if (s?.online && !s.seen) openSetup("about"); });
  checkZone();
})();
