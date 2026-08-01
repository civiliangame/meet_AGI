"use client";

import { useState } from "react";
import { Markdown } from "@/components/Markdown";
import type { Interjection, InterjectionKind } from "@/lib/api/types";

/** A contradiction is Kindred disagreeing with a person; an answer is Kindred being
 *  asked. They should not look the same. Colour is noticeable but never alarming —
 *  an aggressive treatment makes a false positive read as an accusation. */
const KIND_STYLE: Record<string, { label: string; color: string }> = {
  contradiction: { label: "Contradiction", color: "var(--color-flag-500)" },
  correction: { label: "Correction", color: "var(--color-flag-500)" },
  context: { label: "Context", color: "var(--color-accent-500)" },
  answer: { label: "Answer", color: "var(--color-speak-500)" },
  clarification: { label: "Clarification", color: "var(--color-speak-500)" },
};

export function ClaimCard({
  claim,
  onJumpToTranscript,
  onJumpToSource,
  onApprove,
  onDismiss,
  highlighted,
}: {
  claim: Interjection;
  onJumpToTranscript: (segmentId: string) => void;
  onJumpToSource: (documentId: string) => void;
  onApprove?: (id: string) => void;
  onDismiss?: (id: string) => void;
  highlighted?: boolean;
}) {
  const [showCitations, setShowCitations] = useState(true);
  const kind = KIND_STYLE[claim.kind as InterjectionKind] ?? {
    label: String(claim.kind),
    color: "var(--text-muted)",
  };
  const triggerSegment = claim.trigger?.segment_ids?.[0];
  const pending = claim.status === "proposed";

  return (
    <article
      id={`claim-${claim.id}`}
      className={`scroll-mt-4 rounded-lg border p-4 ${highlighted ? "flash" : ""}`}
      style={{
        borderColor: highlighted ? kind.color : "var(--border)",
        background: "var(--bg-raised)",
      }}
    >
      <div className="mb-2 flex items-start gap-2.5">
        <span
          className="mt-0.5 shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide"
          style={{
            background: `color-mix(in srgb, ${kind.color} 15%, transparent)`,
            color: kind.color,
          }}
        >
          {kind.label}
        </span>
        <h3 className="flex-1 text-[14px] font-medium leading-snug">{claim.headline}</h3>
        {claim.spoken ? (
          <span
            className="shrink-0 rounded px-1.5 py-0.5 text-[10px]"
            style={{
              background: "color-mix(in srgb, var(--color-speak-500) 15%, transparent)",
              color: "var(--color-speak-500)",
            }}
            title="Kindred said this out loud in the meeting"
          >
            spoken
          </span>
        ) : null}
      </div>

      {claim.trigger?.quote ? (
        <button
          onClick={() => triggerSegment && onJumpToTranscript(triggerSegment)}
          disabled={!triggerSegment}
          className="quote-human mb-3 block w-full rounded-r px-3 py-2 text-left text-[12.5px] italic transition-opacity hover:opacity-80 disabled:cursor-default"
          style={{ color: "var(--text-muted)" }}
          title={triggerSegment ? "Jump to this line in the transcript" : undefined}
        >
          “{claim.trigger.quote}”
        </button>
      ) : null}

      <Markdown text={claim.body_md} />

      <div className="mt-3 border-t pt-3" style={{ borderColor: "var(--border)" }}>
        {claim.citations.length === 0 ? (
          // An unsupported assertion is exactly what an auditor is looking for, so it
          // gets a visible line rather than an empty section.
          <p className="text-[11.5px]" style={{ color: "var(--color-flag-500)" }}>
            No sources cited — Kindred asserted this without supporting evidence.
          </p>
        ) : (
          <>
            <button
              onClick={() => setShowCitations((v) => !v)}
              className="mb-2 text-[10px] uppercase tracking-wider transition-colors"
              style={{ color: "var(--text-faint)" }}
            >
              {showCitations ? "▾" : "▸"} {claim.citations.length} source
              {claim.citations.length === 1 ? "" : "s"}
            </button>

            {showCitations ? (
              <div className="flex flex-col gap-1.5">
                {claim.citations.map((citation, i) => (
                  <button
                    key={`${citation.chunk_id}-${i}`}
                    onClick={() => onJumpToSource(citation.document_id)}
                    className="quote-doc rounded-r px-3 py-2 text-left transition-opacity hover:opacity-80"
                  >
                    <div
                      className="mb-1 flex items-baseline gap-2 font-sans text-[10.5px]"
                      style={{ color: "var(--color-flag-500)" }}
                    >
                      <span className="font-medium">{citation.filename}</span>
                      {citation.page ? <span>p.{citation.page}</span> : null}
                    </div>
                    <div className="text-[12px] leading-snug" style={{ color: "var(--text-muted)" }}>
                      {citation.quote}
                    </div>
                  </button>
                ))}
              </div>
            ) : null}
          </>
        )}
      </div>

      <div className="mt-3 flex items-center gap-2 border-t pt-3" style={{ borderColor: "var(--border)" }}>
        <span className="shrink-0 text-[10px] uppercase tracking-wider" style={{ color: "var(--text-faint)" }}>
          {claim.status === "posted" ? "Posted to chat" : claim.status}
        </span>
        <p
          className="min-w-0 flex-1 truncate text-[11.5px]"
          style={{ color: "var(--text-faint)" }}
          title={claim.chat_alert}
        >
          {claim.chat_alert}
        </p>
      </div>

      {pending && (onApprove || onDismiss) ? (
        <div className="mt-3 flex gap-2">
          <button
            onClick={() => onApprove?.(claim.id)}
            className="rounded-md px-3 py-1.5 text-[12px] font-medium"
            style={{ background: "var(--color-accent-500)", color: "#fff" }}
          >
            Approve &amp; post
          </button>
          <button
            onClick={() => onDismiss?.(claim.id)}
            className="rounded-md border px-3 py-1.5 text-[12px]"
            style={{ borderColor: "var(--border)", color: "var(--text-muted)" }}
          >
            Dismiss
          </button>
        </div>
      ) : null}
    </article>
  );
}
