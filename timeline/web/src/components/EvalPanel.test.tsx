import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { EvalConfigResult, EvalSummary } from "../types";
import { EvalPanel, findComparison } from "./EvalPanel";

function row(overrides: Partial<EvalConfigResult>): EvalConfigResult {
  return {
    config: "skill_tools",
    model: "model-a",
    split: "test",
    cases: 40,
    top1_accuracy: 0.85,
    top3_accuracy: 0.95,
    valid_rate: 1,
    report_complete_rate: 0.9,
    mean_input_tokens: 18_000,
    mean_output_tokens: 1_300,
    mean_cost_usd: 0.08,
    mean_latency_ms: 21_000,
    p95_latency_ms: 34_000,
    ...overrides,
  };
}

function mockFetch(status: number, body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })),
  );
}

describe("findComparison", () => {
  it("pairs the skill run with the raw-dump baseline on the same split", () => {
    const configs = [
      row({ config: "raw_dump", split: "dev" }),
      row({ config: "raw_dump", split: "test" }),
      row({ config: "skill_tools", split: "test" }),
    ];
    expect(findComparison(configs)).toEqual({ skill: configs[2], raw: configs[1] });
    expect(findComparison([row({})])).toBeNull();
  });
});

describe("<EvalPanel>", () => {
  it("shows a clear empty state when no eval results exist and the incident has no run stats", async () => {
    mockFetch(404, { error: "no eval results yet" });
    const { container } = render(<EvalPanel run={null} />);
    expect(await screen.findByText("No eval results yet.")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "This run" })).not.toBeInTheDocument();
    expect(container.textContent).not.toMatch(/undefined|NaN|null/);
  });

  it("shows the incident's own run stats when present", async () => {
    mockFetch(404, { error: "no eval results yet" });
    render(<EvalPanel run={{ config: "cfg", model: "model-a", input_tokens: 1200, output_tokens: 300, latency_ms: 950 }} />);
    expect(await screen.findByRole("heading", { name: "This run" })).toBeInTheDocument();
    expect(screen.getByText("1,200 in · 300 out")).toBeInTheDocument();
    expect(screen.getByText("n/a")).toBeInTheDocument();
  });

  it("compares skill+tools with the raw dump", async () => {
    const summary: EvalSummary = {
      generated_at: "2026-10-21T08:00:00Z",
      configs: [
        row({}),
        row({ config: "raw_dump", top1_accuracy: 0.6, mean_input_tokens: 150_000, mean_cost_usd: 0.47, mean_latency_ms: 48_000 }),
      ],
    };
    mockFetch(200, summary);
    render(<EvalPanel />);

    const tiles = await screen.findByRole("list", { name: "skill_tools compared with raw_dump" });
    expect(within(tiles).getByText("+25 pts vs 60%")).toBeInTheDocument();
    expect(within(tiles).getByText("−88% vs 150,000")).toBeInTheDocument();

    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("columnheader").map((th) => th.textContent)).toEqual([
      "Metric",
      "skill_toolsmodel-a · test · n=40",
      "raw_dumpmodel-a · test · n=40",
    ]);
    expect(within(table).getByRole("row", { name: /Top-1 accuracy/ })).toHaveTextContent("85%60%");
  });

  it("surfaces other API errors", async () => {
    mockFetch(500, { error: "eval summary is malformed" });
    render(<EvalPanel />);
    expect(await screen.findByRole("alert")).toHaveTextContent("eval summary is malformed");
  });
});
