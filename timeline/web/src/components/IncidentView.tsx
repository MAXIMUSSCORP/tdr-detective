import { useId, useState } from "react";
import type { Incident } from "../types";
import { BugReportCard } from "./BugReportCard";
import { CauseList, type Highlight } from "./CauseList";
import { IncidentHeader } from "./IncidentHeader";
import { Timeline } from "./Timeline";

const NO_IDS: ReadonlySet<string> = new Set();

/** Everything about one incident except the eval side panel. Owns the cause-to-evidence highlight. */
export function IncidentView({ incident }: { incident: Incident }) {
  const [highlight, setHighlight] = useState<Highlight | null>(null);
  const nextStepsId = useId();
  const offset = incident.system.utc_offset_minutes;
  const { diagnosis } = incident;
  const focusTime = Date.parse(diagnosis.bug_report.occurred_at);

  return (
    <div className="incident">
      <IncidentHeader incident={incident} />
      <Timeline
        evidence={incident.evidence}
        offsetMinutes={offset}
        focusTime={focusTime}
        highlighted={highlight?.ids ?? NO_IDS}
      />
      <CauseList
        causes={diagnosis.causes}
        evidence={incident.evidence}
        highlight={highlight}
        onHighlight={setHighlight}
      />
      <section className="card" aria-labelledby={nextStepsId}>
        <h2 id={nextStepsId}>Next steps</h2>
        <ol>
          {diagnosis.next_steps.map((step, i) => (
            <li key={i}>{step}</li>
          ))}
        </ol>
      </section>
      <BugReportCard report={diagnosis.bug_report} evidence={incident.evidence} offsetMinutes={offset} />
    </div>
  );
}
