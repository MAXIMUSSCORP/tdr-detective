import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { Domain, Series, SeriesPoint } from "../timeline";
import { EvidenceSummary } from "./Evidence";

/** Must match --axis-gutter / --plot-right in styles.css so all lanes share one x scale. */
export const AXIS_GUTTER_PX = 48;
export const PLOT_RIGHT_PX = 12;
const CHART_HEIGHT_PX = 120;

interface ChartLaneProps {
  series: Series[];
  domain: Domain;
  offsetMinutes: number;
  highlighted: ReadonlySet<string>;
  /** When replaying, only points at or before this time are drawn. */
  cursor: number | null;
}

interface DotProps {
  cx?: number;
  cy?: number;
  payload?: SeriesPoint;
}

function TooltipBody({ active, payload, offsetMinutes }: {
  active?: boolean;
  payload?: readonly { payload?: unknown }[];
  offsetMinutes: number;
}) {
  const point = payload?.[0]?.payload as SeriesPoint | undefined;
  if (!active || !point) return null;
  return (
    <div className="tip" role="tooltip">
      <EvidenceSummary item={point.item} offsetMinutes={offsetMinutes} />
    </div>
  );
}

export function ChartLane({ series, domain, offsetMinutes, highlighted, cursor }: ChartLaneProps) {
  const dimOthers = highlighted.size > 0;

  const renderDot = ({ cx, cy, payload }: DotProps) => {
    if (cx === undefined || cy === undefined || !payload) return <g />;
    const { item } = payload;
    const isHighlighted = highlighted.has(item.id);
    const classes = [
      "dot",
      `sev-${item.severity ?? "info"}`,
      isHighlighted ? "is-highlighted" : "",
      dimOthers && !isHighlighted ? "is-dimmed" : "",
    ];
    return (
      <circle
        key={item.id}
        cx={cx}
        cy={cy}
        r={isHighlighted ? 6 : 3.5}
        className={classes.filter(Boolean).join(" ")}
        data-evidence-id={item.id}
        data-highlighted={isHighlighted}
      />
    );
  };

  return (
    <ResponsiveContainer width="100%" height={CHART_HEIGHT_PX} initialDimension={{ width: 640, height: CHART_HEIGHT_PX }}>
      <LineChart margin={{ top: 10, right: PLOT_RIGHT_PX, bottom: 4, left: 0 }}>
        <CartesianGrid vertical={false} className="chart-grid" />
        <XAxis
          type="number"
          dataKey="t"
          domain={[domain[0], domain[1]]}
          allowDataOverflow
          hide
        />
        <YAxis
          width={AXIS_GUTTER_PX}
          tickCount={4}
          tickLine={false}
          axisLine={false}
          className="chart-axis"
          tickFormatter={(v: number) => String(v)}
        />
        <Tooltip
          cursor={{ className: "chart-cursor" }}
          isAnimationActive={false}
          content={(props) => <TooltipBody active={props.active} payload={props.payload} offsetMinutes={offsetMinutes} />}
        />
        {series.map((s) => (
          <Line
            key={s.key}
            data={cursor === null ? s.points : s.points.filter((p) => p.t <= cursor)}
            dataKey="value"
            name={s.unit ?? s.key}
            type="linear"
            className="chart-line"
            isAnimationActive={false}
            dot={renderDot}
            activeDot={{ r: 6, className: "chart-active-dot" }}
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}
