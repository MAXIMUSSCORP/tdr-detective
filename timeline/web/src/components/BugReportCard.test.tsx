import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { bugReportToMarkdown, escapeInline } from "../markdown";
import { demoIncident } from "../test/fixtures";
import type { BugReport } from "../types";
import { BugReportCard } from "./BugReportCard";

const report = demoIncident.diagnosis.bug_report;
const offset = demoIncident.system.utc_offset_minutes;

describe("bugReportToMarkdown", () => {
  it("renders headings, a facts table, numbered steps and resolved evidence", () => {
    const md = bugReportToMarkdown(report, demoIncident.evidence, offset);
    expect(md.startsWith(`## ${escapeInline(report.title)}\n\n\`\`\`text\n${report.summary}\n\`\`\`\n`)).toBe(true);
    expect(md).toContain("| Occurred | 2026-10-20 09:42:07 (UTC\u221204:00) |");
    expect(md).toContain("| Driver | 32.0.21013.1000 |");
    expect(md).toContain("### Steps to reproduce\n\n1. Boot Windows 11");
    expect(md).toContain("\n3. Watch for the display going black and recovering.\n");
    expect(md).toContain("### Actual\n\n```text\nFrame time spiked");
    expect(md).toContain(
      "- `ev-lke-141` 2026-10-20 09:42:09: LiveKernelEvent 141 \\(VIDEO\\_ENGINE\\_TIMEOUT\\_DETECTED\\)",
    );
    expect(md.endsWith("\n")).toBe(true);
  });

  it("neutralises hostile content: images, links, HTML, pipes and backtick runs", () => {
    const hostile: BugReport = {
      ...report,
      title: "![x](http://evil/p.png) <img src=x onerror=alert(1)>",
      summary: "line one\n```\n<script>alert(1)</script>\n````",
      gpu: "GPU | [click](http://evil)",
      steps_to_reproduce: ["# not a heading\n![y](http://evil/q.png)"],
      expected: "fine",
      actual: "`code` and ``more``",
      evidence: ["ev`1`", "not-in-incident"],
    };
    const evidence = [{ id: "ev`1`", kind: "event" as const, time: report.occurred_at, label: "<b>bold</b> [x](y)\n- item" }];
    const md = bugReportToMarkdown(hostile, evidence, 0);

    expect(md).toContain("## \\!\\[x\\]\\(http://evil/p.png\\) \\<img src=x onerror=alert\\(1\\)\\>\n");
    // The summary contains runs of up to four backticks, so its fence is five long.
    expect(md).toContain("`````text\nline one\n```\n<script>alert(1)</script>\n````\n`````");
    expect(md).toContain("| GPU | GPU \\| \\[click\\]\\(http://evil\\) |");
    expect(md).toContain("1. \\# not a heading \\!\\[y\\]\\(http://evil/q.png\\)\n");
    expect(md).toContain("```text\n`code` and ``more``\n```");
    expect(md).toContain("- `` ev`1` `` 2026-10-20 13:42:07: ");
    expect(md).toContain(": \\<b\\>bold\\</b\\> \\[x\\]\\(y\\) - item\n");
    expect(md).toContain("- `not-in-incident`\n");
    // No live image syntax or <img> tag survives anywhere in the output.
    expect(md).not.toMatch(/(?<!\\)!(?<!\\)\[/);
    expect(md).not.toMatch(/(?<!\\)<img/);
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
