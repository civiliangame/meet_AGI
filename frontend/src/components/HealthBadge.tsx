"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api/client";
import type { Health } from "@/lib/api/types";

/**
 * What is actually wired up.
 *
 * Worth a permanent corner of the UI: "is Kindred reasoning for real or replaying
 * fixtures" is the first question to ask when something looks wrong, and reading it
 * off the screen beats guessing.
 */
export function HealthBadge() {
  const [health, setHealth] = useState<Health | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    api.health().then(setHealth).catch(() => setFailed(true));
  }, []);

  if (failed) {
    return (
      <div className="rounded-md border px-2.5 py-2 text-[11px]" style={{ borderColor: "var(--border)" }}>
        <div className="font-medium" style={{ color: "var(--color-flag-500)" }}>
          Backend unreachable
        </div>
        <div className="mt-1 leading-snug" style={{ color: "var(--text-faint)" }}>
          Start it on :8000
        </div>
      </div>
    );
  }

  if (!health) return null;

  return (
    <div className="rounded-md border px-2.5 py-2" style={{ borderColor: "var(--border)" }}>
      <div className="mb-1.5 text-[10px] uppercase tracking-wider" style={{ color: "var(--text-faint)" }}>
        Backend
      </div>
      {health.providers.map((provider) => (
        <div key={provider.name} className="flex items-center gap-1.5 py-0.5" title={provider.detail}>
          <span
            className="h-1.5 w-1.5 shrink-0 rounded-full"
            style={{
              background: provider.configured ? "var(--color-speak-500)" : "var(--color-ink-400)",
            }}
          />
          <span className="truncate text-[11px]" style={{ color: "var(--text-muted)" }}>
            {provider.name}
          </span>
        </div>
      ))}
    </div>
  );
}
