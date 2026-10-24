import { useId } from "react";
import { categoryLabel, formatPercent } from "../format";
import type { EvidenceItem, RankedCause } from "../types";
import { SeverityGlyph } from "./Evidence";

export interface Highlight {
  /** What triggered the highlight, so the same control can toggle it off. */
  source: string;
  ids: ReadonlySet<string>;
}

interface CauseListProps {
  causes: RankedCause[];
  evidence: EvidenceItem[];
  highlight: Highlight | null;
  onHighlight: (highlight: Highlight | null) => void;
}

export function CauseList({ causes, evidence, highlight, onHighlight }: CauseListProps) {
  const headingId = useId();
  const byId = new Map(evidence.map((item) => [item.id, item]));

  const toggle = (source: string, ids: string[]) => {
    onHighlight(highlight?.source === source ? null : { source, ids: new Set(ids) });
  };

  return (
    <section className="card" aria-labelledby={headingId}>
      <h2 id={headingId}>Ranked causes</h2>
      <ol className="causes">
        {causes.map((cause, rank) => {
          const refs = cause.evidence_refs ?? [];
          const source = `cause-${rank}`;
          const active = highlight?.source === source;
          return (
            <li key={`${cause.category}-${rank}`} className={`cause ${active ? "is-active" : ""}`}>
              <div className="cause-head">
                <span className="cause-rank" aria-hidden="true">
                  {rank + 1}
                </span>
                <h3>{categoryLabel(cause.category)}</h3>
                <span className="confidence">
                  <span className="confidence-bar" aria-hidden="true">
                    <span style={{ width: formatPercent(cause.confidence) }} />
                  </span>
                  <span>{formatPercent(cause.confidence)}</span>
                  <span className="sr-only"> confidence</span>
                </span>
              </div>
              <p>{cause.rationale}</p>
              {refs.length > 0 ? (
                <div className="cause-refs">
                  <button
                    type="button"
                    className="button"
                    aria-pressed={active}
                    onClick={() => toggle(source, refs)}
                  >
                    {active ? "Clear highlight" : `Highlight ${refs.length} on timeline`}
                  </button>
                  <ul className="chips" aria-label={`Evidence cited for ${categoryLabel(cause.category)}`}>
                    {refs.map((id) => {
                      const item = byId.get(id);
                      const chipSource = `evidence-${id}`;
                      return (
                        <li key={id}>
                          {item ? (
                            <button
                              type="button"
                              className="chip"
                              aria-pressed={highlight?.source === chipSource}
                              title={item.label}
                              onClick={() => toggle(chipSource, [id])}
                            >
                              <SeverityGlyph severity={item.severity ?? "info"} size={10} />
                              <code>{id}</code>
                            </button>
                          ) : (
                            <span className="chip is-missing" title="Not present in this incident's evidence">
                              <code>{id}</code>
                            </span>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                </div>
              ) : null}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
