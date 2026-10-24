import { useCallback, useEffect, useId, useMemo, useRef, useState, type CSSProperties } from "react";
import { formatLocalTime, formatUtcOffset, formatValue } from "../format";
import {
  assignRows,
  buildLanes,
  fullDomain,
  percentOf,
  timeOf,
  timeTicks,
  zoomDomain,
  ZOOM_LEVELS,
  type Domain,
  type Lane,
  type ZoomLevel,
} from "../timeline";
import type { EvidenceItem } from "../types";
import { AXIS_GUTTER_PX, ChartLane, PLOT_RIGHT_PX } from "./ChartLane";
import { EvidenceSummary, SEVERITIES, SeverityGlyph } from "./Evidence";

const REPLAY_MS = 7000;
const MARKER_ROW_PX = 26;

interface TimelineProps {
  evidence: EvidenceItem[];
  offsetMinutes: number;
  /** Time the zoom levels centre on, usually when the failure occurred. */
  focusTime: number;
  highlighted: ReadonlySet<string>;
}

function prefersReducedMotion(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** Sweep a cursor across the domain so evidence "lights up" in the order it happened. */
function useReplay(domain: Domain) {
  const [cursor, setCursor] = useState<number | null>(null);
  const frame = useRef<number | null>(null);

  const stop = useCallback(() => {
    if (frame.current !== null) cancelAnimationFrame(frame.current);
    frame.current = null;
    setCursor(null);
  }, []);

  const play = useCallback(() => {
    stop();
    if (prefersReducedMotion()) return;
    const startedAt = performance.now();
    const tick = (now: number) => {
      const progress = Math.min((now - startedAt) / REPLAY_MS, 1);
      if (progress >= 1) {
        frame.current = null;
        setCursor(null);
        return;
      }
      setCursor(domain[0] + progress * (domain[1] - domain[0]));
      frame.current = requestAnimationFrame(tick);
    };
    frame.current = requestAnimationFrame(tick);
  }, [domain, stop]);

  useEffect(() => stop, [stop]);

  return { cursor, playing: cursor !== null, play, stop };
}

function laneSummary(lane: Lane): string {
  const parts: string[] = [];
  for (const s of lane.series) {
    const values = s.points.map((p) => p.value);
    const min = Math.min(...values);
    const max = Math.max(...values);
    parts.push(`${s.points.length} readings, ${formatValue(min, s.unit)} to ${formatValue(max, s.unit)}`);
  }
  if (lane.markers.length) parts.push(`${lane.markers.length} ${lane.markers.length === 1 ? "item" : "items"}`);
  return parts.join("; ");
}

function Gridlines({ ticks, domain, cursor }: { ticks: number[]; domain: Domain; cursor: number | null }) {
  return (
    <>
      {ticks.map((t) => (
        <span key={t} className="gridline" style={{ left: `${percentOf(t, domain)}%` }} aria-hidden="true" />
      ))}
      {cursor !== null ? (
        <span className="replay-cursor" style={{ left: `${percentOf(cursor, domain)}%` }} aria-hidden="true" />
      ) : null}
    </>
  );
}

interface MarkerTrackProps {
  items: EvidenceItem[];
  domain: Domain;
  offsetMinutes: number;
  highlighted: ReadonlySet<string>;
  cursor: number | null;
  activeId: string | null;
  onActivate: (id: string | null) => void;
  tooltipId: string;
}

function MarkerTrack({ items, domain, offsetMinutes, highlighted, cursor, activeId, onActivate, tooltipId }: MarkerTrackProps) {
  const visible = items.filter((item) => {
    const x = percentOf(timeOf(item), domain);
    return x >= 0 && x <= 100;
  });
  const rows = assignRows(visible, domain);
  const rowCount = Math.max(1, ...[...rows.values()].map((r) => r + 1));
  const active = visible.find((item) => item.id === activeId);

  return (
    <div className="marker-track" style={{ height: rowCount * MARKER_ROW_PX + 8 }}>
      {visible.map((item) => {
        const x = percentOf(timeOf(item), domain);
        const severity = item.severity ?? "info";
        const isHighlighted = highlighted.has(item.id);
        const pending = cursor !== null && timeOf(item) > cursor;
        const classes = [
          "marker",
          `sev-${severity}`,
          isHighlighted ? "is-highlighted" : "",
          highlighted.size > 0 && !isHighlighted ? "is-dimmed" : "",
          pending ? "is-pending" : "",
        ];
        return (
          <button
            key={item.id}
            type="button"
            className={classes.filter(Boolean).join(" ")}
            style={{ left: `${x}%`, top: 4 + (rows.get(item.id) ?? 0) * MARKER_ROW_PX }}
            aria-label={`${item.label}, ${severity}, ${formatLocalTime(item.time, offsetMinutes)}${isHighlighted ? ", highlighted" : ""}`}
            aria-describedby={activeId === item.id ? tooltipId : undefined}
            data-evidence-id={item.id}
            data-highlighted={isHighlighted}
            onMouseEnter={() => onActivate(item.id)}
            onMouseLeave={() => onActivate(null)}
            onFocus={() => onActivate(item.id)}
            onBlur={() => onActivate(null)}
            onClick={() => onActivate(item.id)}
            onKeyDown={(e) => {
              if (e.key === "Escape") onActivate(null);
            }}
          >
            <SeverityGlyph severity={severity} />
          </button>
        );
      })}
      {active ? (
        <div
          id={tooltipId}
          role="tooltip"
          className={`tip marker-tip ${percentOf(timeOf(active), domain) > 55 ? "align-right" : ""}`}
          style={
            {
              "--x": `${percentOf(timeOf(active), domain)}%`,
              top: ((rows.get(active.id) ?? 0) + 1) * MARKER_ROW_PX + 6,
            } as CSSProperties
          }
        >
          <EvidenceSummary item={active} offsetMinutes={offsetMinutes} />
        </div>
      ) : null}
    </div>
  );
}

export function Timeline({ evidence, offsetMinutes, focusTime, highlighted }: TimelineProps) {
  const headingId = useId();
  const tooltipId = useId();
  const [zoom, setZoom] = useState<ZoomLevel>("all");
  const [activeId, setActiveId] = useState<string | null>(null);

  const lanes = useMemo(() => buildLanes(evidence), [evidence]);
  const full = useMemo(() => fullDomain(evidence), [evidence]);
  const domain = useMemo(() => zoomDomain(full, zoom, focusTime), [full, zoom, focusTime]);
  const ticks = useMemo(() => timeTicks(domain, offsetMinutes), [domain, offsetMinutes]);
  const { cursor, playing, play, stop } = useReplay(domain);
  const highlightStatus = useMemo(() => {
    if (highlighted.size === 0) return "";
    const items = evidence.filter((e) => highlighted.has(e.id));
    const hidden = items.filter((e) => {
      const t = timeOf(e);
      return t < domain[0] || t > domain[1];
    }).length;
    const noun = items.length === 1 ? "evidence item" : "evidence items";
    return `${items.length} ${noun} highlighted${hidden ? `, ${hidden} outside this zoom` : ""}.`;
  }, [evidence, highlighted, domain]);

  const changeZoom = (level: ZoomLevel) => {
    stop();
    setZoom(level);
  };

  const style = { "--axis-gutter": `${AXIS_GUTTER_PX}px`, "--plot-right": `${PLOT_RIGHT_PX}px` } as CSSProperties;

  return (
    <section className="card timeline" aria-labelledby={headingId}>
      <div className="timeline-toolbar">
        <h2 id={headingId}>Timeline</h2>
        <div className="segmented" role="group" aria-label="Zoom">
          {ZOOM_LEVELS.map((z) => (
            <button key={z.level} type="button" aria-pressed={zoom === z.level} onClick={() => changeZoom(z.level)}>
              {z.label}
            </button>
          ))}
        </div>
        <button type="button" className="button" onClick={playing ? stop : play} aria-pressed={playing}>
          {playing ? "Stop replay" : "Replay"}
        </button>
      </div>
      <ul className="legend" aria-label="Severity legend">
        {SEVERITIES.map((s) => (
          <li key={s}>
            <SeverityGlyph severity={s} size={12} /> {s}
          </li>
        ))}
      </ul>
      <p className="timeline-status" role="status">
        {highlightStatus}
      </p>

      {lanes.length === 0 ? (
        <p className="empty">This incident has no evidence items.</p>
      ) : (
        <div className="lanes" style={style}>
          {lanes.map((lane) => {
            const labelId = `${headingId}-${lane.kind}`;
            const summary = laneSummary(lane);
            return (
              <div key={lane.kind} className="lane" role="group" aria-labelledby={labelId} data-kind={lane.kind}>
                <div className="lane-label">
                  <span id={labelId}>{lane.label}</span>
                  <span className="lane-summary">{summary}</span>
                </div>
                <div className="lane-plot">
                  {lane.series.length > 0 ? (
                    <figure className="chart">
                      <figcaption className="sr-only">
                        {lane.label}: {summary}
                      </figcaption>
                      <ChartLane
                        series={lane.series}
                        domain={domain}
                        offsetMinutes={offsetMinutes}
                        highlighted={highlighted}
                        cursor={cursor}
                      />
                      <div className="track overlay">
                        <Gridlines ticks={ticks} domain={domain} cursor={cursor} />
                      </div>
                    </figure>
                  ) : null}
                  {lane.markers.length > 0 ? (
                    <div className="track">
                      <Gridlines ticks={ticks} domain={domain} cursor={cursor} />
                      <MarkerTrack
                        items={lane.markers}
                        domain={domain}
                        offsetMinutes={offsetMinutes}
                        highlighted={highlighted}
                        cursor={cursor}
                        activeId={activeId}
                        onActivate={setActiveId}
                        tooltipId={tooltipId}
                      />
                    </div>
                  ) : null}
                </div>
              </div>
            );
          })}
          <div className="lane lane-axis" aria-hidden="true">
            <div className="lane-label">{formatUtcOffset(offsetMinutes)}</div>
            <div className="lane-plot">
              <div className="track axis-track">
                {ticks.map((t) => (
                  <span key={t} className="tick" style={{ left: `${percentOf(t, domain)}%` }}>
                    {formatLocalTime(t, offsetMinutes, domain[1] - domain[0] <= 10 * 60_000)}
                  </span>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}
      <p className="sr-only" aria-live="polite">
        {playing ? `Replaying ${formatLocalTime(domain[0], offsetMinutes)} to ${formatLocalTime(domain[1], offsetMinutes)}` : ""}
      </p>
    </section>
  );
}
