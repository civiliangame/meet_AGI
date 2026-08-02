"use client";

import { useMemo, useState } from "react";
import { Empty } from "@/components/primitives";
import { speakerColor, timecode } from "@/lib/format";
import type { Interjection, RosterEntry, TranscriptSegment } from "@/lib/api/types";

export function TranscriptView({
  transcript,
  interjections,
  roster,
  highlightedSegment,
  livePartials,
  onJumpToClaim,
}: {
  transcript: TranscriptSegment[];
  interjections: Interjection[];
  roster: RosterEntry[];
  highlightedSegment: string | null;
  livePartials?: TranscriptSegment[];
  onJumpToClaim: (interjectionId: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [speaker, setSpeaker] = useState<string>("");

  /** segment id -> the claim it triggered. The reverse of the claim→transcript jump,
   *  and the half that is easy to forget. */
  const claimBySegment = useMemo(() => {
    const map = new Map<string, Interjection>();
    for (const claim of interjections) {
      for (const segmentId of claim.trigger?.segment_ids ?? []) {
        map.set(segmentId, claim);
      }
    }
    return map;
  }, [interjections]);

  // Filtering client-side: a session's transcript is already fully loaded in the
  // bundle, so a round-trip per keystroke would be slower and no more correct.
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return transcript.filter((segment) => {
      if (speaker && segment.participant_id !== speaker) return false;
      if (needle && !segment.text.toLowerCase().includes(needle)) return false;
      return true;
    });
  }, [transcript, query, speaker]);

  return (
    <div>
      <div className="mb-4 flex gap-2">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search transcript…"
          className="flex-1 rounded-md border px-3 py-1.5 text-[13px] outline-none focus:border-[var(--color-accent-500)]"
          style={{ borderColor: "var(--border)", background: "var(--bg-raised)", color: "var(--text)" }}
        />
        <select
          value={speaker}
          onChange={(e) => setSpeaker(e.target.value)}
          className="rounded-md border px-2 py-1.5 text-[13px] outline-none"
          style={{ borderColor: "var(--border)", background: "var(--bg-raised)", color: "var(--text)" }}
        >
          <option value="">All speakers</option>
          {roster.map((entry) => (
            <option key={entry.participant_id} value={entry.participant_id}>
              {entry.display_name}
            </option>
          ))}
        </select>
      </div>

      {filtered.length === 0 ? (
        <Empty title={query || speaker ? "No matching lines." : "No transcript yet."} />
      ) : (
        <div className="flex flex-col">
          {filtered.map((segment) => (
            <Line
              key={segment.id}
              segment={segment}
              claim={claimBySegment.get(segment.id)}
              highlighted={segment.id === highlightedSegment}
              onJumpToClaim={onJumpToClaim}
            />
          ))}
        </div>
      )}

      {livePartials?.length ? (
        <div className="mt-1 flex flex-col">
          {livePartials.map((segment) => (
            <Line key={`partial-${segment.participant_id}`} segment={segment} partial />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function Line({
  segment,
  claim,
  highlighted,
  partial,
  onJumpToClaim,
}: {
  segment: TranscriptSegment;
  claim?: Interjection;
  highlighted?: boolean;
  partial?: boolean;
  onJumpToClaim?: (id: string) => void;
}) {
  const color = speakerColor(segment.speaker_name);
  const unidentified = !segment.person_id;

  return (
    <div
      id={`segment-${segment.id}`}
      className={`scroll-mt-4 rounded-md px-3 py-2 ${highlighted ? "flash" : ""}`}
      style={{ opacity: partial ? 0.55 : 1 }}
    >
      <div className="mb-0.5 flex items-baseline gap-2">
        <span className="text-[12px] font-medium" style={{ color }}>
          {segment.speaker_name}
        </span>
        {unidentified ? (
          <span
            className="text-[10px]"
            style={{ color: "var(--text-faint)" }}
            title="Not matched to a known person — Meet AGI had no context on this speaker"
          >
            unidentified
          </span>
        ) : null}
        <span className="ml-auto shrink-0 font-mono text-[10.5px] tabular-nums" style={{ color: "var(--text-faint)" }}>
          {partial ? "live" : timecode(segment.start_ms)}
        </span>
      </div>

      <p className="text-[13px] leading-relaxed" style={{ color: "var(--text)" }}>
        {segment.text}
        {partial ? <span className="ml-0.5 opacity-60">▍</span> : null}
      </p>

      {claim && onJumpToClaim ? (
        <button
          onClick={() => onJumpToClaim(claim.id)}
          className="mt-1.5 inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10.5px] transition-opacity hover:opacity-80"
          style={{
            background: "color-mix(in srgb, var(--color-flag-500) 14%, transparent)",
            color: "var(--color-flag-500)",
          }}
        >
          ↳ Meet AGI responded to this
        </button>
      ) : null}
    </div>
  );
}
