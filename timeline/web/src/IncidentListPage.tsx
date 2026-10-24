import { api, useApi } from "./api";
import { categoryLabel, formatPercent } from "./format";

export function IncidentListPage() {
  const state = useApi(api.listIncidents, "incidents");

  return (
    <div className="page-narrow">
      <h1>Incidents</h1>
      {state.status === "loading" ? <p className="muted">Loading incidents…</p> : null}
      {state.status === "error" ? <p role="alert">Could not load incidents: {state.error.message}</p> : null}
      {state.status === "ok" && state.data.items.length === 0 ? (
        <div className="card empty">
          <p>
            <strong>No incidents yet.</strong>
          </p>
          <p className="muted">
            Load the demo with <code>npm run seed</code>, or POST an incident to <code>/api/incidents</code>.
          </p>
        </div>
      ) : null}
      {state.status === "ok" && state.data.items.length > 0 ? (
        <>
          <p className="muted small">
            Showing {state.data.items.length} of {state.data.total}, newest first.
          </p>
          <ul className="incident-list">
            {state.data.items.map((item) => (
              <li key={item.id} className="card incident-row">
                <a href={`#/incidents/${encodeURIComponent(item.id)}`}>{item.question}</a>
                <p className="muted">
                  <span className="badge">
                    {categoryLabel(item.primary_category)} · {formatPercent(item.confidence)}
                  </span>{" "}
                  {item.gpu ?? "Unknown GPU"} · build {item.os_build} · {item.evidence_count} evidence items ·{" "}
                  <time dateTime={item.created_at}>{item.created_at.slice(0, 16).replace("T", " ")} UTC</time>
                </p>
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </div>
  );
}
