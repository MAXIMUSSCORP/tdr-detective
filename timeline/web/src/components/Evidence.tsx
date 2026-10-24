import { formatLocalTime, formatValue } from "../format";
import type { EvidenceItem, Severity } from "../types";

export const SEVERITIES: readonly Severity[] = ["info", "warning", "error", "critical"];

/** Severity is encoded by shape as well as colour, so it never relies on colour alone. */
export function SeverityGlyph({ severity, size = 14 }: { severity: Severity; size?: number }) {
  let shape;
  switch (severity) {
    case "info":
      shape = <circle cx="7" cy="7" r="5" />;
      break;
    case "warning":
      shape = <path d="M7 1.5 12.8 12H1.2Z" />;
      break;
    case "error":
      shape = <rect x="2" y="2" width="10" height="10" rx="1" />;
      break;
    case "critical":
      shape = <path d="M7 0.8 13.2 7 7 13.2 0.8 7Z" />;
      break;
  }
  return (
    <svg className={`glyph sev-${severity}`} width={size} height={size} viewBox="0 0 14 14" aria-hidden="true">
      {shape}
    </svg>
  );
}

export function EvidenceSummary({ item, offsetMinutes }: { item: EvidenceItem; offsetMinutes: number }) {
  const severity = item.severity ?? "info";
  return (
    <>
      <p className="tip-title">
        <SeverityGlyph severity={severity} size={12} /> {item.label}
      </p>
      <p className="tip-meta">
        {formatLocalTime(item.time, offsetMinutes)} · {severity}
        {typeof item.value === "number" ? ` · ${formatValue(item.value, item.unit)}` : ""}
      </p>
      {item.detail ? <p className="tip-detail">{item.detail}</p> : null}
      <p className="tip-id">
        <code>{item.id}</code>
      </p>
    </>
  );
}
