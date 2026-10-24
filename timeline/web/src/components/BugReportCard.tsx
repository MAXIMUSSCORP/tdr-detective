import { useId, useMemo, useState } from "react";
import { formatLocalDateTime, formatUtcOffset } from "../format";
import { bugReportToMarkdown } from "../markdown";
import type { BugReport, EvidenceItem } from "../types";

interface BugReportCardProps {
  report: BugReport;
  evidence: EvidenceItem[];
  offsetMinutes: number;
}

type CopyState = "idle" | "copied" | "failed";

export function BugReportCard({ report, evidence, offsetMinutes }: BugReportCardProps) {
  const headingId = useId();
  const [copyState, setCopyState] = useState<CopyState>("idle");
  const markdown = useMemo(
    () => bugReportToMarkdown(report, evidence, offsetMinutes),
    [report, evidence, offsetMinutes],
  );

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(markdown);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  };

  return (
    <section className="card bug-report" aria-labelledby={headingId}>
      <div className="card-head">
        <h2 id={headingId}>Bug report draft</h2>
        <button type="button" className="button primary" onClick={copy}>
          Copy as Markdown
        </button>
        <span className="copy-status" role="status">
          {copyState === "copied" ? "Copied to clipboard" : copyState === "failed" ? "Copy failed. Use the Markdown below." : ""}
        </span>
      </div>
      <h3>{report.title}</h3>
      <p>{report.summary}</p>
      <dl className="facts">
        <div>
          <dt>Severity</dt>
          <dd className={`badge report-${report.severity}`}>{report.severity}</dd>
        </div>
        <div>
          <dt>Occurred</dt>
          <dd>
            {formatLocalDateTime(report.occurred_at, offsetMinutes)} {formatUtcOffset(offsetMinutes)}
          </dd>
        </div>
        <div>
          <dt>OS build</dt>
          <dd>{report.os_build}</dd>
        </div>
        <div>
          <dt>GPU</dt>
          <dd>{report.gpu}</dd>
        </div>
        <div>
          <dt>Driver</dt>
          <dd>{report.driver_version}</dd>
        </div>
      </dl>
      <h4>Steps to reproduce</h4>
      <ol>
        {report.steps_to_reproduce.map((step, i) => (
          <li key={i}>{step}</li>
        ))}
      </ol>
      <div className="expected-actual">
        <div>
          <h4>Expected</h4>
          <p>{report.expected}</p>
        </div>
        <div>
          <h4>Actual</h4>
          <p>{report.actual}</p>
        </div>
      </div>
      <details open={copyState === "failed"}>
        <summary>Markdown</summary>
        <pre>
          <code>{markdown}</code>
        </pre>
      </details>
    </section>
  );
}
