import * as maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import "./style.css";
import {
  fmtNum, fmtPct, loadEvents, loadGeo, loadMeta, loadSeries,
  type ChokepointProps, type EventItem,
} from "./data";
import { hideTimeMap, showTimeMap, updateTimeMapSpec } from "./timemap";
import { baselineIndex, indexChart, totalChart, typeChart, type ChartHandle, type IndexLine } from "./charts";

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

type View = "map" | "story" | "time";

function showView(view: View, spec?: string) {
  for (const v of ["map", "story", "time"] as const) $(`tab-${v}`).setAttribute("aria-selected", String(v === view));
  $("story").hidden = view !== "story";
  $("legend").style.display = view === "map" ? "" : "none";
  if (view !== "time") hideTimeMap();
  if (view === "story") {
    $("panel").hidden = true;
    void renderStory();
  } else if (view === "time") {
    $("panel").hidden = true;
    void showTimeMap(spec); // sets its own hash
    return;
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
  const idx = series.map((s) => baselineIndex(s));
  const last = (i: number) => idx[i][idx[i].length - 1];
  const cape = idx[2];
  const peak = Math.max(...cape.map((v) => v ?? -1));
  const peakDate = series[2].dates[cape.indexOf(peak)];
  const latest = series[2].dates[series[2].dates.length - 1];
  $("story-lede").textContent =
    `Since Houthi attacks on shipping began in November 2023, transits through the Bab el-Mandeb Strait and the Suez Canal fell well below their 2019-2022 norm while the Cape of Good Hope route rose. ` +
    `Over the four weeks to the latest data day (${latest}), traffic ran at ${fmtNum(last(0))}% of baseline at Bab el-Mandeb, ${fmtNum(last(1))}% at Suez and ${fmtNum(last(2))}% at the Cape of Good Hope (4-week peak ${fmtNum(peak)}%, reached ${peakDate}).`;
  storyChart?.destroy();
  storyChart = indexChart($("story-chart"), lines, ev ? [ev] : []);
  $("story-events").innerHTML = ev ? eventLi(ev) : "";
}

function bindUI() {
  document.querySelectorAll<HTMLButtonElement>("#tabs button").forEach((b) =>
    b.addEventListener("click", () => showView(b.dataset.view as View)),
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
    center: [30, 15],
    zoom: window.innerWidth < 720 ? 0 : 1.5,
    attributionControl: { compact: true },
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-left");
  const styleReady = new Promise<void>((res) => map.once("load", () => res()));
  bindUI();

  const [meta, geo] = await Promise.all([loadMeta(), loadGeo()]);
  geoProps = new Map(geo.features.map((f) => [f.properties.portid, f.properties]));
  // Desktop: frame every chokepoint. Phones are narrower than the world at zoom 0,
  // so centre on a view that still holds the Panama Canal and East Asia.
  if (window.innerWidth < 720) {
    map.jumpTo({ center: [30, 15], zoom: 0 });
  } else {
    const bounds = new maplibregl.LngLatBounds();
    for (const f of geo.features) bounds.extend(f.geometry.coordinates as [number, number]);
    if (!bounds.isEmpty()) map.fitBounds(bounds, { padding: 60, maxZoom: 3, duration: 0 });
  }
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

  route();
  window.addEventListener("hashchange", () => route(true));
}

function route(fromLink = false) {
  const h = location.hash;
  if (h === "#story") showView("story");
  else if (h.startsWith("#time")) {
    if (fromLink && !$("timemap").hidden) updateTimeMapSpec(h.slice(6));
    else showView("time", h.slice(6));
  } else if (h.startsWith("#cp=")) void renderPanel(h.slice(4));
  else if (fromLink) showView("map");
}

init().catch((err) => {
  console.error(err);
  $("fresh").textContent = "Could not load data. Run `uv run mlake all` first.";
});
