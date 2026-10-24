import { useEffect, useState } from "react";
import type { EvalSummary, Incident, IncidentPage } from "./types";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, { signal, headers: { Accept: "application/json" } });
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { error?: unknown };
      if (typeof body.error === "string") message = body.error;
    } catch {
      // Non-JSON error body: keep the status line.
    }
    throw new ApiError(res.status, message);
  }
  return (await res.json()) as T;
}

export const api = {
  listIncidents: (signal?: AbortSignal) => getJson<IncidentPage>("/api/incidents?limit=100", signal),
  getIncident: (id: string, signal?: AbortSignal) =>
    getJson<Incident>(`/api/incidents/${encodeURIComponent(id)}`, signal),
  getEvals: (signal?: AbortSignal) => getJson<EvalSummary>("/api/evals", signal),
};

export type AsyncState<T> =
  | { status: "loading" }
  | { status: "ok"; data: T }
  | { status: "error"; error: ApiError | Error };

/** Run `load` when `key` changes, aborting the previous request. */
export function useApi<T>(load: (signal: AbortSignal) => Promise<T>, key: string): AsyncState<T> {
  const [state, setState] = useState<{ key: string; value: AsyncState<T> }>({ key, value: { status: "loading" } });

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal).then(
      (data) => setState({ key, value: { status: "ok", data } }),
      (error: unknown) => {
        if (controller.signal.aborted) return;
        setState({ key, value: { status: "error", error: error instanceof Error ? error : new Error(String(error)) } });
      },
    );
    return () => controller.abort();
    // `load` is expected to be stable for a given key.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  return state.key === key ? state.value : { status: "loading" };
}
