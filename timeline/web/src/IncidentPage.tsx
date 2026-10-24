import { api, ApiError, useApi } from "./api";
import { EvalPanel } from "./components/EvalPanel";
import { IncidentView } from "./components/IncidentView";

export default function IncidentPage({ id }: { id: string }) {
  const state = useApi((signal) => api.getIncident(id, signal), `incident:${id}`);

  if (state.status === "loading") return <p className="muted">Loading incident…</p>;
  if (state.status === "error") {
    const notFound = state.error instanceof ApiError && state.error.status === 404;
    return (
      <div className="card empty" role="alert">
        <p>
          <strong>{notFound ? "Incident not found." : "Could not load the incident."}</strong>
        </p>
        <p className="muted">{notFound ? <code>{id}</code> : state.error.message}</p>
        <p>
          <a href="#/">Back to all incidents</a>
        </p>
      </div>
    );
  }
  return (
    <div className="detail-layout">
      <IncidentView incident={state.data} />
      <EvalPanel run={state.data.run} />
    </div>
  );
}
