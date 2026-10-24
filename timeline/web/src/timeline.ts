import type { EvidenceItem, EvidenceKind } from "./types";

/** Lanes top to bottom: context first, then the signals that build up to the crash. */
export const LANE_ORDER: readonly EvidenceKind[] = ["driver", "process", "sensor", "frametime", "event"];

export const LANE_LABELS: Record<EvidenceKind, string> = {
  driver: "Driver",
  process: "Processes",
  sensor: "Sensors",
  frametime: "Frame time",
  event: "Events",
};

/** Kinds whose numeric values are drawn as a line chart instead of markers. */
const CHART_KINDS: ReadonlySet<EvidenceKind> = new Set(["sensor", "frametime"]);

export interface SeriesPoint {
  t: number;
  value: number;
  item: EvidenceItem;
}

export interface Series {
  key: string;
  unit: string | null;
  points: SeriesPoint[];
}

export interface Lane {
  kind: EvidenceKind;
  label: string;
  /** Items drawn as discrete markers. */
  markers: EvidenceItem[];
  /** Numeric items drawn as lines, one series per unit. */
  series: Series[];
}

export type Domain = readonly [number, number];

export function timeOf(item: EvidenceItem): number {
  return Date.parse(item.time);
}

function byTime(a: EvidenceItem, b: EvidenceItem): number {
  return timeOf(a) - timeOf(b) || a.id.localeCompare(b.id);
}

/** Group evidence into lanes in LANE_ORDER, skipping kinds with no evidence. */
export function buildLanes(evidence: EvidenceItem[]): Lane[] {
  const lanes: Lane[] = [];
  for (const kind of LANE_ORDER) {
    const items = evidence.filter((e) => e.kind === kind).sort(byTime);
    if (items.length === 0) continue;
    const markers: EvidenceItem[] = [];
    const seriesByUnit = new Map<string, Series>();
    for (const item of items) {
      if (CHART_KINDS.has(kind) && typeof item.value === "number" && Number.isFinite(item.value)) {
        const unit = item.unit ?? null;
        const key = unit ?? "";
        let series = seriesByUnit.get(key);
        if (!series) {
          series = { key: `${kind}:${key}`, unit, points: [] };
          seriesByUnit.set(key, series);
        }
        series.points.push({ t: timeOf(item), value: item.value, item });
      } else {
        markers.push(item);
      }
    }
    lanes.push({ kind, label: LANE_LABELS[kind], markers, series: [...seriesByUnit.values()] });
  }
  return lanes;
}

/** Evidence extent, padded so markers at the edges stay visible. */
export function fullDomain(evidence: EvidenceItem[]): Domain {
  const times = evidence.map(timeOf).filter(Number.isFinite);
  if (times.length === 0) {
    const now = Date.now();
    return [now - 60_000, now];
  }
  const min = Math.min(...times);
  const max = Math.max(...times);
  const pad = Math.max((max - min) * 0.03, 30_000);
  return [min - pad, max + pad];
}

export type ZoomLevel = "all" | "5m" | "1m";

export const ZOOM_LEVELS: { level: ZoomLevel; label: string; spanMs: number | null }[] = [
  { level: "all", label: "All", spanMs: null },
  { level: "5m", label: "5 min", spanMs: 5 * 60_000 },
  { level: "1m", label: "1 min", spanMs: 60_000 },
];

/** Domain for a zoom level, centred slightly before `focus` so the lead-up stays in view. */
export function zoomDomain(full: Domain, level: ZoomLevel, focus: number): Domain {
  const span = ZOOM_LEVELS.find((z) => z.level === level)?.spanMs ?? null;
  if (span === null || span >= full[1] - full[0]) return full;
  const start = Math.min(Math.max(focus - span * 0.7, full[0]), full[1] - span);
  return [start, start + span];
}

/** Position of `t` within the domain as a percentage, unclamped. */
export function percentOf(t: number, [start, end]: Domain): number {
  return ((t - start) / (end - start)) * 100;
}

const TICK_STEPS_MS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 21600].map((s) => s * 1000);

/** Round tick times aligned to the machine's local clock. */
export function timeTicks([start, end]: Domain, offsetMinutes: number, maxTicks = 6): number[] {
  const span = end - start;
  const step = TICK_STEPS_MS.find((s) => span / s <= maxTicks) ?? TICK_STEPS_MS[TICK_STEPS_MS.length - 1]!;
  const offsetMs = offsetMinutes * 60_000;
  const first = Math.ceil((start + offsetMs) / step) * step - offsetMs;
  const ticks: number[] = [];
  for (let t = first; t <= end; t += step) ticks.push(t);
  return ticks;
}

/**
 * Spread markers over rows so ones closer than `minGapPercent` don't overlap.
 * Returns the row index for each marker id.
 */
export function assignRows(items: EvidenceItem[], domain: Domain, minGapPercent = 3.5): Map<string, number> {
  const rowEnds: number[] = [];
  const rows = new Map<string, number>();
  for (const item of [...items].sort(byTime)) {
    const x = percentOf(timeOf(item), domain);
    let row = rowEnds.findIndex((end) => x - end >= minGapPercent);
    if (row === -1) {
      row = rowEnds.length;
      rowEnds.push(x);
    } else {
      rowEnds[row] = x;
    }
    rows.set(item.id, row);
  }
  return rows;
}
