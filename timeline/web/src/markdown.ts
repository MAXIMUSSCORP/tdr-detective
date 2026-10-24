import { formatLocalDateTime, formatUtcOffset } from "./format";
import type { BugReport, EvidenceItem } from "./types";

// Report fields come from model output and captured logs, so all of it is treated as untrusted:
// nothing in it may turn into links, images, raw HTML or code that escapes its context.

function oneLine(text: string): string {
  return text.replace(/\s*\r?\n\s*/g, " ").trim();
}

/** Escape a single-line field so Markdown syntax, links, images and HTML render literally. */
export function escapeInline(text: string): string {
  return oneLine(text).replace(/[\\`*_[\]()<>!|#~]/g, "\\$&");
}

function longestBacktickRun(text: string): number {
  return Math.max(0, ...(text.match(/`+/g) ?? []).map((run) => run.length));
}

/** Inline code span whose fence is longer than any backtick run in the content. */
export function codeSpan(text: string): string {
  const content = oneLine(text);
  const fence = "`".repeat(longestBacktickRun(content) + 1);
  const pad = content.startsWith("`") || content.endsWith("`") ? " " : "";
  return `${fence}${pad}${content}${pad}${fence}`;
}

/** Fenced block for multi-line free text; the fence outlasts any backtick run inside. */
export function fencedBlock(text: string): string {
  const fence = "`".repeat(Math.max(3, longestBacktickRun(text) + 1));
  return `${fence}text\n${text.replace(/\r\n/g, "\n").trimEnd()}\n${fence}`;
}

/**
 * Render a bug report as GitHub-flavoured Markdown, resolving evidence ids
 * to their labels and local timestamps where the incident has them.
 */
export function bugReportToMarkdown(report: BugReport, evidence: EvidenceItem[], offsetMinutes: number): string {
  const byId = new Map(evidence.map((item) => [item.id, item]));
  const occurred = `${formatLocalDateTime(report.occurred_at, offsetMinutes)} (${formatUtcOffset(offsetMinutes)})`;

  const lines = [
    `## ${escapeInline(report.title)}`,
    "",
    fencedBlock(report.summary),
    "",
    "| Field | Value |",
    "| --- | --- |",
    `| Severity | ${escapeInline(report.severity)} |`,
    `| Occurred | ${occurred} |`,
    `| OS build | ${escapeInline(report.os_build)} |`,
    `| GPU | ${escapeInline(report.gpu)} |`,
    `| Driver | ${escapeInline(report.driver_version)} |`,
    "",
    "### Steps to reproduce",
    "",
    ...report.steps_to_reproduce.map((step, i) => `${i + 1}. ${escapeInline(step)}`),
    "",
    "### Expected",
    "",
    fencedBlock(report.expected),
    "",
    "### Actual",
    "",
    fencedBlock(report.actual),
    "",
    "### Evidence",
    "",
    ...report.evidence.map((id) => {
      const item = byId.get(id);
      return item
        ? `- ${codeSpan(id)} ${formatLocalDateTime(item.time, offsetMinutes)}: ${escapeInline(item.label)}`
        : `- ${codeSpan(id)}`;
    }),
  ];
  return `${lines.join("\n")}\n`;
}
