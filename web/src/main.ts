import * as maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import "./style.css";
import {
  fmtNum, fmtPct, loadEvents, loadGeo, loadMeta, loadSeries,
  type ChokepointProps, type EventItem,
} from "./data";
import { indexChart, totalChart, typeChart, type ChartHandle, type IndexLine } from "./charts";

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

const STORY = {
  event: "red-sea-houthi-attacks",
  lines: [
    { id: "chokepoint4", name: "Bab el-Mandeb Strait", color: "#e8825b" },
    { id: "chokepoint1", name: "Suez Canal", color: "#e8c468" },
    { id: "chokepoint7", name: "Cape of Good Hope", color: "#5bb5e8" },
  ],
};

let map: maplibregl.Map;
let current: { id: string; chart?: ChartHandle } | null = null;
let mode: "total" | "type" = "total";
let storyChart: ChartHandle | null = null;
let geoProps = new Map<string, ChokepointProps>();

function showView(view: "map" | "story") {
  $("tab-map").setAttribute("aria-selected", String(view === "map"));
  $("tab-story").setAttribute("aria-selected", String(view === "story"));
  $("story").hidden = view !== "story";
  $("legend").style.display = view === "story" ? "none" : "";
  if (view === "story") {
    $("panel").hidden = true;
    void renderStory();
  } else {
    map.resize();
  }
  history.replaceState(null, "", view === "story" ? "#story" : current ? `#cp=${current.id}` : "#");
}

function eventLi(e: EventItem): string {
  const when = e.end ? `${e.start} to ${e.end}` : `from ${e.start}`;
  const kind = e.kind === "curated" ? "event" : (e.event_type ?? "disruption");
  const src = e.kind === "curated" ? ` <a href="${e.source_url}" target="_blank" rel="noopener">source</a>` : "";
  return `<li><span class="kind">${kind}</span><strong>${e.title}</strong><div class="when">${when}</div><div>${e.summary}${src}</div></li>`;
}

async function renderPanel(id: string) {
  const props = geoProps.get(id);
  if (!props) return;
  const [series, events] = await Promise.all([loadSeries(id), loadEvents()]);
  const own = events.filter((e) => e.chokepoints.includes(id));
  $("panel").hidden = false;
  $("panel-title").textContent = props.name;
  $("panel-stats").innerHTML = [
    ["7-day mean / day", fmtNum(props.ma7_total, 1)],
    ["vs 2019-2022", fmtPct(props.vs_baseline_pct)],
    ["vs a year ago", fmtPct(props.yoy_pct)],
  ].map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("");
  $("panel-events").innerHTML = own.length
    ? own.slice().sort((a, b) => b.start.localeCompare(a.start)).map(eventLi).join("")
    : `<li class="when">No events recorded for this chokepoint.</li>`;
  current?.chart?.destroy();
  const draw = mode === "total" ? totalChart : typeChart;
  current = { id, chart: draw($("panel-chart"), series, own) };
  map.setFilter("selected", ["==", ["get", "portid"], id]);
  history.replaceState(null, "", `#cp=${id}`);
}

async function renderStory() {
  const [events, ...series] = await Promise.all([loadEvents(), ...STORY.lines.map((l) => loadSeries(l.id))]);
  const ev = events.find((e) => e.id === STORY.event);
  const lines: IndexLine[] = STORY.lines.map((l, i) => ({ name: l.name, color: l.color, series: series[i] }));
  const last = (i: number) => {
    const s = series[i];
    const k = s.ma7_total.length - 1;
    const v = s.ma7_total[k], b = s.baseline_ma7[k];
    return v != null && b ? (100 * v) / b : null;
  };
  const cape = lines[2].series;
  const idx = cape.ma7_total.map((v, i) => (v != null && cape.baseline_ma7[i] ? (100 * v) / (cape.baseline_ma7[i] as number) : -1));
  const peak = Math.max(...idx);
  const peakDate = cape.dates[idx.indexOf(peak)];
  $("story-lede").textContent =
    `Since Houthi attacks on shipping began in November 2023, transits through the Bab el-Mandeb Strait and the Suez Canal fell well below their 2019-2022 norm while the Cape of Good Hope route rose. ` +
    `On the latest data day (${cape.dates[cape.dates.length - 1]}) the 7-day mean is at ${fmtNum(last(0))}% of baseline at Bab el-Mandeb, ${fmtNum(last(1))}% at Suez and ${fmtNum(last(2))}% at the Cape of Good Hope (peak ${fmtNum(peak)}% on ${peakDate}).`;
  storyChart?.destroy();
  storyChart = indexChart($("story-chart"), lines, ev ? [ev] : []);
  $("story-events").innerHTML = ev ? eventLi(ev) : "";
}

function bindUI() {
  document.querySelectorAll<HTMLButtonElement>("#tabs button").forEach((b) =>
    b.addEventListener("click", () => showView(b.dataset.view as "map" | "story")),
  );
  $("panel-close").addEventListener("click", () => {
    $("panel").hidden = true;
    current?.chart?.destroy();
    current = null;
    map.setFilter("selected", ["==", ["get", "portid"], ""]);
    history.replaceState(null, "", "#");
  });
  const setMode = (m: "total" | "type") => {
    mode = m;
    $("mode-total").setAttribute("aria-pressed", String(m === "total"));
    $("mode-type").setAttribute("aria-pressed", String(m === "type"));
    if (current) void renderPanel(current.id);
  };
  $("mode-total").addEventListener("click", () => setMode("total"));
  $("mode-type").addEventListener("click", () => setMode("type"));
}

async function init() {
  map = new maplibregl.Map({
    container: "map",
    style: "https://tiles.openfreemap.org/styles/dark",
    center: [60, 22],
    zoom: window.innerWidth < 720 ? 1.2 : 1.9,
    attributionControl: { compact: true },
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-left");
  const styleReady = new Promise<void>((res) => map.once("load", () => res()));
  bindUI();

  const [meta, geo] = await Promise.all([loadMeta(), loadGeo()]);
  geoProps = new Map(geo.features.map((f) => [f.properties.portid, f.properties]));
  $("fresh").textContent = `Data through ${meta.data_max_date} (estimates, ~6 day lag) · built ${meta.built_at.slice(0, 10)}`;
  $("attribution").textContent = meta.attribution + " Modelled estimates; may be revised.";

  await styleReady;
  map.addSource("chokepoints", { type: "geojson", data: geo });
  const colour: maplibregl.ExpressionSpecification = [
    "case", ["==", ["get", "vs_baseline_pct"], null], "#7a8794",
    ["interpolate", ["linear"], ["get", "vs_baseline_pct"],
      -60, "#e66101", -30, "#fdb863", 0, "#e8e8e8", 30, "#92c5de", 60, "#2b8cbe"],
  ];
  map.addLayer({
    id: "chokepoints", type: "circle", source: "chokepoints",
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["coalesce", ["get", "ma7_total"], 0], 0, 5, 10, 8, 50, 13, 100, 18, 250, 28],
      "circle-color": colour,
      "circle-opacity": 0.88,
      "circle-stroke-color": "#0e141b",
      "circle-stroke-width": 1.5,
    },
  });
  map.addLayer({
    id: "selected", type: "circle", source: "chokepoints", filter: ["==", ["get", "portid"], ""],
    paint: { "circle-radius": ["interpolate", ["linear"], ["coalesce", ["get", "ma7_total"], 0], 0, 8, 250, 32], "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": "#ffffff", "circle-stroke-width": 2.5 },
  });
  map.addLayer({
    id: "labels", type: "symbol", source: "chokepoints", minzoom: 2.6,
    layout: { "text-field": ["get", "name"], "text-font": ["Noto Sans Regular"], "text-size": 11, "text-offset": [0, 1.9], "text-anchor": "top" },
    paint: { "text-color": "#e6edf3", "text-halo-color": "#0e141b", "text-halo-width": 1.5 },
  });

  const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 14 });
  map.on("mousemove", "chokepoints", (e) => {
    map.getCanvas().style.cursor = "pointer";
    const f = e.features?.[0];
    if (!f) return;
    const p = f.properties as unknown as ChokepointProps;
    popup.setLngLat((f.geometry as GeoJSON.Point).coordinates as [number, number])
      .setHTML(`<strong>${p.name}</strong><br>${fmtPct(p.vs_baseline_pct)} vs baseline`).addTo(map);
  });
  map.on("mouseleave", "chokepoints", () => { map.getCanvas().style.cursor = ""; popup.remove(); });
  map.on("click", "chokepoints", (e) => {
    const id = e.features?.[0]?.properties?.portid as string | undefined;
    if (id) void renderPanel(id);
  });

  const h = location.hash;
  if (h === "#story") showView("story");
  else if (h.startsWith("#cp=")) void renderPanel(h.slice(4));
}

init().catch((err) => {
  console.error(err);
  $("fresh").textContent = "Could not load data. Run `uv run mlake all` first.";
});
