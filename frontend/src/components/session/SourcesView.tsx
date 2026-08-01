"use client";

import { Empty } from "@/components/primitives";
import type { CitedDocument, Interjection } from "@/lib/api/types";

/**
 * The audit surface.
 *
 * Of everything Kindred could have read, this is what it actually used. Every passage
 * links back to the claim that used it, which is the hop that makes the reasoning
 * checkable rather than merely presented.
 */
export function SourcesView({
  sources,
  interjections,
  highlightedDocument,
  onJumpToClaim,
}: {
  sources: CitedDocument[];
  interjections: Interjection[];
  highlightedDocument: string | null;
  onJumpToClaim: (interjectionId: string) => void;
}) {
  if (sources.length === 0) {
    return (
      <Empty
        title="No sources cited in this session."
        hint="Kindred did not reference any documents — nothing here to audit."
      />
    );
  }

  const headlineFor = (id: string) =>
    interjections.find((claim) => claim.id === id)?.headline ?? "View claim";

  return (
    <div className="flex flex-col gap-3">
      {sources.map((source) => (
        <section
          key={source.document_id}
          id={`source-${source.document_id}`}
          className={`scroll-mt-4 rounded-lg border p-4 ${
            source.document_id === highlightedDocument ? "flash" : ""
          }`}
          style={{
            borderColor:
              source.document_id === highlightedDocument ? "var(--color-flag-500)" : "var(--border)",
            background: "var(--bg-raised)",
          }}
        >
          <header className="mb-3 flex items-baseline justify-between gap-3">
            <h3 className="font-mono text-[13px] font-medium">{source.filename}</h3>
            <span className="shrink-0 text-[11px]" style={{ color: "var(--text-faint)" }}>
              {source.citation_count} citation{source.citation_count === 1 ? "" : "s"} ·{" "}
              {source.interjection_ids.length} claim
              {source.interjection_ids.length === 1 ? "" : "s"}
            </span>
          </header>

          <div className="flex flex-col gap-2">
            {source.quotes.map((quote, i) => (
              <div key={`${quote.chunk_id}-${i}`}>
                <div className="quote-doc rounded-r px-3 py-2 text-[12px] leading-snug" style={{ color: "var(--text-muted)" }}>
                  {quote.page ? (
                    <span className="mr-2 font-sans text-[10.5px]" style={{ color: "var(--color-flag-500)" }}>
                      p.{quote.page}
                    </span>
                  ) : null}
                  {quote.quote}
                </div>
                <div className="mt-1 flex flex-col items-start gap-0.5">
                  {quote.interjection_ids.map((claimId) => (
                    <button
                      key={claimId}
                      onClick={() => onJumpToClaim(claimId)}
                      className="max-w-full truncate text-left text-[11px] transition-opacity hover:opacity-70"
                      style={{ color: "var(--color-accent-500)" }}
                    >
                      ↳ used in: {headlineFor(claimId)}
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
