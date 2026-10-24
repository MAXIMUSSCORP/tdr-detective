import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { bugReportToMarkdown } from "../markdown";
import { demoIncident } from "../test/fixtures";
import type { BugReport } from "../types";
import { BugReportCard } from "./BugReportCard";

const report = demoIncident.diagnosis.bug_report;
const offset = demoIncident.system.utc_offset_minutes;

describe("bugReportToMarkdown", () => {
  it("renders headings, a facts table, numbered steps and resolved evidence", () => {
    const md = bugReportToMarkdown(report, demoIncident.evidence, offset);
    expect(md.startsWith(`## ${report.title}\n\n${report.summary}\n`)).toBe(true);
    expect(md).toContain("| Occurred | 2026-10-20 09:42:07 (UTC−04:00) |");
    expect(md).toContain("| Driver | 32.0.21013.1000 |");
    expect(md).toContain("### Steps to reproduce\n\n1. Boot Windows 11");
    expect(md).toContain("\n3. Watch for the display going black and recovering.\n");
    expect(md).toContain(
      "- `ev-lke-141` 2026-10-20 09:42:09: LiveKernelEvent 141 (VIDEO_ENGINE_TIMEOUT_DETECTED)",
    );
    expect(md.endsWith("\n")).toBe(true);
  });

  it("escapes table pipes, flattens multi-line text and keeps unknown evidence ids", () => {
    const custom: BugReport = {
      ...report,
      title: "Driver\nreset",
      gpu: "GPU | with pipe",
      evidence: ["multi", "not-in-incident"],
    };
    const evidence = [{ id: "multi", kind: "event" as const, time: report.occurred_at, label: "line one\n- line two" }];
    const md = bugReportToMarkdown(custom, evidence, 0);
    expect(md.startsWith("## Driver reset\n")).toBe(true);
    expect(md).toContain("| GPU | GPU \\| with pipe |");
    expect(md).toContain(": line one - line two\n");
    expect(md).toContain("- `not-in-incident`\n");
  });
});

describe("<BugReportCard>", () => {
  it("copies the report as Markdown", async () => {
    const user = userEvent.setup();
    render(<BugReportCard report={report} evidence={demoIncident.evidence} offsetMinutes={offset} />);

    await user.click(screen.getByRole("button", { name: "Copy as Markdown" }));

    expect(await navigator.clipboard.readText()).toBe(bugReportToMarkdown(report, demoIncident.evidence, offset));
    expect(screen.getByRole("status")).toHaveTextContent("Copied to clipboard");
  });

  it("reports a failed copy and opens the Markdown for manual copying", async () => {
    const user = userEvent.setup();
    render(<BugReportCard report={report} evidence={demoIncident.evidence} offsetMinutes={offset} />);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: () => Promise.reject(new Error("denied")) },
    });

    await user.click(screen.getByRole("button", { name: "Copy as Markdown" }));

    expect(screen.getByRole("status")).toHaveTextContent("Copy failed");
    expect(screen.getByText("Markdown").closest("details")).toHaveAttribute("open");
  });
});
