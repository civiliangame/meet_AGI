/**
 * Friendly names for the generated schema types.
 *
 * `generated.ts` is machine output and awkward to read
 * (`components["schemas"]["Interjection"]`). This file is the only hand-written type
 * module in the API layer, and it does nothing but alias — it never redefines a shape.
 * If a field is wrong, fix the Pydantic model in `backend/app/schemas/` and rerun
 * `./scripts/gen-types.sh`.
 *
 * Import from here, not from `generated.ts`.
 */

import type { components, operations } from "./generated";

type S = components["schemas"];

// --- Core resources --------------------------------------------------------------

export type Person = S["Person"];
export type PersonCreate = S["PersonCreate"];
export type PersonUpdate = S["PersonUpdate"];

export type Document = S["Document"];
export type DocumentStatus = S["DocumentStatus"];
export type DocumentSource = S["DocumentSource"];
export type Citation = S["Citation"];

export type Integration = S["Integration"];
export type IntegrationProvider = S["IntegrationProvider"];
export type IntegrationStatus = S["IntegrationStatus"];

export type Settings = S["Settings"];
export type SettingsUpdate = S["SettingsUpdate"];
export type Autonomy = S["Autonomy"];

export type Meeting = S["Meeting"];
export type MeetingCreate = S["MeetingCreate"];
export type MeetingState = S["MeetingState"];
export type AgentState = S["AgentState"];
export type RosterEntry = S["RosterEntry"];
export type MeetingStats = S["MeetingStats"];

export type TranscriptSegment = S["TranscriptSegment"];

export type Interjection = S["Interjection"];
export type InterjectionKind = S["InterjectionKind"];
export type InterjectionStatus = S["InterjectionStatus"];
export type InterjectionTrigger = S["InterjectionTrigger"];

export type Fixture = S["Fixture"];
export type Health = S["Health"];

// --- Post-meeting review ---------------------------------------------------------

/** Everything a review page needs, from one `GET /api/meetings/{id}/bundle`. */
export type MeetingBundle = S["MeetingBundle"];

/** A document Kindred actually cited during a session, with the passages it used. */
export type CitedDocument = S["CitedDocument"];
export type SourceQuote = S["SourceQuote"];

// --- Live events -----------------------------------------------------------------

/**
 * Every frame the server can push over WebSocket, as a discriminated union on `type`.
 *
 * Extracted from the schema-anchor endpoint's response rather than hand-listed, so a
 * new event type added in Python appears here with no frontend edit. See
 * `backend/app/api/schema_only.py` for why that endpoint exists.
 *
 * Always include a `default` branch when switching on `type`. New event types are
 * added without announcement, and an older frontend must not crash on one it has
 * never seen.
 */
export type LiveEvent =
  operations["live_event_schema_api__schema_live_event_get"]["responses"][200]["content"]["application/json"];

/** Narrow `LiveEvent` to one member by its `type`. */
export type LiveEventOf<T extends LiveEvent["type"]> = Extract<LiveEvent, { type: T }>;

export type SnapshotData = S["SnapshotData"];

// --- Envelopes -------------------------------------------------------------------

export type ApiErrorBody = S["ErrorResponse"];

/** Cursor-paginated list. Cursors are opaque; pass `next_cursor` back as `cursor`. */
export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}
