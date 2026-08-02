import { timecode } from "@/lib/format";
import type { SessionReview } from "@/lib/review";

export function ExecutiveSummary({
  review,
  onJumpToTranscript,
}: {
  review: SessionReview;
  onJumpToTranscript: (segmentId: string) => void;
}) {
  const { primaryTakeaway, topics, followUps, counts } = review;

  return (
    <section
      aria-labelledby="executive-summary-title"
      className="mb-6 overflow-hidden rounded-xl border"
      data-follow-up-total={counts.total}
      data-follow-up-outstanding={counts.outstanding}
      data-follow-up-resolved={counts.resolved}
      style={{ borderColor: "var(--border)", background: "var(--bg-raised)" }}
    >
      <div className="px-5 py-5 sm:px-6">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 id="executive-summary-title" className="text-[11px] font-semibold uppercase tracking-[0.14em]">
            Executive summary
          </h2>
          <span className="text-[10px]" style={{ color: "var(--text-faint)" }}>
            Derived from finalized transcript
          </span>
        </div>

        <div className="mt-4">
          <div className="text-[10px] uppercase tracking-wider" style={{ color: "var(--text-faint)" }}>
            Primary takeaway
          </div>
          <p className="mt-1 max-w-2xl text-[17px] font-medium leading-snug tracking-tight">
            {primaryTakeaway.text}
          </p>
          {primaryTakeaway.evidenceSegmentId ? (
            <button
              onClick={() => onJumpToTranscript(primaryTakeaway.evidenceSegmentId!)}
              className="mt-2 text-[11px] hover:underline"
              style={{ color: "var(--color-accent-500)" }}
            >
              View transcript evidence
              {primaryTakeaway.evidenceStartMs === null ? "" : ` · ${timecode(primaryTakeaway.evidenceStartMs)}`}
            </button>
          ) : null}
        </div>
      </div>

      <div className="grid border-t md:grid-cols-[minmax(0,1fr)_14rem]" style={{ borderColor: "var(--border)" }}>
        <div className="px-5 py-5 sm:px-6">
          <h3 className="text-[12px] font-semibold">Key topics and takeaways</h3>
          <div className="mt-2 divide-y" style={{ borderColor: "var(--border)" }}>
            {topics.map((topic) => {
              const content = (
                <>
                  <span className="flex items-baseline justify-between gap-3">
                    <span className="text-[12px] font-medium">{topic.label}</span>
                    <span className="shrink-0 text-[10px] tabular-nums" style={{ color: "var(--text-faint)" }}>
                      {topic.mentions} {topic.mentions === 1 ? "mention" : "mentions"}
                    </span>
                  </span>
                  <span className="mt-1 block text-[12.5px] leading-relaxed" style={{ color: "var(--text-muted)" }}>
                    {topic.takeaway}
                  </span>
                </>
              );

              return topic.evidenceSegmentId ? (
                <button
                  key={topic.label}
                  onClick={() => onJumpToTranscript(topic.evidenceSegmentId!)}
                  className="block w-full py-3 text-left transition-opacity hover:opacity-75"
                >
                  {content}
                </button>
              ) : (
                <div key={topic.label} className="py-3">
                  {content}
                </div>
              );
            })}
          </div>
        </div>

        <aside className="border-t px-5 py-5 md:border-t-0 md:border-l" style={{ borderColor: "var(--border)" }}>
          <div className="text-[10px] uppercase tracking-wider" style={{ color: "var(--text-faint)" }}>
            Follow-ups
          </div>
          <div className="mt-1 text-[30px] font-semibold leading-none tabular-nums">{counts.total}</div>
          <div className="mt-4 space-y-2 text-[12px]">
            <div className="flex items-center justify-between gap-3">
              <span style={{ color: "var(--text-muted)" }}>Outstanding</span>
              <span className="font-medium tabular-nums" style={{ color: counts.outstanding ? "var(--color-flag-500)" : "var(--text)" }}>
                {counts.outstanding}
              </span>
            </div>
            <div className="flex items-center justify-between gap-3">
              <span style={{ color: "var(--text-muted)" }}>Resolved</span>
              <span className="font-medium tabular-nums" style={{ color: "var(--color-speak-500)" }}>
                {counts.resolved}
              </span>
            </div>
          </div>
          <p className="mt-4 text-[10.5px] leading-relaxed" style={{ color: "var(--text-faint)" }}>
            Resolution requires a later completion statement from the same speaker.
          </p>
        </aside>
      </div>

      <div className="border-t px-5 py-5 sm:px-6" style={{ borderColor: "var(--border)" }}>
        <div className="flex items-baseline justify-between gap-3">
          <h3 className="text-[12px] font-semibold">Follow-up review</h3>
          <span className="text-[10px]" style={{ color: "var(--text-faint)" }}>
            Owner commitments only
          </span>
        </div>

        {followUps.length === 0 ? (
          <p className="mt-3 text-[12px]" style={{ color: "var(--text-muted)" }}>
            No explicit owner commitments were detected.
          </p>
        ) : (
          <div className="mt-2 divide-y" style={{ borderColor: "var(--border)" }}>
            {followUps.map((followUp) => (
              <button
                key={followUp.id}
                onClick={() => onJumpToTranscript(followUp.evidenceSegmentId)}
                className="grid w-full gap-2 py-3 text-left sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start sm:gap-4"
              >
                <span>
                  <span className="block text-[13px] font-medium leading-snug">{followUp.action}</span>
                  <span className="mt-1 block text-[11px]" style={{ color: "var(--text-muted)" }}>
                    {followUp.owner}
                    {followUp.due ? ` · ${followUp.due}` : " · No due date stated"}
                    {` · ${timecode(followUp.evidenceStartMs)}`}
                  </span>
                </span>
                <span
                  className="w-fit rounded px-2 py-0.5 text-[10px] font-medium capitalize"
                  style={{
                    background:
                      followUp.status === "resolved"
                        ? "color-mix(in srgb, var(--color-speak-500) 14%, transparent)"
                        : "color-mix(in srgb, var(--color-flag-500) 14%, transparent)",
                    color:
                      followUp.status === "resolved" ? "var(--color-speak-500)" : "var(--color-flag-500)",
                  }}
                >
                  {followUp.status}
                </span>
              </button>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
