/**
 * Typed REST client.
 *
 * Thin on purpose: one `request` helper plus named methods, no data-fetching library,
 * no caching. Wrap it in TanStack Query / SWR / server components as you prefer — that
 * choice is yours, and this layer stays out of it.
 *
 * Every error becomes an `ApiError` carrying the backend's `{code, message, detail}`,
 * so `catch (e) { if (e instanceof ApiError) ... }` works everywhere.
 */

import type {
  CitedDocument,
  Document,
  Fixture,
  Health,
  Integration,
  IntegrationProvider,
  Interjection,
  Meeting,
  MeetingBundle,
  MeetingCreate,
  Page,
  Person,
  PersonCreate,
  PersonUpdate,
  Settings,
  SettingsUpdate,
  TranscriptSegment,
} from "./types";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

/** A non-2xx response, with the backend's error envelope unpacked. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: unknown;

  constructor(status: number, code: string, message: string, detail: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined | null>;
  signal?: AbortSignal;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, query, signal } = options;

  const url = new URL(`${API_BASE}${path}`);
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null) {
      url.searchParams.set(key, String(value));
    }
  }

  const response = await fetch(url, {
    method,
    signal,
    headers: body === undefined ? undefined : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (!response.ok) {
    // The backend always sends the error envelope, but a proxy or a network failure
    // might not — fall back rather than throwing a JSON parse error over the real one.
    let code = "http_error";
    let message = `${response.status} ${response.statusText}`;
    let detail: unknown = null;
    try {
      const parsed = await response.json();
      if (parsed?.error) {
        code = parsed.error.code ?? code;
        message = parsed.error.message ?? message;
        detail = parsed.error.detail ?? null;
      }
    } catch {
      /* keep the fallback */
    }
    throw new ApiError(response.status, code, message, detail);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** Multipart upload. Kept separate so `request` never has to branch on body type. */
async function upload<T>(path: string, form: FormData): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { method: "POST", body: form });
  if (!response.ok) {
    let code = "http_error";
    let message = `${response.status} ${response.statusText}`;
    let detail: unknown = null;
    try {
      const parsed = await response.json();
      if (parsed?.error) {
        code = parsed.error.code ?? code;
        message = parsed.error.message ?? message;
        detail = parsed.error.detail ?? null;
      }
    } catch {
      /* keep the fallback */
    }
    throw new ApiError(response.status, code, message, detail);
  }
  return (await response.json()) as T;
}

export const api = {
  health: () => request<Health>("/api/health"),

  people: {
    list: (query?: { cursor?: string; limit?: number }) =>
      request<Page<Person>>("/api/people", { query }),
    create: (body: PersonCreate) =>
      request<Person>("/api/people", { method: "POST", body }),
    get: (id: string) => request<Person>(`/api/people/${id}`),
    update: (id: string, body: PersonUpdate) =>
      request<Person>(`/api/people/${id}`, { method: "PATCH", body }),
    remove: (id: string) =>
      request<Person>(`/api/people/${id}`, { method: "DELETE" }),
    uploadVoiceSample: (id: string, file: File) => {
      const form = new FormData();
      form.append("file", file);
      return upload<Person>(`/api/people/${id}/voice-sample`, form);
    },
  },

  documents: {
    list: (query?: {
      status?: string;
      tag?: string;
      source?: string;
      cursor?: string;
      limit?: number;
    }) => request<Page<Document>>("/api/documents", { query }),
    get: (id: string) => request<Document>(`/api/documents/${id}`),
    /** Returns immediately with `status: "pending"`. Watch the global socket for progress. */
    upload: (files: File[], tags: string[] = []) => {
      const form = new FormData();
      for (const file of files) form.append("files", file);
      for (const tag of tags) form.append("tags", tag);
      return upload<Page<Document>>("/api/documents", form);
    },
    updateTags: (id: string, tags: string[]) =>
      request<Document>(`/api/documents/${id}`, { method: "PATCH", body: { tags } }),
    remove: (id: string) =>
      request<Document>(`/api/documents/${id}`, { method: "DELETE" }),
  },

  integrations: {
    list: () => request<Page<Integration>>("/api/integrations"),
    /** Simulated: resolves after ~1.2s with `status: "connected"`. */
    connect: (provider: IntegrationProvider) =>
      request<Integration>(`/api/integrations/${provider}/connect`, { method: "POST" }),
    disconnect: (provider: IntegrationProvider) =>
      request<Integration>(`/api/integrations/${provider}`, { method: "DELETE" }),
  },

  settings: {
    get: () => request<Settings>("/api/settings"),
    /** Partial. Nested objects replace wholesale — send the complete sub-object. */
    update: (body: SettingsUpdate) =>
      request<Settings>("/api/settings", { method: "PATCH", body }),
  },

  meetings: {
    list: (query?: { state?: string; cursor?: string; limit?: number }) =>
      request<Page<Meeting>>("/api/meetings", { query }),
    create: (body: MeetingCreate) =>
      request<Meeting>("/api/meetings", { method: "POST", body }),
    get: (id: string) => request<Meeting>(`/api/meetings/${id}`),
    leave: (id: string) =>
      request<Meeting>(`/api/meetings/${id}/leave`, { method: "POST" }),
    transcript: (id: string, query?: { cursor?: string; limit?: number }) =>
      request<Page<TranscriptSegment>>(`/api/meetings/${id}/transcript`, { query }),
    interjections: (id: string, query?: { cursor?: string; limit?: number }) =>
      request<Page<Interjection>>(`/api/meetings/${id}/interjections`, { query }),

    /** Delete a session and its archive. Irreversible. */
    remove: (id: string) =>
      request<{ ok: boolean }>(`/api/meetings/${id}`, { method: "DELETE" }),

    // --- Post-meeting review ---
    /** Meeting + transcript + interjections + sources in one call. Use for review pages. */
    bundle: (id: string) => request<MeetingBundle>(`/api/meetings/${id}/bundle`),
    /** Documents Meet AGI actually cited, most-used first. The audit surface. */
    sources: (id: string) =>
      request<Page<CitedDocument>>(`/api/meetings/${id}/sources`),
    /** Substring search over the finalized transcript, optionally scoped to a speaker. */
    search: (
      id: string,
      query: { q?: string; person_id?: string; cursor?: string; limit?: number },
    ) => request<Page<TranscriptSegment>>(`/api/meetings/${id}/search`, { query }),
    /** One interjection, for deep-linking a claim from a shared URL. */
    interjection: (id: string, interjectionId: string) =>
      request<Interjection>(`/api/meetings/${id}/interjections/${interjectionId}`),

    // --- Operator controls. Stage insurance; wire these to visible buttons. ---
    /** Force `listening` without the wake word. */
    wake: (id: string) =>
      request<Meeting>(`/api/meetings/${id}/wake`, { method: "POST" }),
    /** Hard override. While muted Meet AGI will not post or speak. */
    mute: (id: string, muted: boolean) =>
      request<Meeting>(`/api/meetings/${id}/mute`, { method: "POST", body: { muted } }),
    /** Type a question. With `speak`, Meet AGI also says the answer into the meeting. */
    ask: (id: string, question: string, speak = false) =>
      request<Interjection>(`/api/meetings/${id}/ask`, {
        method: "POST",
        body: { question, speak },
      }),
  },

  interjections: {
    approve: (id: string, editedChatAlert?: string) =>
      request<Interjection>(`/api/interjections/${id}/approve`, {
        method: "POST",
        body: { edited_chat_alert: editedChatAlert ?? null },
      }),
    dismiss: (id: string, reason?: string) =>
      request<Interjection>(`/api/interjections/${id}/dismiss`, {
        method: "POST",
        body: { reason: reason ?? null },
      }),
    speak: (id: string) =>
      request<Interjection>(`/api/interjections/${id}/speak`, { method: "POST" }),
  },

  /** Fixture replay. This is how you develop without a live meeting. */
  dev: {
    fixtures: () => request<Page<Fixture>>("/api/dev/fixtures"),
    startHarness: (fixtureId: string, speed = 1.0, loop = false) =>
      request<Meeting>("/api/dev/harness/start", {
        method: "POST",
        body: { fixture_id: fixtureId, speed, loop },
      }),
    stopHarness: (meetingId: string) =>
      request<Meeting>("/api/dev/harness/stop", {
        method: "POST",
        body: { meeting_id: meetingId },
      }),
    reset: () => request<{ ok: boolean }>("/api/dev/reset", { method: "POST" }),
  },
};
