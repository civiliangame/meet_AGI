"use client";

import { useCallback, useEffect, useState } from "react";
import { Avatar, Empty, Panel, Spinner } from "@/components/primitives";
import { api } from "@/lib/api/client";
import type { Document, Integration, Person, Settings } from "@/lib/api/types";

const AUTONOMY: { value: string; label: string; blurb: string }[] = [
  { value: "silent", label: "Silent", blurb: "Kindred never speaks or posts. Findings appear here only." },
  { value: "propose", label: "Propose", blurb: "Kindred asks before posting to the meeting." },
  { value: "auto_post", label: "Auto-post", blurb: "Kindred posts to the meeting chat on its own." },
];

export default function SettingsPage() {
  const [people, setPeople] = useState<Person[] | null>(null);
  const [documents, setDocuments] = useState<Document[] | null>(null);
  const [integrations, setIntegrations] = useState<Integration[] | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);

  const load = useCallback(async () => {
    const [p, d, i, s] = await Promise.all([
      api.people.list(),
      api.documents.list(),
      api.integrations.list(),
      api.settings.get(),
    ]);
    setPeople(p.items);
    setDocuments(d.items);
    setIntegrations(i.items);
    setSettings(s);
  }, []);

  useEffect(() => {
    void load().catch(() => undefined);
  }, [load]);

  if (!settings) return <Spinner label="Loading settings" />;

  return (
    <div className="mx-auto max-w-3xl px-8 py-9">
      <h1 className="text-[22px] font-semibold tracking-tight">Settings</h1>
      <p className="mt-1 mb-7 text-[13px]" style={{ color: "var(--text-muted)" }}>
        What Kindred knows, and how far it is allowed to go.
      </p>

      <Section
        title="Agent"
        blurb="Autonomy is a trust decision, not a preference."
      >
        <div className="p-4">
          <div className="mb-4 flex flex-col gap-1.5">
            {AUTONOMY.map((option) => (
              <label
                key={option.value}
                className="flex cursor-pointer items-start gap-2.5 rounded-md border p-2.5 transition-colors"
                style={{
                  borderColor:
                    settings.autonomy === option.value ? "var(--color-accent-500)" : "var(--border)",
                  background:
                    settings.autonomy === option.value
                      ? "color-mix(in srgb, var(--color-accent-500) 7%, transparent)"
                      : "transparent",
                }}
              >
                <input
                  type="radio"
                  name="autonomy"
                  checked={settings.autonomy === option.value}
                  onChange={async () => {
                    const next = await api.settings.update({ autonomy: option.value as Settings["autonomy"] });
                    setSettings(next);
                  }}
                  className="mt-0.5"
                />
                <div>
                  <div className="text-[13px] font-medium">{option.label}</div>
                  <div className="text-[12px]" style={{ color: "var(--text-muted)" }}>
                    {option.blurb}
                  </div>
                </div>
              </label>
            ))}
          </div>

          <div className="grid grid-cols-2 gap-4 border-t pt-4" style={{ borderColor: "var(--border)" }}>
            <Field label="Wake phrase" value={settings.wake_word} />
            <Field label="Also answers to" value={(settings.wake_aliases ?? []).join(", ") || "—"} />
            <Field label="Cooldown" value={`${settings.interjection.cooldown_seconds}s between interjections`} />
            <Field label="Cap" value={`${settings.interjection.max_per_meeting} per meeting`} />
            <Field label="Voice" value={`${settings.voice.provider} · ${settings.voice.voice_id ?? "default"}`} />
            <Field label="Triage" value={settings.triage.provider} />
          </div>
        </div>
      </Section>

      <Section title="People" blurb="How Kindred knows who is talking, and why they would say it.">
        {people === null ? (
          <Spinner />
        ) : people.length === 0 ? (
          <Empty title="No people yet." />
        ) : (
          <div className="divide-y" style={{ borderColor: "var(--border)" }}>
            {people.map((person) => (
              <div key={person.id} className="flex items-center gap-3 px-4 py-3">
                <Avatar name={person.display_name} size={28} />
                <div className="min-w-0 flex-1">
                  <div className="text-[13px] font-medium">{person.display_name}</div>
                  <div className="truncate text-[12px]" style={{ color: "var(--text-muted)" }}>
                    {[person.role, person.org].filter(Boolean).join(" · ") || "—"}
                  </div>
                </div>
                {person.aliases?.length ? (
                  <div className="shrink-0 text-[11px]" style={{ color: "var(--text-faint)" }}>
                    aka {person.aliases.join(", ")}
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        )}
      </Section>

      <Section title="Documents" blurb="The corpus Kindred checks claims against.">
        {documents === null ? (
          <Spinner />
        ) : documents.length === 0 ? (
          <Empty title="No documents yet." />
        ) : (
          <div className="divide-y" style={{ borderColor: "var(--border)" }}>
            {documents.map((document) => (
              <div key={document.id} className="flex items-center gap-3 px-4 py-3">
                <div className="min-w-0 flex-1">
                  <div className="truncate font-mono text-[12.5px]">{document.filename}</div>
                  <div className="text-[11px]" style={{ color: "var(--text-faint)" }}>
                    {document.source} · {document.chunk_count} chunks
                  </div>
                </div>
                <StatusChip status={document.status} />
              </div>
            ))}
          </div>
        )}
      </Section>

      <Section title="Integrations" blurb="Every connection below is simulated for the hackathon.">
        {integrations === null ? (
          <Spinner />
        ) : (
          <div className="grid grid-cols-2 gap-2 p-4">
            {integrations.map((integration) => (
              <div
                key={integration.provider}
                className="flex items-center gap-2 rounded-md border p-2.5"
                style={{ borderColor: "var(--border)" }}
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className="text-[13px]">{integration.display_name}</span>
                    {integration.is_stub ? (
                      <span
                        className="rounded px-1 py-0.5 text-[9px] uppercase tracking-wide"
                        style={{ background: "var(--bg-sunken)", color: "var(--text-faint)" }}
                      >
                        demo
                      </span>
                    ) : null}
                  </div>
                  <div className="truncate text-[11px]" style={{ color: "var(--text-faint)" }}>
                    {integration.account_label ?? "Not connected"}
                  </div>
                </div>
                <button
                  onClick={async () => {
                    const next =
                      integration.status === "connected"
                        ? await api.integrations.disconnect(integration.provider)
                        : await api.integrations.connect(integration.provider);
                    setIntegrations((prev) =>
                      (prev ?? []).map((x) => (x.provider === next.provider ? next : x)),
                    );
                  }}
                  className="shrink-0 rounded border px-2 py-1 text-[11px]"
                  style={{
                    borderColor:
                      integration.status === "connected" ? "var(--color-speak-500)" : "var(--border)",
                    color:
                      integration.status === "connected" ? "var(--color-speak-500)" : "var(--text-muted)",
                  }}
                >
                  {integration.status === "connected" ? "Connected" : "Connect"}
                </button>
              </div>
            ))}
          </div>
        )}
      </Section>
    </div>
  );
}

function Section({
  title,
  blurb,
  children,
}: {
  title: string;
  blurb: string;
  children: React.ReactNode;
}) {
  return (
    <section className="mb-6">
      <div className="mb-2">
        <h2 className="text-[15px] font-medium">{title}</h2>
        <p className="text-[12px]" style={{ color: "var(--text-faint)" }}>
          {blurb}
        </p>
      </div>
      <Panel className="overflow-hidden">{children}</Panel>
    </section>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider" style={{ color: "var(--text-faint)" }}>
        {label}
      </div>
      <div className="mt-0.5 text-[13px]">{value}</div>
    </div>
  );
}

function StatusChip({ status }: { status: string }) {
  const color =
    status === "ready"
      ? "var(--color-speak-500)"
      : status === "failed"
        ? "var(--color-flag-500)"
        : "var(--text-faint)";
  return (
    <span
      className="shrink-0 rounded px-1.5 py-0.5 text-[10px]"
      style={{ background: `color-mix(in srgb, ${color} 15%, transparent)`, color }}
    >
      {status}
    </span>
  );
}
