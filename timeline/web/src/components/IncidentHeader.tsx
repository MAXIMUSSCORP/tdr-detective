import { categoryLabel, formatLocalDateTime, formatPercent, formatUtcOffset } from "../format";
import type { Incident } from "../types";

export function IncidentHeader({ incident }: { incident: Incident }) {
  const { system, diagnosis } = incident;
  const primary = diagnosis.causes[0];
  const offset = system.utc_offset_minutes;

  return (
    <header className="incident-header">
      <p className="eyebrow">
        Incident <code>{incident.id}</code> · analysed {formatLocalDateTime(incident.created_at, offset)}{" "}
        {formatUtcOffset(offset)}
      </p>
      <h1>{incident.question}</h1>
      <dl className="facts">
        <div>
          <dt>System</dt>
          <dd>
            {system.os_caption}, build {system.os_build}
          </dd>
        </div>
        {system.gpus.map((gpu, i) => (
          <div key={`${gpu.name}-${i}`}>
            <dt>{system.gpus.length > 1 ? `GPU ${i + 1}` : "GPU"}</dt>
            <dd>
              {gpu.name}, driver {gpu.driver_version}
              {gpu.driver_date ? ` (${gpu.driver_date})` : ""}
            </dd>
          </div>
        ))}
        {primary ? (
          <div className="primary-cause">
            <dt>Primary cause</dt>
            <dd>
              <strong>{categoryLabel(primary.category)}</strong>{" "}
              <span className="badge">{formatPercent(primary.confidence)} confidence</span>
            </dd>
          </div>
        ) : null}
      </dl>
    </header>
  );
}
