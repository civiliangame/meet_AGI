"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { ClaimCard } from "@/components/session/ClaimCard";
import { SourcesView } from "@/components/session/SourcesView";
import { TranscriptView } from "@/components/session/TranscriptView";
import { Avatar, Empty, Spinner } from "@/components/primitives";
import { api } from "@/lib/api/client";
import { absoluteTime, duration, relativeTime } from "@/lib/format";
import { connectMeeting, type LiveConnection } from "@/lib/api/ws";
import type { AgentState, MeetingBundle, TranscriptSegment } from "@/lib/api/types";

type Tab = "claims" | "transcript" | "sources";

const AGENT_STATE_STYLE: Record<string, { label: string; color: string; pulse: boolean }> = {
  idle: { label: "Listening", color: "var(--text-faint)", pulse: false },
  listening: { label: "Woken", color: "var(--color-accent-400)", pulse: true },
  thinking: { label: "Thinking", color: "var(--color-flag-400)", pulse: true },
  speaking: { label: "Speaking", color: "var(--color-speak-400)", pulse: true },
  muted: { label: "Muted", color: "var(--text-faint)", pulse: false },
};

export default function SessionPage() {
  const { id } = useParams<{ id: string }>();
  const [bundle, setBundle] = useState<MeetingBundle | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("claims");

  const [highlightedClaim, setHighlightedClaim] = useState<string | null>(null);
  const [highlightedSegment, setHighlightedSegment] = useState<string | null>(null);
  const [highlightedDocument, setHighlightedDocument] = useState<string | null>(null);

  const [agentState, setAgentState] = useState<AgentState | null>(null);
  const [partials, setPartials] = useState<Record<string, TranscriptSegment>>({});
  const connection = useRef<LiveConnection | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await api.meetings.bundle(id);
      setBundle(next);
      setAgentState(next.meeting.agent_state);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load this session");
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  const isLive = bundle?.meeting.state === "in_call";

  // Attach the socket only while the meeting is running. A finished session is a
  // document, not a stream.
  useEffect(() => {
    if (!isLive) return;
    const conn = connectMeeting(id, {
      onSnapshot: () => void load(),
      onEvent: (event) => {
        switch (event.type) {
          case "agent.state_changed":
            setAgentState(event.data.agent_state);
            break;
          case "transcript.partial":
            setPartials((prev) => ({ ...prev, [event.data.participant_id]: event.data }));
            break;
          case "transcript.final":
            setPartials((prev) => {
              const next = { ...prev };
              delete next[event.data.participant_id];
              return next;
            });
            void load();
            break;
          case "interjection.proposed":
          case "interjection.updated":
          case "meeting.state_changed":
          case "participant.joined":
            void load();
            break;
          default:
            break;
        }
      },
    });
    connection.current = conn;
    return () => conn.close();
  }, [id, isLive, load]);

  /** Cross-view navigation: switch tab, scroll to the anchor, flash it. */
  const jump = useCallback((nextTab: Tab, anchorId: string, set: (v: string) => void) => {
    setTab(nextTab);
    set(anchorId);
    requestAnimationFrame(() => {
      document.getElementById(anchorId)?.scrollIntoView({ behavior: "smooth", block: "center" });
    });
    setTimeout(() => set(""), 1600);
  }, []);

  if (error) {
    return (
      <div className="mx-auto max-w-3xl px-8 py-9">
        <Link href="/" className="text-[12px]" style={{ color: "var(--color-accent-500)" }}>
          ← Sessions
        </Link>
        <Empty title={error} />
      </div>
    );
  }

  if (!bundle) {
    return <Spinner label="Loading session" />;
  }

  const { meeting, transcript, interjections, sources } = bundle;
  const state = AGENT_STATE_STYLE[agentState ?? "idle"] ?? AGENT_STATE_STYLE.idle;

  const tabs: [Tab, string, number][] = [
    ["claims", "Claims", interjections.length],
    ["transcript", "Transcript", transcript.length],
    ["sources", "Sources", sources.length],
  ];

  return (
    <div className="mx-auto max-w-3xl px-8 py-9">
      <Link href="/" className="text-[12px] transition-opacity hover:opacity-70" style={{ color: "var(--color-accent-500)" }}>
        ← Sessions
      </Link>

      <header className="mt-4 mb-6">
        <div className="flex items-start gap-3">
          <h1 className="flex-1 text-[22px] font-semibold leading-tight tracking-tight">{meeting.title}</h1>
          {isLive ? (
            <span
              className={`shrink-0 rounded-full px-2.5 py-1 text-[11px] font-medium ${state.pulse ? "pulsing" : ""}`}
              style={{
                background: `color-mix(in srgb, ${state.color} 16%, transparent)`,
                color: state.color,
              }}
            >
              {state.label}
            </span>
          ) : null}
        </div>

        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px]" style={{ color: "var(--text-faint)" }}>
          <span title={absoluteTime(meeting.started_at)}>{relativeTime(meeting.started_at)}</span>
          <span>·</span>
          <span className="tabular-nums">{duration(meeting.stats.duration_seconds)}</span>
          {meeting.meeting_url ? (
            <>
              <span>·</span>
              <a
                href={meeting.meeting_url}
                target="_blank"
                rel="noreferrer"
                className="truncate hover:underline"
                style={{ color: "var(--color-accent-500)" }}
              >
                {meeting.meeting_url.replace(/^https?:\/\//, "")}
              </a>
            </>
          ) : null}
          {meeting.source === "harness" ? (
            <>
              <span>·</span>
              <span>fixture</span>
            </>
          ) : null}
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-3">
          {meeting.roster.map((entry) => (
            <div key={entry.participant_id} className="flex items-center gap-1.5">
              <Avatar name={entry.display_name} matched={entry.matched} size={22} />
              <div className="leading-tight">
                <div className="text-[12px]">{entry.display_name}</div>
                {entry.matched ? null : (
                  <div className="text-[10px]" style={{ color: "var(--text-faint)" }}>
                    unidentified
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>

        {isLive ? <LiveControls meetingId={id} muted={agentState === "muted"} onChange={load} /> : null}
      </header>

      <nav className="mb-5 flex gap-1 border-b" style={{ borderColor: "var(--border)" }}>
        {tabs.map(([key, label, count]) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className="-mb-px border-b-2 px-3 py-2 text-[13px] transition-colors"
            style={{
              borderColor: tab === key ? "var(--color-accent-500)" : "transparent",
              color: tab === key ? "var(--text)" : "var(--text-muted)",
              fontWeight: tab === key ? 550 : 400,
            }}
          >
            {label}
            <span className="ml-1.5 text-[11px] tabular-nums" style={{ color: "var(--text-faint)" }}>
              {count}
            </span>
          </button>
        ))}
      </nav>

      {tab === "claims" ? (
        interjections.length === 0 ? (
          <Empty title="Kindred did not speak up in this session." />
        ) : (
          <div className="flex flex-col gap-3">
            {interjections.map((claim) => (
              <ClaimCard
                key={claim.id}
                claim={claim}
                highlighted={claim.id === highlightedClaim}
                onJumpToTranscript={(segmentId) =>
                  jump("transcript", `segment-${segmentId}`, () => setHighlightedSegment(segmentId))
                }
                onJumpToSource={(documentId) =>
                  jump("sources", `source-${documentId}`, () => setHighlightedDocument(documentId))
                }
                onApprove={async (claimId) => {
                  await api.interjections.approve(claimId);
                  void load();
                }}
                onDismiss={async (claimId) => {
                  await api.interjections.dismiss(claimId);
                  void load();
                }}
              />
            ))}
          </div>
        )
      ) : null}

      {tab === "transcript" ? (
        <TranscriptView
          transcript={transcript}
          interjections={interjections}
          roster={meeting.roster}
          highlightedSegment={highlightedSegment}
          livePartials={isLive ? Object.values(partials) : undefined}
          onJumpToClaim={(claimId) =>
            jump("claims", `claim-${claimId}`, () => setHighlightedClaim(claimId))
          }
        />
      ) : null}

      {tab === "sources" ? (
        <SourcesView
          sources={sources}
          interjections={interjections}
          highlightedDocument={highlightedDocument}
          onJumpToClaim={(claimId) =>
            jump("claims", `claim-${claimId}`, () => setHighlightedClaim(claimId))
          }
        />
      ) : null}
    </div>
  );
}

/** Stage insurance. Visible controls, not a debug panel — if wake-word detection
 *  misfires during a demo, this is how Kindred gets driven from the dashboard. */
function LiveControls({
  meetingId,
  muted,
  onChange,
}: {
  meetingId: string;
  muted: boolean;
  onChange: () => void;
}) {
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);

  async function ask() {
    if (!question.trim()) return;
    setBusy(true);
    try {
      await api.meetings.ask(meetingId, question, true);
      setQuestion("");
      onChange();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-4 flex flex-wrap items-center gap-2 rounded-lg border p-2.5" style={{ borderColor: "var(--border)" }}>
      <button
        onClick={async () => {
          await api.meetings.mute(meetingId, !muted);
          onChange();
        }}
        className="rounded-md border px-2.5 py-1.5 text-[12px]"
        style={{
          borderColor: muted ? "var(--color-flag-500)" : "var(--border)",
          color: muted ? "var(--color-flag-500)" : "var(--text-muted)",
        }}
      >
        {muted ? "Unmute" : "Mute"}
      </button>
      <button
        onClick={async () => {
          await api.meetings.wake(meetingId);
          onChange();
        }}
        disabled={muted}
        className="rounded-md border px-2.5 py-1.5 text-[12px] disabled:opacity-40"
        style={{ borderColor: "var(--border)", color: "var(--text-muted)" }}
      >
        Wake
      </button>
      <input
        value={question}
        onChange={(e) => setQuestion(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && ask()}
        placeholder="Ask Kindred out loud…"
        className="min-w-40 flex-1 rounded-md border px-2.5 py-1.5 text-[12px] outline-none focus:border-[var(--color-accent-500)]"
        style={{ borderColor: "var(--border)", background: "var(--bg)", color: "var(--text)" }}
      />
      <button
        onClick={ask}
        disabled={busy || !question.trim()}
        className="rounded-md px-3 py-1.5 text-[12px] font-medium disabled:opacity-40"
        style={{ background: "var(--color-accent-500)", color: "#fff" }}
      >
        Ask
      </button>
    </div>
  );
}
