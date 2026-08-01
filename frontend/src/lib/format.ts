/** Display formatting. Everything here is presentation-only. */

/** `158` -> `2m 38s`. Durations are read at a glance, so no leading zeros. */
export function duration(seconds: number): string {
  if (!seconds) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

/** Offset within a meeting: `154200` -> `2:34`. */
export function timecode(ms: number): string {
  const total = Math.floor(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

const RELATIVE = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 31536000],
  ["month", 2592000],
  ["day", 86400],
  ["hour", 3600],
  ["minute", 60],
];

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const deltaSeconds = (Date.parse(iso) - Date.now()) / 1000;
  for (const [unit, size] of UNITS) {
    if (Math.abs(deltaSeconds) >= size) {
      return RELATIVE.format(Math.round(deltaSeconds / size), unit);
    }
  }
  return "just now";
}

/** Absolute time for the hover title, in the viewer's timezone. */
export function absoluteTime(iso: string | null | undefined): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

export function initials(name: string): string {
  return name
    .replace(/\(.*?\)/g, "")
    .trim()
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? "")
    .join("");
}

/** Stable per-speaker colour so a name reads the same in roster and transcript. */
const SPEAKER_COLORS = [
  "#7aa2f7",
  "#6cc4a1",
  "#e0a458",
  "#c99ce0",
  "#5fb3c4",
  "#e08585",
];

export function speakerColor(key: string): string {
  let hash = 0;
  for (let i = 0; i < key.length; i++) hash = (hash * 31 + key.charCodeAt(i)) | 0;
  return SPEAKER_COLORS[Math.abs(hash) % SPEAKER_COLORS.length];
}
