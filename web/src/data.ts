export interface Meta {
  built_at: string;
  data_max_date: string;
  row_counts: Record<string, number>;
  attribution: string;
  note: string;
}

export interface Series {
  portid: string;
  name: string;
  dates: string[];
  n_total: number[];
  n_by_type: Record<string, number[]>;
  ma7_total: (number | null)[];
  baseline_ma7: (number | null)[];
  baseline_lo: (number | null)[];
  baseline_hi: (number | null)[];
  capacity: number[];
}

export interface EventItem {
  id: string;
  kind: "curated" | "disruption";
  title: string;
  start: string;
  end: string | null;
  chokepoints: string[];
  summary: string;
  source_url: string;
  event_type?: string;
}

export interface ChokepointProps {
  portid: string;
  name: string;
  date: string;
  n_total: number;
  ma7_total: number | null;
  ma7_capacity: number | null;
  yoy_pct: number | null;
  baseline_ma7: number | null;
  vs_baseline_pct: number | null;
}

const cache = new Map<string, Promise<unknown>>();
function getJSON<T>(url: string): Promise<T> {
  let p = cache.get(url);
  if (!p) {
    p = fetch(url).then((r) => {
      if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
      return r.json();
    });
    cache.set(url, p);
  }
  return p as Promise<T>;
}

export const loadMeta = () => getJSON<Meta>("/data/meta.json");
export const loadEvents = () => getJSON<EventItem[]>("/data/events.json");
export const loadSeries = (id: string) => getJSON<Series>(`/data/series/${id}.json`);
export const loadGeo = () =>
  getJSON<GeoJSON.FeatureCollection<GeoJSON.Point, ChokepointProps>>("/data/chokepoints.geojson");

export const toSec = (d: string) => Date.UTC(+d.slice(0, 4), +d.slice(5, 7) - 1, +d.slice(8, 10)) / 1000;
export const fmtPct = (v: number | null | undefined, digits = 0) =>
  v == null ? "n/a" : `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
export const fmtNum = (v: number | null | undefined, digits = 0) =>
  v == null ? "n/a" : v.toLocaleString("en-US", { maximumFractionDigits: digits });

// ---- container time map (Phase 1c) ----

export interface ContainerPort {
  portid: string;
  portname: string;
  country: string | null;
  lat: number;
  lon: number;
  rank: number | null;
  calls_365d: number | null;
  ma7_container: number | null;
  latest_date: string | null;
}

export interface ScenarioDef {
  id: string;
  label: string;
  restrictions: string[];
}

export interface ContainerOD {
  speed_kn: number;
  ports: string[];
  scenarios: ScenarioDef[];
  /** nm[scenario][origin index][destination index]; null = no sea path */
  nm: Record<string, (number | null)[][]>;
}

export const loadContainerPorts = () => getJSON<ContainerPort[]>("/data/container_ports.json");
export const loadContainerOD = () => getJSON<ContainerOD>("/data/container_od.json");
