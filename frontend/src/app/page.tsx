"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { Avatar, Empty, Panel, Spinner } from "@/components/primitives";
import { api } from "@/lib/api/client";
import { absoluteTime, duration, relativeTime } from "@/lib/format";
import { deriveSessionReview, type FollowUpCounts } from "@/lib/review";
import type { Meeting } from "@/lib/api/types";

export default function SessionsPage() {
  const [sessions, setSessions] = useState<Meeting[] | null>(null);
  const [followUpsBySession, setFollowUpsBySession] = useState<Record<string, FollowUpCounts | null>>({});
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  const load = useCallback(async () => {
    try {
      const page = await api.meetings.list();
      setSessions(page.items);
      setFollowUpsBySession({});
      setError(null);

      const results = await Promise.allSettled(
        page.items.map(async (meeting) => deriveSessionReview(await api.meetings.bundle(meeting.id)).counts),
      );
      const next: Record<string, FollowUpCounts | null> = {};
      page.items.forEach((meeting, index) => {
        const result = results[index];
        next[meeting.id] = result?.status === "fulfilled" ? result.value : null;
      });
      setFollowUpsBySession(next);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not reach the backend");
      setSessions([]);
      setFollowUpsBySession({});
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function runFixture() {
    setStarting(true);
    try {
      await api.dev.startHarness("q3_revenue_review", 50);
      // The fixture is ~160s of content at 50x, so a beat over 3s covers it.
      setTimeout(() => {
        void load();
        setStarting(false);
      }, 4000);
    } catch {
      setStarting(false);
    }
  }

  return (
    <div className="mx-auto max-w-4xl px-4 py-6 sm:px-8 sm:py-9">
      <header className="mb-7 flex flex-col items-start gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-[22px] font-semibold tracking-tight">Sessions</h1>
          <p className="mt-1 text-[13px]" style={{ color: "var(--text-muted)" }}>
            What Meet AGI said in each meeting, and what it read before saying it.
          </p>
        </div>
        <button
          onClick={runFixture}
          disabled={starting}
          className="rounded-md border px-3 py-1.5 text-[12px] transition-colors disabled:opacity-50"
          style={{ borderColor: "var(--border)", color: "var(--text-muted)" }}
        >
          {starting ? "Running…" : "Run fixture meeting"}
        </button>
      </header>

      {error ? (
        <Panel>
          <Empty
            title={error}
            hint={
              <>
                Start the backend:{" "}
                <code className="font-mono">uvicorn app.main:app --port 8000</code>
              </>
            }
          />
        </Panel>
      ) : sessions === null ? (
        <Panel>
          <Spinner label="Loading sessions" />
        </Panel>
      ) : sessions.length === 0 ? (
        <Panel>
          <Empty
            title="No sessions yet."
            hint="Meet AGI will appear here after its first meeting. Or run the fixture above."
          />
        </Panel>
      ) : (
        <Panel className="divide-y overflow-hidden" >
          {sessions.map((session) => (
            <SessionRow
              key={session.id}
              session={session}
              followUps={followUpsBySession[session.id]}
            />
          ))}
        </Panel>
      )}
    </div>
  );
}

function SessionRow({
  session,
  followUps,
}: {
  session: Meeting;
  followUps: FollowUpCounts | null | undefined;
}) {
  const stats = session.stats;
  const roster = session.roster ?? [];
  const shown = roster.slice(0, 4);
  const overflow = roster.length - shown.length;

  return (
    <Link
      href={`/sessions/${session.id}`}
      className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-3 px-4 py-4 transition-colors hover:bg-[var(--bg-sunken)] sm:flex sm:gap-4 sm:px-5"
      style={{ borderColor: "var(--border)" }}
    >
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-[14px] font-medium">{session.title}</span>
          {session.state === "failed" ? (
            <span
              className="shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium"
              style={{
                background: "color-mix(in srgb, var(--color-flag-500) 18%, transparent)",
                color: "var(--color-flag-500)",
              }}
            >
              failed
            </span>
          ) : null}
          {session.source === "harness" ? (
            <span
              className="shrink-0 rounded px-1.5 py-0.5 text-[10px]"
              style={{ background: "var(--bg-sunken)", color: "var(--text-faint)" }}
            >
              fixture
            </span>
          ) : null}
          {session.state === "in_call" ? (
            <span
              className="pulsing shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-medium"
              style={{
                background: "color-mix(in srgb, var(--color-speak-500) 18%, transparent)",
                color: "var(--color-speak-500)",
              }}
            >
              live
            </span>
          ) : null}
        </div>

        <div
          className="mt-1 flex items-center gap-2 text-[12px]"
          style={{ color: "var(--text-faint)" }}
        >
          <span title={absoluteTime(session.started_at)}>{relativeTime(session.started_at)}</span>
          <span>·</span>
          <span className="tabular-nums">{duration(stats.duration_seconds)}</span>
        </div>
      </div>

      <div className="flex shrink-0 justify-self-end -space-x-1.5">
        {shown.map((entry) => (
          <Avatar key={entry.participant_id} name={entry.display_name} matched={entry.matched} />
        ))}
        {overflow > 0 ? (
          <span
            className="inline-flex h-6 w-6 items-center justify-center rounded-full text-[10px]"
            style={{ background: "var(--bg-sunken)", color: "var(--text-faint)" }}
          >
            +{overflow}
          </span>
        ) : null}
      </div>

      <div className="col-span-2 flex shrink-0 items-start justify-start gap-5 text-left sm:w-[19rem] sm:justify-end sm:text-right">
        <div
          data-testid={`follow-up-summary-${session.id}`}
          data-follow-up-total={followUps?.total}
          data-follow-up-outstanding={followUps?.outstanding}
          data-follow-up-resolved={followUps?.resolved}
        >
          <div className="text-[14px] tabular-nums">{followUps === undefined || followUps === null ? "—" : followUps.total}</div>
          <div className="text-[10px] uppercase tracking-wider" style={{ color: "var(--text-faint)" }}>
            follow-ups
          </div>
          <div className="mt-0.5 whitespace-nowrap text-[9.5px]" style={{ color: "var(--text-faint)" }}>
            {followUps === undefined
              ? "reviewing…"
              : followUps === null
                ? "unavailable"
                : `${followUps.outstanding} outstanding · ${followUps.resolved} resolved`}
          </div>
        </div>
        <div>
          <div className="text-[14px] tabular-nums">{stats.interjection_count}</div>
          <div className="text-[10px] uppercase tracking-wider" style={{ color: "var(--text-faint)" }}>
            claims
          </div>
        </div>
        <div>
          <div className="text-[14px] tabular-nums">{stats.source_document_count}</div>
          <div className="text-[10px] uppercase tracking-wider" style={{ color: "var(--text-faint)" }}>
            sources
          </div>
        </div>
      </div>
    </Link>
  );
}
