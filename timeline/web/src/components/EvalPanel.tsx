import { useId } from "react";
import { api, ApiError, useApi } from "../api";
import { formatDuration, formatInteger, formatPercent, formatUsd } from "../format";
import type { EvalConfigResult, EvalSummary, RunStats } from "../types";

interface Metric {
  label: string;
  format: (row: EvalConfigResult) => string;
}

const METRICS: Metric[] = [
  { label: "Top-1 accuracy", format: (r) => formatPercent(r.top1_accuracy) },
  { label: "Top-3 accuracy", format: (r) => formatPercent(r.top3_accuracy) },
  { label: "Valid output", format: (r) => formatPercent(r.valid_rate) },
  { label: "Complete report", format: (r) => formatPercent(r.report_complete_rate) },
  { label: "Mean input tokens", format: (r) => formatInteger(r.mean_input_tokens) },
  { label: "Mean output tokens", format: (r) => formatInteger(r.mean_output_tokens) },
  { label: "Mean cost", format: (r) => formatUsd(r.mean_cost_usd) },
  { label: "Mean latency", format: (r) => formatDuration(r.mean_latency_ms) },
  { label: "p95 latency", format: (r) => formatDuration(r.p95_latency_ms) },
];

/** Pick the skill+tools run and the raw-dump baseline, preferring a pair on the same split. */
export function findComparison(configs: EvalConfigResult[]): { skill: EvalConfigResult; raw: EvalConfigResult } | null {
  const skills = configs.filter((c) => /skill/i.test(c.config));
  const raws = configs.filter((c) => /raw/i.test(c.config));
  for (const skill of skills) {
    const raw = raws.find((r) => r.split === skill.split && r.model === skill.model) ?? raws.find((r) => r.split === skill.split);
    if (raw) return { skill, raw };
  }
  return skills[0] && raws[0] ? { skill: skills[0], raw: raws[0] } : null;
}

function formatGenerated(iso: string): string {
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? iso : `${new Date(ms).toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

function signed(n: number, digits = 0): string {
  const fixed = Math.abs(n).toFixed(digits);
  return n < 0 ? `−${fixed}` : `+${fixed}`;
}

function Comparison({ skill, raw }: { skill: EvalConfigResult; raw: EvalConfigResult }) {
  const tokenChange = raw.mean_input_tokens > 0 ? (skill.mean_input_tokens / raw.mean_input_tokens - 1) * 100 : 0;
  const tiles = [
    {
      label: "Top-1 accuracy",
      value: formatPercent(skill.top1_accuracy),
      delta: `${signed((skill.top1_accuracy - raw.top1_accuracy) * 100)} pts vs ${formatPercent(raw.top1_accuracy)}`,
    },
    {
      label: "Mean input tokens",
      value: formatInteger(skill.mean_input_tokens),
      delta: `${signed(tokenChange)}% vs ${formatInteger(raw.mean_input_tokens)}`,
    },
    {
      label: "Mean cost",
      value: formatUsd(skill.mean_cost_usd),
      delta: `vs ${formatUsd(raw.mean_cost_usd)}`,
    },
    {
      label: "Mean latency",
      value: formatDuration(skill.mean_latency_ms),
      delta: `vs ${formatDuration(raw.mean_latency_ms)}`,
    },
  ];
  return (
    <ul className="tiles" aria-label={`${skill.config} compared with ${raw.config}`}>
      {tiles.map((tile) => (
        <li key={tile.label} className="tile">
          <span className="tile-label">{tile.label}</span>
          <span className="tile-value">{tile.value}</span>
          <span className="tile-delta">{tile.delta}</span>
        </li>
      ))}
    </ul>
  );
}

function SummaryTable({ summary }: { summary: EvalSummary }) {
  const columns = summary.configs;
  return (
    <div className="table-scroll">
      <table>
        <caption className="sr-only">Eval results by configuration</caption>
        <thead>
          <tr>
            <th scope="col">Metric</th>
            {columns.map((c, i) => (
              <th key={`${c.config}-${c.model}-${c.split}-${i}`} scope="col">
                {c.config}
                <span className="th-sub">
                  {c.model} · {c.split} · n={c.cases}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {METRICS.map((m) => (
            <tr key={m.label}>
              <th scope="row">{m.label}</th>
              {columns.map((c, i) => (
                <td key={i}>{m.format(c)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RunCard({ run }: { run: RunStats }) {
  return (
    <div className="run">
      <h3>This run</h3>
      <dl className="facts compact">
        <div>
          <dt>Config</dt>
          <dd>
            {run.config} · {run.model}
          </dd>
        </div>
        <div>
          <dt>Tokens</dt>
          <dd>
            {formatInteger(run.input_tokens)} in · {formatInteger(run.output_tokens)} out
            {run.cache_read_tokens ? ` · ${formatInteger(run.cache_read_tokens)} cached` : ""}
          </dd>
        </div>
        <div>
          <dt>Cost</dt>
          <dd>{formatUsd(run.cost_usd)}</dd>
        </div>
        <div>
          <dt>Latency</dt>
          <dd>{formatDuration(run.latency_ms)}</dd>
        </div>
      </dl>
    </div>
  );
}

export function EvalPanel({ run }: { run?: RunStats | null }) {
  const headingId = useId();
  const state = useApi(api.getEvals, "evals");

  let body;
  if (state.status === "loading") {
    body = <p className="muted">Loading eval results…</p>;
  } else if (state.status === "error") {
    body =
      state.error instanceof ApiError && state.error.status === 404 ? (
        <div className="empty">
          <p>
            <strong>No eval results yet.</strong>
          </p>
          <p className="muted">
            Run the eval harness to write <code>evals/results/summary.json</code>, then reload.
          </p>
        </div>
      ) : (
        <p role="alert">Could not load eval results: {state.error.message}</p>
      );
  } else {
    const comparison = findComparison(state.data.configs);
    body = (
      <>
        {comparison ? (
          <>
            <p className="muted">
              <strong>{comparison.skill.config}</strong> vs <strong>{comparison.raw.config}</strong> baseline,{" "}
              {comparison.skill.split} split
            </p>
            <Comparison skill={comparison.skill} raw={comparison.raw} />
          </>
        ) : null}
        <SummaryTable summary={state.data} />
        <p className="muted small">Generated {formatGenerated(state.data.generated_at)}</p>
      </>
    );
  }

  return (
    <aside className="card eval-panel" aria-labelledby={headingId}>
      <h2 id={headingId}>Skill + tools vs raw dump</h2>
      {body}
      {run ? <RunCard run={run} /> : null}
    </aside>
  );
}
