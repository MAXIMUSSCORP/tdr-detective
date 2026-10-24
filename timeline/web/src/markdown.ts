import { formatLocalDateTime, formatUtcOffset } from "./format";
import type { BugReport, EvidenceItem } from "./types";

function oneLine(text: string): string {
  return text.replace(/\s*\r?\n\s*/g, " ").trim();
}

function cell(text: string): string {
  return oneLine(text).replace(/\|/g, "\\|");
}

/**
 * Render a bug report as GitHub-flavoured Markdown, resolving evidence ids
 * to their labels and local timestamps where the incident has them.
 */
export function bugReportToMarkdown(report: BugReport, evidence: EvidenceItem[], offsetMinutes: number): string {
  const byId = new Map(evidence.map((item) => [item.id, item]));
  const occurred = `${formatLocalDateTime(report.occurred_at, offsetMinutes)} (${formatUtcOffset(offsetMinutes)})`;

  const lines = [
    `## ${oneLine(report.title)}`,
    "",
    report.summary,
    "",
    "| Field | Value |",
    "| --- | --- |",
    `| Severity | ${cell(report.severity)} |`,
    `| Occurred | ${cell(occurred)} |`,
    `| OS build | ${cell(report.os_build)} |`,
    `| GPU | ${cell(report.gpu)} |`,
    `| Driver | ${cell(report.driver_version)} |`,
    "",
    "### Steps to reproduce",
    "",
    ...report.steps_to_reproduce.map((step, i) => `${i + 1}. ${oneLine(step)}`),
    "",
    "### Expected",
    "",
    report.expected,
    "",
    "### Actual",
    "",
    report.actual,
    "",
    "### Evidence",
    "",
    ...report.evidence.map((id) => {
      const item = byId.get(id);
      return item
        ? `- \`${id}\` ${formatLocalDateTime(item.time, offsetMinutes)}: ${oneLine(item.label)}`
        : `- \`${id}\``;
    }),
  ];
  return `${lines.join("\n")}\n`;
}
