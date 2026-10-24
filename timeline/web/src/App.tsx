import { lazy, Suspense, useEffect, useRef, useSyncExternalStore } from "react";
import { ErrorBoundary } from "./ErrorBoundary";
import { IncidentListPage } from "./IncidentListPage";

// The detail page carries the chart library; load it only when an incident is opened.
const IncidentPage = lazy(() => import("./IncidentPage"));

type Route = { name: "list" } | { name: "incident"; id: string };

function subscribe(onChange: () => void) {
  window.addEventListener("hashchange", onChange);
  return () => window.removeEventListener("hashchange", onChange);
}

function parseRoute(hash: string): Route {
  const match = /^#\/incidents\/([^/?#]+)$/.exec(hash);
  if (match?.[1]) {
    try {
      return { name: "incident", id: decodeURIComponent(match[1]) };
    } catch {
      return { name: "list" };
    }
  }
  return { name: "list" };
}

/** Hash routing keeps the app servable as static files with no server-side fallback. */
function useHash(): string {
  return useSyncExternalStore(subscribe, () => window.location.hash);
}

export function App() {
  const hash = useHash();
  const route = parseRoute(hash);
  const mainRef = useRef<HTMLElement>(null);
  const firstRender = useRef(true);

  useEffect(() => {
    // Move focus to the new page on navigation so keyboard and screen reader users land on it.
    if (firstRender.current) {
      firstRender.current = false;
      return;
    }
    mainRef.current?.focus();
    window.scrollTo(0, 0);
  }, [hash]);

  return (
    <>
      <a className="skip-link" href="#main" onClick={(e) => {
        e.preventDefault();
        mainRef.current?.focus();
      }}>
        Skip to content
      </a>
      <header className="site-header">
        <a href="#/" className="brand">
          TDR Detective <span>incident timeline</span>
        </a>
      </header>
      <main id="main" ref={mainRef} tabIndex={-1}>
        {route.name === "incident" ? (
          <ErrorBoundary key={route.id}>
            <Suspense fallback={<p className="muted">Loading incident…</p>}>
              <IncidentPage id={route.id} />
            </Suspense>
          </ErrorBoundary>
        ) : (
          <IncidentListPage />
        )}
      </main>
    </>
  );
}
