import { initials, speakerColor } from "@/lib/format";

export function Avatar({ name, matched = true, size = 24 }: { name: string; matched?: boolean; size?: number }) {
  const color = speakerColor(name);
  return (
    <span
      title={matched ? name : `${name} — not matched to a known person`}
      className="inline-flex shrink-0 items-center justify-center rounded-full font-medium"
      style={{
        width: size,
        height: size,
        fontSize: size * 0.38,
        background: matched ? `color-mix(in srgb, ${color} 22%, transparent)` : "var(--bg-sunken)",
        color: matched ? color : "var(--text-faint)",
        border: matched ? "none" : "1px dashed var(--border)",
      }}
    >
      {initials(name)}
    </span>
  );
}

export function Stat({ value, label }: { value: React.ReactNode; label: string }) {
  return (
    <div className="flex flex-col">
      <span className="text-[13px] tabular-nums" style={{ color: "var(--text)" }}>
        {value}
      </span>
      <span className="text-[10px] uppercase tracking-wider" style={{ color: "var(--text-faint)" }}>
        {label}
      </span>
    </div>
  );
}

export function Panel({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return (
    <div
      className={`rounded-lg border ${className}`}
      style={{ borderColor: "var(--border)", background: "var(--bg-raised)" }}
    >
      {children}
    </div>
  );
}

export function Empty({ title, hint }: { title: string; hint?: React.ReactNode }) {
  return (
    <div className="px-6 py-16 text-center">
      <p className="text-[13px]" style={{ color: "var(--text-muted)" }}>
        {title}
      </p>
      {hint ? (
        <div className="mt-2 text-[12px]" style={{ color: "var(--text-faint)" }}>
          {hint}
        </div>
      ) : null}
    </div>
  );
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <div className="px-6 py-16 text-center text-[13px]" style={{ color: "var(--text-faint)" }}>
      {label}…
    </div>
  );
}
