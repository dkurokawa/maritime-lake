import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import { toSec, type EventItem, type Series } from "./data";

const TEXT = "#e6edf3";
const MUTED = "#93a1b0";
const GRID = "rgba(147,161,176,0.15)";

export const TYPE_COLORS: Record<string, string> = {
  container: "#5bb5e8",
  dry_bulk: "#e8c468",
  general_cargo: "#9a8fd1",
  roro: "#6fcf97",
  tanker: "#e8825b",
};
const TYPE_LABELS: Record<string, string> = {
  container: "Container",
  dry_bulk: "Dry bulk",
  general_cargo: "General cargo",
  roro: "Ro-ro",
  tanker: "Tanker",
};

function ma7(values: number[]): (number | null)[] {
  const out: (number | null)[] = [];
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    sum += values[i];
    if (i >= 7) sum -= values[i - 7];
    out.push(i >= 6 ? sum / 7 : null);
  }
  return out;
}

function eventsPlugin(events: EventItem[]): uPlot.Plugin {
  return {
    hooks: {
      draw: (u) => {
        const ctx = u.ctx;
        const { left, top, width, height } = u.bbox;
        const [min, max] = [u.scales.x.min ?? -Infinity, u.scales.x.max ?? Infinity];
        ctx.save();
        for (const e of events) {
          const x0 = toSec(e.start);
          if (x0 < min || x0 > max) continue;
          const curated = e.kind === "curated";
          ctx.strokeStyle = curated ? "#f4a259" : "rgba(244,162,89,0.35)";
          ctx.fillStyle = curated ? "rgba(244,162,89,0.12)" : "rgba(244,162,89,0.06)";
          ctx.lineWidth = curated ? 1.5 * devicePixelRatio : devicePixelRatio;
          ctx.setLineDash(curated ? [4 * devicePixelRatio, 3 * devicePixelRatio] : []);
          const px = u.valToPos(x0, "x", true);
          if (e.end) {
            const px1 = Math.min(u.valToPos(toSec(e.end), "x", true), left + width);
            ctx.fillRect(px, top, Math.max(px1 - px, 1), height);
          }
          ctx.beginPath();
          ctx.moveTo(px, top);
          ctx.lineTo(px, top + height);
          ctx.stroke();
          if (curated) {
            ctx.setLineDash([]);
            ctx.fillStyle = "#f4a259";
            ctx.font = `${11 * devicePixelRatio}px system-ui, sans-serif`;
            ctx.fillText(e.title, Math.min(px + 4 * devicePixelRatio, left + width - 160 * devicePixelRatio), top + 12 * devicePixelRatio);
          }
        }
        ctx.restore();
      },
    },
  };
}

const axes = (valueFmt?: (v: number) => string): uPlot.Axis[] => [
  { stroke: MUTED, grid: { stroke: GRID, width: 1 }, ticks: { stroke: GRID } },
  {
    stroke: MUTED,
    grid: { stroke: GRID, width: 1 },
    ticks: { stroke: GRID },
    size: 52,
    ...(valueFmt ? { values: (_u: uPlot, ticks: number[]) => ticks.map(valueFmt) } : {}),
  },
];

export interface ChartHandle {
  destroy(): void;
}

/** hideInLegend: series indexes that only exist to draw a band and should not get a legend row. */
function mount(el: HTMLElement, opts: (w: number) => uPlot.Options, data: uPlot.AlignedData, hideInLegend: number[] = []): ChartHandle {
  el.innerHTML = "";
  const w = Math.max(el.clientWidth, 280);
  const plot = new uPlot(opts(w), data, el);
  const rows = plot.root.querySelectorAll<HTMLElement>(".u-legend .u-series");
  for (const i of hideInLegend) if (rows[i]) rows[i].style.display = "none";
  const ro = new ResizeObserver(() => plot.setSize({ width: Math.max(el.clientWidth, 280), height: plot.height }));
  ro.observe(el);
  return {
    destroy() {
      ro.disconnect();
      plot.destroy();
    },
  };
}

/** Total transits: 7-day mean, 2019-2022 baseline line + min/max band, event markers. */
export function totalChart(el: HTMLElement, s: Series, events: EventItem[]): ChartHandle {
  const x = s.dates.map(toSec);
  const data: uPlot.AlignedData = [x, s.ma7_total, s.baseline_ma7, s.baseline_hi, s.baseline_lo] as uPlot.AlignedData;
  return mount(
    el,
    (width) => ({
      width,
      height: 300,
      padding: [8, 8, 0, 0],
      cursor: { drag: { x: true, y: false } },
      scales: { x: { time: true }, y: { range: (_u, _min, max) => [0, max * 1.1] } },
      axes: axes(),
      series: [
        {},
        { label: "7-day mean transits/day", stroke: "#5bb5e8", width: 2, points: { show: false } },
        { label: "2019-2022 baseline", stroke: MUTED, width: 1.5, dash: [5, 4], points: { show: false } },
        { label: "baseline max", stroke: "transparent", points: { show: false } },
        { label: "baseline min", stroke: "transparent", points: { show: false } },
      ],
      bands: [{ series: [3, 4], fill: "rgba(147,161,176,0.16)" }],
      plugins: [eventsPlugin(events)],
    }),
    data,
    [3, 4],
  );
}

/** Stacked 7-day means by vessel type. */
export function typeChart(el: HTMLElement, s: Series, events: EventItem[]): ChartHandle {
  const x = s.dates.map(toSec);
  const types = Object.keys(TYPE_COLORS).filter((t) => s.n_by_type[t]);
  const perType = types.map((t) => ma7(s.n_by_type[t]));
  // cumulative, top of stack first
  const cum: (number | null)[][] = [];
  let running: (number | null)[] = x.map(() => 0);
  for (const arr of perType) {
    running = running.map((v, i) => (v == null || arr[i] == null ? null : v + (arr[i] as number)));
    cum.push(running);
  }
  const order = types.map((_, i) => types.length - 1 - i); // indices into types, top first
  const zero = x.map(() => 0);
  const data = [x, ...order.map((i) => cum[i]), zero] as uPlot.AlignedData;
  const raw = [null, ...order.map((i) => perType[i]), null];
  const series: uPlot.Series[] = [{}];
  order.forEach((i, k) => {
    const t = types[i];
    series.push({
      label: TYPE_LABELS[t],
      stroke: TYPE_COLORS[t],
      width: 1,
      points: { show: false },
      value: (_u, _v, _si, idx) => {
        const v = idx == null ? null : (raw[k + 1] as (number | null)[])[idx];
        return v == null ? "--" : v.toFixed(1);
      },
    });
  });
  series.push({ label: "", show: true, stroke: "transparent", points: { show: false } });
  const bands: uPlot.Band[] = order.map((i, k) => ({ series: [k + 1, k + 2], fill: TYPE_COLORS[types[i]] + "aa" }));
  return mount(
    el,
    (width) => ({
      width,
      height: 300,
      padding: [8, 8, 0, 0],
      scales: { x: { time: true }, y: { range: (_u, _min, max) => [0, max * 1.1] } },
      axes: axes(),
      series,
      bands,
      plugins: [eventsPlugin(events)],
    }),
    data,
    [series.length - 1],
  );
}

export interface IndexLine {
  name: string;
  color: string;
  series: Series;
}

export const STORY_WINDOW_DAYS = 28;

/**
 * Daily transits as % of the 2019-2022 baseline, averaged over a trailing window
 * (NULL until the window is complete). 7-day means at low-traffic chokepoints are
 * noisy; the story is about multi-month shifts, so it reads a 4-week average.
 */
export function baselineIndex(s: Series, windowDays = STORY_WINDOW_DAYS): (number | null)[] {
  const daily = s.ma7_total.map((v, i) => {
    const b = s.baseline_ma7[i];
    return v == null || b == null || b <= 0 ? null : (100 * v) / b;
  });
  return daily.map((_, i) => {
    if (i < windowDays - 1) return null;
    let sum = 0;
    for (let k = i - windowDays + 1; k <= i; k++) {
      const v = daily[k];
      if (v == null) return null;
      sum += v;
    }
    return sum / windowDays;
  });
}

/** Red Sea story: baseline index (see baselineIndex), several chokepoints on one chart. */
export function indexChart(el: HTMLElement, lines: IndexLine[], events: EventItem[]): ChartHandle {
  const base = lines[0].series;
  const x = base.dates.map(toSec);
  const cols = lines.map((l) => baselineIndex(l.series));
  const data = [x, ...cols] as uPlot.AlignedData;
  return mount(
    el,
    (width) => ({
      width,
      height: Math.min(420, Math.max(300, Math.round(width * 0.45))),
      padding: [8, 8, 0, 0],
      scales: { x: { time: true }, y: { range: (_u, _min, max) => [0, Math.max(160, max * 1.05)] } },
      axes: axes((v) => `${v}`),
      series: [
        {},
        ...lines.map((l) => ({
          label: l.name,
          stroke: l.color,
          width: 2,
          points: { show: false },
          value: (_u: uPlot, v: number | null) => (v == null ? "--" : v.toFixed(0)),
        })),
      ],
      plugins: [
        eventsPlugin(events),
        {
          hooks: {
            drawAxes: (u) => {
              // reference line at 100 (the baseline)
              const ctx = u.ctx;
              const y = u.valToPos(100, "y", true);
              ctx.save();
              ctx.strokeStyle = "rgba(230,237,243,0.5)";
              ctx.setLineDash([3 * devicePixelRatio, 3 * devicePixelRatio]);
              ctx.beginPath();
              ctx.moveTo(u.bbox.left, y);
              ctx.lineTo(u.bbox.left + u.bbox.width, y);
              ctx.stroke();
              ctx.restore();
            },
          },
        },
      ],
    }),
    data,
  );
}

export { TEXT };
