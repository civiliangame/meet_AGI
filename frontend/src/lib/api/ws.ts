/**
 * Typed WebSocket client for the live event stream.
 *
 * Handles the three things that are easy to get wrong and annoying to debug:
 *
 * 1. **Reconnect with `since_seq`.** On reconnect it asks for everything after the last
 *    frame it saw. If the server can still serve that, you get the missed frames. If not,
 *    you get a fresh `snapshot` and must reset local state — `onSnapshot` fires again,
 *    which is your cue.
 * 2. **Sequence gaps.** `seq` is monotonic per connection. A gap means frames were
 *    dropped, so the client force-reconnects rather than rendering an inconsistent view.
 * 3. **Server pings.** The server sends `{"type":"ping"}` on idle to keep the socket
 *    warm. Those are swallowed here and never reach your handler.
 *
 * Usage:
 *
 * ```ts
 * const conn = connectMeeting(meetingId, {
 *   onSnapshot: (data) => resetStore(data),
 *   onEvent: (event) => {
 *     switch (event.type) {
 *       case "transcript.partial": upsertLiveLine(event.data); break;
 *       case "transcript.final":   commitSegment(event.data);  break;
 *       case "interjection.proposed": addCard(event.data);     break;
 *       case "agent.state_changed":   setAgentState(event.data.agent_state); break;
 *       default: break; // new event types arrive without announcement
 *     }
 *   },
 * });
 * // later
 * conn.close();
 * ```
 */

import { API_BASE } from "./client";
import type { LiveEvent, SnapshotData } from "./types";

export type ConnectionStatus = "connecting" | "open" | "reconnecting" | "closed";

export interface LiveOptions {
  /** Full state. Fires on first connect and again after any unrecoverable gap. */
  onSnapshot?: (data: SnapshotData) => void;
  /** Every frame except `snapshot` and transport pings. */
  onEvent?: (event: LiveEvent) => void;
  onStatus?: (status: ConnectionStatus) => void;
  onError?: (error: Error) => void;
  /** Backoff ceiling. Defaults to 10s. */
  maxBackoffMs?: number;
}

export interface LiveConnection {
  close: () => void;
  status: () => ConnectionStatus;
  /** Last sequence number seen. Useful in a debug panel. */
  lastSeq: () => number | null;
}

function httpToWs(base: string): string {
  return base.replace(/^http/, "ws");
}

function connect(path: string, options: LiveOptions): LiveConnection {
  const { onSnapshot, onEvent, onStatus, onError, maxBackoffMs = 10_000 } = options;

  let socket: WebSocket | null = null;
  let lastSeq: number | null = null;
  let attempt = 0;
  let status: ConnectionStatus = "connecting";
  let closedByCaller = false;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;

  const setStatus = (next: ConnectionStatus) => {
    if (status !== next) {
      status = next;
      onStatus?.(next);
    }
  };

  const scheduleReconnect = () => {
    if (closedByCaller) return;
    setStatus("reconnecting");
    // Exponential backoff with jitter, so a server restart does not get a
    // synchronized stampede from every open tab.
    const base = Math.min(maxBackoffMs, 250 * 2 ** attempt);
    const delay = base / 2 + Math.random() * (base / 2);
    attempt += 1;
    retryTimer = setTimeout(open, delay);
  };

  function open() {
    if (closedByCaller) return;

    const url = new URL(`${httpToWs(API_BASE)}${path}`);
    if (lastSeq !== null) url.searchParams.set("since_seq", String(lastSeq));

    const ws = new WebSocket(url.toString());
    socket = ws;

    ws.onopen = () => {
      attempt = 0;
      setStatus("open");
    };

    ws.onmessage = (message) => {
      let frame: unknown;
      try {
        frame = JSON.parse(message.data as string);
      } catch {
        onError?.(new Error("received a non-JSON frame"));
        return;
      }

      // Transport keepalive. Not part of the event contract.
      if ((frame as { type?: string }).type === "ping") return;

      const event = frame as LiveEvent;

      if (typeof event.seq === "number") {
        if (lastSeq !== null && event.seq > lastSeq + 1) {
          // Frames were dropped. Reconnecting is the documented recovery: either the
          // server replays the gap or it sends a fresh snapshot.
          onError?.(
            new Error(`sequence gap: expected ${lastSeq + 1}, got ${event.seq}`),
          );
          lastSeq = null;
          ws.close();
          return;
        }
        lastSeq = event.seq;
      }

      if (event.type === "snapshot") {
        onSnapshot?.(event.data);
        return;
      }
      onEvent?.(event);
    };

    ws.onerror = () => {
      onError?.(new Error("websocket error"));
    };

    ws.onclose = () => {
      if (closedByCaller) {
        setStatus("closed");
        return;
      }
      scheduleReconnect();
    };
  }

  open();

  return {
    close: () => {
      closedByCaller = true;
      if (retryTimer) clearTimeout(retryTimer);
      socket?.close();
      setStatus("closed");
    },
    status: () => status,
    lastSeq: () => lastSeq,
  };
}

/** Per-meeting stream: transcript, participants, agent state, interjections. */
export function connectMeeting(
  meetingId: string,
  options: LiveOptions = {},
): LiveConnection {
  return connect(`/api/meetings/${meetingId}/live`, options);
}

/** Global stream: document ingestion status and meeting lifecycle. */
export function connectGlobal(options: LiveOptions = {}): LiveConnection {
  return connect("/api/live", options);
}
