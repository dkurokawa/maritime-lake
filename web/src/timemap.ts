import { geoAzimuthalEquidistant, geoDistance, geoStream } from "d3-geo";
import { Delaunay } from "d3-delaunay";
import { feature } from "topojson-client";
import type { Topology } from "topojson-specification";
import landUrl from "world-atlas/land-50m.json?url";
import {
  fmtNum, loadContainerOD, loadContainerPorts, loadMeta,
  type ContainerOD, type ContainerPort,
} from "./data";

// ---------------------------------------------------------------------------
// Container time map: the world as a container sees it. Distance from the origin
// port = sailing time. Geography and time share one scale (nautical miles), so
// the toggle only moves points radially; bearings never change.
// ---------------------------------------------------------------------------

const NM_PER_RAD = 10800 / Math.PI; // 1 nm = 1 arc minute of great circle
const RING_NM = 10800; // great-circle radius of the boundary anchor ring
const ANCHORS = 24;
const PRESETS = ["port1188", "port1201", "port1114", "port664", "port1305"]; // Shanghai, Singapore, Rotterdam, LA-LB, Tokyo
const ROTTERDAM = "port1114";
const DEFAULT_ORIGIN = "port1188";
const TWEEN_MS = 1200;

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

interface Land {
  /** projected (nm, origin-centred) vertices, x,y interleaved */
  geo: Float32Array;
  /** same vertices after the piecewise-affine warp into time space */
  time: Float32Array;
  starts: Uint32Array; // ring start offsets in vertices, plus a final end offset
}

interface Layout {
  originId: string;
  scenario: string;
  geoXY: Float32Array; // per port (OD order)
  timeXY: Float32Array;
  hasPath: boolean[];
  gcNm: number[];
  sailNm: (number | null)[];
  land: Land;
  viewNm: number;
}

interface Model {
  ports: ContainerPort[]; // OD order
  od: ContainerOD;
  index: Map<string, number>;
  topo: Topology;
}

let model: Model | null = null;
let layout: Layout | null = null;
let originId = DEFAULT_ORIGIN;
let scenario = "open";
let t = 1; // 0 = geography, 1 = sailing time
let target = 1;
let raf = 0;
let hover = -1;
let armed = -1; // touch: port whose tooltip is open; a second tap makes it the origin
let started = false;
let bound = false;
let foldTimer = 0; // arrival fold into sailing time; cancelled by any explicit toggle or hide
let canvas: HTMLCanvasElement;
let ctx: CanvasRenderingContext2D;
let cssW = 0;
let cssH = 0;
let topLabels: Set<number> = new Set();

const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const ease = (x: number) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2);
const days = (nm: number) => nm / (model!.od.speed_kn * 24);

// ---------- geometry ----------

function projectLand(topo: Topology, lon0: number, lat0: number): { xy: Float32Array; starts: Uint32Array } {
  const land = feature(topo, topo.objects.land as never);
  const proj = geoAzimuthalEquidistant().rotate([-lon0, -lat0]).scale(NM_PER_RAD).translate([0, 0]).precision(25);
  const xs: number[] = [];
  const starts: number[] = [];
  geoStream(land as never, proj.stream({
    point(x: number, y: number) { xs.push(x, y); },
    lineStart() { starts.push(xs.length / 2); },
    lineEnd() {},
    polygonStart() {},
    polygonEnd() {},
    sphere() {},
  }));
  starts.push(xs.length / 2);
  return { xy: Float32Array.from(xs), starts: Uint32Array.from(starts) };
}

/** Piecewise-affine warp: triangulate control points in geography, map by barycentric coordinates. */
class Warp {
  private del: Delaunay<[number, number]>;
  private tri: Uint32Array;
  private incident: number[][];
  private gx: Float64Array;
  private gy: Float64Array;
  constructor(private geo: Float64Array, private time: Float64Array, private fallback: number) {
    const n = geo.length / 2;
    const pts: [number, number][] = [];
    for (let i = 0; i < n; i++) pts.push([geo[2 * i], geo[2 * i + 1]]);
    this.gx = geo.filter((_, i) => i % 2 === 0);
    this.gy = geo.filter((_, i) => i % 2 === 1);
    this.del = Delaunay.from(pts);
    this.tri = this.del.triangles;
    this.incident = Array.from({ length: n }, () => []);
    for (let k = 0; k < this.tri.length; k += 3) {
      for (let j = 0; j < 3; j++) this.incident[this.tri[k + j]].push(k);
    }
  }

  /** Best barycentric fit among candidate triangles: largest minimum coordinate. */
  private best(cands: Iterable<number>, x: number, y: number, bestK: number, bestMin: number, out: number[]) {
    const { tri, gx, gy } = this;
    for (const k of cands) {
      const a = tri[k], b = tri[k + 1], c = tri[k + 2];
      const det = (gy[b] - gy[c]) * (gx[a] - gx[c]) + (gx[c] - gx[b]) * (gy[a] - gy[c]);
      if (Math.abs(det) < 1e-9) continue; // degenerate sliver
      const l0 = ((gy[b] - gy[c]) * (x - gx[c]) + (gx[c] - gx[b]) * (y - gy[c])) / det;
      const l1 = ((gy[c] - gy[a]) * (x - gx[c]) + (gx[a] - gx[c]) * (y - gy[c])) / det;
      const l2 = 1 - l0 - l1;
      const m = Math.min(l0, l1, l2);
      if (m > bestMin) {
        bestMin = m;
        bestK = k;
        out[0] = l0; out[1] = l1; out[2] = l2;
      }
    }
    return { bestK, bestMin };
  }

  map(x: number, y: number, out: Float32Array, o: number, l: number[]) {
    const s = this.del.find(x, y);
    let r = this.best(this.incident[s], x, y, -1, -Infinity, l);
    if (r.bestMin < -1e-9) {
      const near: number[] = [];
      for (const nb of this.del.neighbors(s)) near.push(...this.incident[nb]);
      r = this.best(near, x, y, r.bestK, r.bestMin, l);
    }
    // outside the hull: r.bestK is the nearest triangle, the affine map extrapolates
    if (r.bestK >= 0) {
      const a = this.tri[r.bestK], b = this.tri[r.bestK + 1], c = this.tri[r.bestK + 2];
      const px = l[0] * this.time[2 * a] + l[1] * this.time[2 * b] + l[2] * this.time[2 * c];
      const py = l[0] * this.time[2 * a + 1] + l[1] * this.time[2 * b + 1] + l[2] * this.time[2 * c + 1];
      if (Number.isFinite(px) && Number.isFinite(py)) {
        out[o] = px; out[o + 1] = py;
        return;
      }
    }
    out[o] = x * this.fallback; // never NaN: radial scale by the median detour
    out[o + 1] = y * this.fallback;
  }
}

function median(v: number[]): number {
  const s = v.slice().sort((a, b) => a - b);
  return s.length ? s[Math.floor(s.length / 2)] : 1;
}

function computeLayout(m: Model, origin: string, sc: string): Layout {
  const oi = m.index.get(origin)!;
  const o = m.ports[oi];
  const proj = geoAzimuthalEquidistant().rotate([-o.lon, -o.lat]).scale(NM_PER_RAD).translate([0, 0]);
  const n = m.ports.length;
  const matrix = m.od.nm[sc];
  const geoXY = new Float32Array(2 * n);
  const timeXY = new Float32Array(2 * n);
  const hasPath: boolean[] = [];
  const gcNm: number[] = [];
  const sailNm: (number | null)[] = [];
  const ratios: number[] = [];
  for (let i = 0; i < n; i++) {
    const p = m.ports[i];
    const xy = proj([p.lon, p.lat]) ?? [0, 0];
    geoXY[2 * i] = xy[0];
    geoXY[2 * i + 1] = xy[1];
    const gc = geoDistance([o.lon, o.lat], [p.lon, p.lat]) * NM_PER_RAD;
    gcNm.push(gc);
    const sail = i === oi ? 0 : matrix[oi][i];
    sailNm.push(sail);
    const ok = sail != null;
    hasPath.push(ok);
    const r = Math.hypot(xy[0], xy[1]);
    if (ok && r > 1e-6) {
      const k = sail! / r; // same bearing, radius = sailing nm
      timeXY[2 * i] = xy[0] * k;
      timeXY[2 * i + 1] = xy[1] * k;
      if (gc > 300) ratios.push(sail! / gc);
    } else {
      timeXY[2 * i] = xy[0];
      timeXY[2 * i + 1] = xy[1];
    }
  }
  const ratio = median(ratios);

  // control points: origin, ports with a path, boundary anchors
  const cg: number[] = [0, 0];
  const ct: number[] = [0, 0];
  const seen: [number, number][] = [[0, 0]];
  for (let i = 0; i < n; i++) {
    if (i === oi || !hasPath[i]) continue;
    const x = geoXY[2 * i], y = geoXY[2 * i + 1];
    if (seen.some(([sx, sy]) => Math.hypot(sx - x, sy - y) < 2)) continue; // coincident sites break the triangulation
    seen.push([x, y]);
    cg.push(x, y);
    ct.push(timeXY[2 * i], timeXY[2 * i + 1]);
  }
  for (let k = 0; k < ANCHORS; k++) {
    const a = (2 * Math.PI * k) / ANCHORS;
    cg.push(RING_NM * Math.cos(a), RING_NM * Math.sin(a));
    ct.push(RING_NM * ratio * Math.cos(a), RING_NM * ratio * Math.sin(a));
  }
  const warp = new Warp(Float64Array.from(cg), Float64Array.from(ct), ratio);

  const { xy, starts } = projectLand(m.topo, o.lon, o.lat);
  const nv = xy.length / 2;
  const tm = new Float32Array(xy.length);
  const l = [0, 0, 0];
  for (let i = 0; i < nv; i++) warp.map(xy[2 * i], xy[2 * i + 1], tm, 2 * i, l);
  let bad = 0;
  for (let i = 0; i < tm.length; i++) {
    if (!Number.isFinite(tm[i])) { tm[i] = xy[i]; bad++; } // last line of defence: keep the geographic position
  }
  if (bad) console.warn(`time map: ${bad} non-finite warped coordinates replaced (${origin}, ${sc})`);

  let view = 6000;
  for (let i = 0; i < n; i++) {
    view = Math.max(view, Math.hypot(geoXY[2 * i], geoXY[2 * i + 1]), Math.hypot(timeXY[2 * i], timeXY[2 * i + 1]));
  }
  return { originId: origin, scenario: sc, geoXY, timeXY, hasPath, gcNm, sailNm, land: { geo: xy, time: tm, starts }, viewNm: view * 1.08 };
}

// ---------- drawing ----------

const COL = {
  bg: "#0b131b", land: "#22384c", landEdge: "#3a5873", ring: "rgba(230,237,243,.13)",
  ringText: "#93a1b0", port: "rgba(230,237,243,.88)", grey: "rgba(147,161,176,.75)", origin: "#5bb5e8", text: "#e6edf3", halo: "#0b131b",
};

function viewTransform() {
  const L = layout!;
  const k = (Math.min(cssW, cssH) / 2 - 10) / L.viewNm;
  return { k, cx: cssW / 2, cy: cssH / 2 };
}

function portXY(i: number, tt: number): [number, number] {
  const L = layout!;
  if (!L.hasPath[i]) return [L.geoXY[2 * i], L.geoXY[2 * i + 1]];
  return [
    L.geoXY[2 * i] + (L.timeXY[2 * i] - L.geoXY[2 * i]) * tt,
    L.geoXY[2 * i + 1] + (L.timeXY[2 * i + 1] - L.geoXY[2 * i + 1]) * tt,
  ];
}

function markerRadius(p: ContainerPort): number {
  return p.ma7_container == null ? 2.5 : Math.min(15, 2.2 + 1.15 * Math.sqrt(p.ma7_container));
}

function draw() {
  if (!layout || !model) return;
  const L = layout;
  const tt = ease(t);
  const { k, cx, cy } = viewTransform();
  ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
  ctx.fillStyle = COL.bg;
  ctx.fillRect(0, 0, cssW, cssH);

  // land: lerp per frame, skip vertices that land on the same pixel
  const { geo, time, starts } = L.land;
  ctx.beginPath();
  for (let r = 0; r < starts.length - 1; r++) {
    let lastX = NaN, lastY = NaN;
    const a = starts[r], b = starts[r + 1];
    for (let v = a; v < b; v++) {
      const x = cx + (geo[2 * v] + (time[2 * v] - geo[2 * v]) * tt) * k;
      const y = cy + (geo[2 * v + 1] + (time[2 * v + 1] - geo[2 * v + 1]) * tt) * k;
      if (v === a) { ctx.moveTo(x, y); lastX = x; lastY = y; continue; }
      const dx = x - lastX, dy = y - lastY;
      if (dx * dx + dy * dy < 0.64 && v < b - 1) continue;
      ctx.lineTo(x, y);
      lastX = x; lastY = y;
    }
    ctx.closePath();
  }
  ctx.fillStyle = COL.land;
  ctx.fill();
  ctx.lineWidth = 0.6;
  ctx.strokeStyle = COL.landEdge;
  ctx.stroke();

  // rings every 10 days
  const stepNm = 10 * 24 * model.od.speed_kn;
  ctx.lineWidth = 1;
  ctx.font = "11px system-ui, sans-serif";
  ctx.textAlign = "center";
  for (let d = 1; d * stepNm < L.viewNm * 1.02; d++) {
    const rad = d * stepNm * k;
    ctx.beginPath();
    ctx.arc(cx, cy, rad, 0, Math.PI * 2);
    ctx.strokeStyle = COL.ring;
    ctx.stroke();
    const label = d === 1 ? `10 days at ${model.od.speed_kn} kn` : `${d * 10} days`;
    drawText(label, cx, cy + rad + 12, COL.ringText, "center", true); // below the origin: least port traffic
  }

  // ports
  const oi = model.index.get(L.originId)!;
  const boxes: [number, number, number, number][] = [];
  const order = model.ports.map((_, i) => i).sort((a, b) => markerRadius(model!.ports[b]) - markerRadius(model!.ports[a]));
  for (const i of order) {
    const [px, py] = portXY(i, tt);
    const x = cx + px * k, y = cy + py * k;
    const p = model.ports[i];
    const r = markerRadius(p);
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    if (i === oi) {
      ctx.fillStyle = COL.origin;
      ctx.fill();
      ctx.lineWidth = 2;
      ctx.strokeStyle = "#fff";
      ctx.stroke();
    } else if (!L.hasPath[i]) {
      ctx.lineWidth = 1.2;
      ctx.strokeStyle = COL.grey;
      ctx.setLineDash([2, 2]);
      ctx.stroke();
      ctx.setLineDash([]);
    } else {
      ctx.fillStyle = COL.port;
      ctx.globalAlpha = 0.8;
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.lineWidth = 1;
      ctx.strokeStyle = COL.halo;
      ctx.stroke();
    }
    if (i === hover) {
      ctx.beginPath();
      ctx.arc(x, y, r + 3, 0, Math.PI * 2);
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = "#fff";
      ctx.stroke();
    }
  }
  // labels: presets and the top 15, greedy collision avoidance; origin and hover first
  const labelOrder = [oi, hover, ...model.ports.map((_, i) => i).filter((i) => topLabels.has(i))];
  ctx.font = "12px system-ui, sans-serif";
  for (const i of labelOrder) {
    if (i < 0) continue;
    const [px, py] = portXY(i, tt);
    const x = cx + px * k, y = cy + py * k;
    const name = model.ports[i].portname;
    const w = ctx.measureText(name).width;
    const r = markerRadius(model.ports[i]);
    let lx = x + r + 4;
    if (lx + w > cssW - 4) lx = x - r - 4 - w;
    const box: [number, number, number, number] = [lx, y - 7, lx + w, y + 7];
    if (boxes.some((b) => box[0] < b[2] && box[2] > b[0] && box[1] < b[3] && box[3] > b[1])) continue;
    boxes.push(box);
    drawText(name, lx, y + 4, i === oi ? "#fff" : COL.text, "left", true);
  }
}

if (import.meta.env.DEV) (window as unknown as { __tmDraw: () => void }).__tmDraw = draw; // dev-only: frame timing

function drawText(s: string, x: number, y: number, fill: string, align: CanvasTextAlign, halo: boolean) {
  ctx.textAlign = align;
  if (halo) {
    ctx.lineWidth = 3;
    ctx.strokeStyle = COL.halo;
    ctx.lineJoin = "round";
    ctx.strokeText(s, x, y);
  }
  ctx.fillStyle = fill;
  ctx.fillText(s, x, y);
}

// ---------- state ----------

function setHash() {
  history.replaceState(null, "", `#time=${originId},${scenario}`);
}

function refresh() {
  if (!model) return;
  layout = computeLayout(model, originId, scenario);
  hover = -1;
  armed = -1;
  $("tm-tip").hidden = true;
  $<HTMLSelectElement>("tm-origin").value = originId;
  $<HTMLSelectElement>("tm-scenario").value = scenario;
  updateNote();
  setHash();
  draw();
}

function fmtDays(nm: number | null): string {
  return nm == null ? "no sea path" : `${fmtNum(days(nm), 1)} days`;
}

function updateNote() {
  const note = $("tm-note");
  const m = model!;
  const oi = m.index.get(originId)!;
  const ri = m.index.get(ROTTERDAM);
  const sc = m.od.scenarios.find((s) => s.id === scenario)!;
  const name = m.ports[oi].portname;
  if (scenario === "open" || ri == null || ri === oi) {
    note.textContent = scenario === "open"
      ? `Distance from ${name} = sailing time at ${m.od.speed_kn} kn. Click a port to make it the origin.`
      : `${sc.label}: sailing times from ${name}.`;
    return;
  }
  const a = m.od.nm.open[oi][ri];
  const b = m.od.nm[scenario][oi][ri];
  const rot = m.ports[ri].portname;
  if (a == null || b == null) {
    note.textContent = `${name} to ${rot}: ${fmtDays(a)} with all canals open, ${fmtDays(b)} with ${sc.label}.`;
  } else {
    const diff = days(b) - days(a);
    note.textContent = `${name} to ${rot}: ${fmtDays(a)} with all canals open, ${fmtDays(b)} with ${sc.label} (${diff >= 0 ? "+" : "-"}${fmtNum(Math.abs(diff), 1)} days).`;
  }
}

function setTarget(to: 0 | 1) {
  window.clearTimeout(foldTimer);
  target = to;
  $("tm-geo").setAttribute("aria-pressed", String(to === 0));
  $("tm-time").setAttribute("aria-pressed", String(to === 1));
  cancelAnimationFrame(raf);
  if (reducedMotion()) {
    t = to;
    draw();
    return;
  }
  const from = t;
  const t0 = performance.now();
  const step = (now: number) => {
    const u = Math.min(1, (now - t0) / TWEEN_MS);
    t = from + (to - from) * u;
    draw();
    if (u < 1) raf = requestAnimationFrame(step);
  };
  raf = requestAnimationFrame(step);
}

function resize() {
  const stage = canvas.parentElement!;
  cssW = stage.clientWidth;
  cssH = stage.clientHeight;
  canvas.width = Math.round(cssW * devicePixelRatio);
  canvas.height = Math.round(cssH * devicePixelRatio);
  canvas.style.width = `${cssW}px`;
  canvas.style.height = `${cssH}px`;
  draw();
}

function nearest(ev: PointerEvent): number {
  if (!layout || !model) return -1;
  const rect = canvas.getBoundingClientRect();
  const mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
  const { k, cx, cy } = viewTransform();
  const tt = ease(t);
  let best = -1, bd = ev.pointerType === "touch" ? 26 : 14;
  for (let i = 0; i < model.ports.length; i++) {
    const [px, py] = portXY(i, tt);
    const d = Math.hypot(cx + px * k - mx, cy + py * k - my) - markerRadius(model.ports[i]) * 0.6;
    if (d < bd) { bd = d; best = i; }
  }
  return best;
}

function showTip(i: number, ev: PointerEvent) {
  const tip = $("tm-tip");
  if (i < 0 || !layout || !model) { tip.hidden = true; return; }
  const p = model.ports[i];
  const L = layout;
  const lines = [`<strong>${p.portname}</strong>${p.country ? `, ${p.country}` : ""}`];
  if (p.portid === L.originId) {
    lines.push("Origin");
  } else if (!L.hasPath[i]) {
    lines.push("No sea path under this scenario (drawn at its geographic position)");
  } else {
    const sail = L.sailNm[i]!;
    const detour = L.gcNm[i] > 1 ? Math.max(0, (sail / L.gcNm[i] - 1) * 100) : 0;
    lines.push(`Sailing: ${fmtNum(days(sail), 1)} days`);
    lines.push(`Great circle: ${fmtNum(days(L.gcNm[i]), 1)} days`);
    lines.push(`Detour: +${fmtNum(detour, 0)}%`);
  }
  if (p.ma7_container != null) lines.push(`Container calls: ${fmtNum(p.ma7_container, 1)} / day (7-day mean)`);
  if (ev.pointerType === "touch" && p.portid !== L.originId && L.hasPath[i]) lines.push("Tap again to set as origin");
  tip.innerHTML = lines.join("<br>");
  tip.hidden = false;
  const rect = canvas.getBoundingClientRect();
  const stage = canvas.parentElement!.getBoundingClientRect();
  let x = ev.clientX - stage.left + 14;
  let y = ev.clientY - stage.top + 14;
  const w = tip.offsetWidth, h = tip.offsetHeight;
  if (x + w > rect.width - 4) x = ev.clientX - stage.left - w - 14;
  if (y + h > rect.height - 4) y = ev.clientY - stage.top - h - 14;
  tip.style.left = `${Math.max(4, x)}px`;
  tip.style.top = `${Math.max(4, y)}px`;
}

function bind() {
  canvas = $("tm-canvas") as HTMLCanvasElement;
  ctx = canvas.getContext("2d")!;
  new ResizeObserver(resize).observe(canvas.parentElement!);
  canvas.addEventListener("pointermove", (ev) => {
    if (ev.pointerType === "touch") return;
    const i = nearest(ev);
    canvas.style.cursor = i >= 0 ? "pointer" : "";
    if (i !== hover) { hover = i; draw(); }
    showTip(i, ev);
  });
  canvas.addEventListener("pointerleave", () => { hover = -1; $("tm-tip").hidden = true; draw(); });
  canvas.addEventListener("click", (ev) => {
    const pe = ev as unknown as PointerEvent;
    const i = nearest(pe);
    if (i < 0 || !model) { armed = -1; hover = -1; $("tm-tip").hidden = true; draw(); return; }
    const touch = (pe as PointerEvent).pointerType === "touch";
    if (touch && armed !== i) {
      armed = i; hover = i; draw(); showTip(i, pe);
      return;
    }
    const p = model.ports[i];
    if (p.portid !== originId && layout?.hasPath[i]) { originId = p.portid; refresh(); }
  });
  $("tm-origin").addEventListener("change", (e) => { originId = (e.target as HTMLSelectElement).value; refresh(); });
  $("tm-scenario").addEventListener("change", (e) => { scenario = (e.target as HTMLSelectElement).value; refresh(); });
  $("tm-geo").addEventListener("click", () => setTarget(0));
  $("tm-time").addEventListener("click", () => setTarget(1));
}

async function load() {
  const [ports, od, meta, topo] = await Promise.all([
    loadContainerPorts(), loadContainerOD(), loadMeta(),
    fetch(landUrl).then((r) => { if (!r.ok) throw new Error(`land: HTTP ${r.status}`); return r.json() as Promise<Topology>; }),
  ]);
  const byId = new Map(ports.map((p) => [p.portid, p]));
  const ordered = od.ports.map((id) => byId.get(id)).filter((p): p is ContainerPort => !!p);
  if (ordered.length !== od.ports.length) throw new Error("container_ports.json and container_od.json disagree on ports");
  model = { ports: ordered, od, index: new Map(od.ports.map((id, i) => [id, i])), topo };
  topLabels = new Set(ordered.map((p, i) => (PRESETS.includes(p.portid) || (p.rank != null && p.rank <= 15) ? i : -1)).filter((i) => i >= 0));

  const sel = $<HTMLSelectElement>("tm-origin");
  const opt = (p: ContainerPort) => `<option value="${p.portid}">${p.portname}${p.country ? ` (${p.country})` : ""}</option>`;
  const presets = PRESETS.map((id) => byId.get(id)).filter((p): p is ContainerPort => !!p);
  const rest = ordered.filter((p) => !PRESETS.includes(p.portid)).sort((a, b) => a.portname.localeCompare(b.portname));
  sel.innerHTML = `<optgroup label="Presets">${presets.map(opt).join("")}</optgroup><optgroup label="All ports A-Z">${rest.map(opt).join("")}</optgroup>`;
  $("tm-scenario").innerHTML = od.scenarios.map((s) => `<option value="${s.id}">${s.label}</option>`).join("");
  $("tm-fine").innerHTML =
    `Distance on the map is sailing time from the origin at ${od.speed_kn} knots (nautical miles / (${od.speed_kn} x 24) = days); the speed is an assumption, not data. ` +
    `Routing is a modelled shortest sea path computed with <a href="https://github.com/genthalili/searoute-py" target="_blank" rel="noopener">searoute-py</a> (Apache-2.0), not actual liner schedules, ports of call or waiting time. ` +
    `Marker area is the 7-day mean of container port calls (PortWatch estimates from AIS data, may be revised). ` +
    `Land: Natural Earth (public domain). ${meta.attribution}`;
}

function parseHash(spec?: string) {
  if (!spec || !model) return;
  const [p, s] = spec.split(",");
  if (p && model.index.has(p)) originId = p;
  if (s && model.od.scenarios.some((x) => x.id === s)) scenario = s;
}

/** Show the time map. `spec` is the hash payload after `#time=` (`<portid>,<scenario>`). */
export async function showTimeMap(spec?: string) {
  $("timemap").hidden = false;
  const msg = $("tm-msg");
  if (!started) {
    started = true;
    if (!bound) { bind(); bound = true; } // a failed load retries without re-binding listeners
    try {
      await load();
    } catch (err) {
      console.error(err);
      started = false;
      msg.textContent = "Could not load the container data. Run `uv run mlake all` first.";
      msg.hidden = false;
      return;
    }
  }
  msg.hidden = true;
  parseHash(spec);
  resize();
  refresh();
  // arrive in geography, then fold the map into sailing time
  t = 0;
  $("tm-geo").setAttribute("aria-pressed", "true");
  $("tm-time").setAttribute("aria-pressed", "false");
  draw();
  window.clearTimeout(foldTimer);
  foldTimer = window.setTimeout(() => setTarget(1), reducedMotion() ? 0 : 500);
}

export function hideTimeMap() {
  window.clearTimeout(foldTimer);
  cancelAnimationFrame(raf);
  $("timemap").hidden = true;
}

export function updateTimeMapSpec(spec: string) {
  if (!model || $("timemap").hidden) return;
  parseHash(spec);
  refresh();
}
