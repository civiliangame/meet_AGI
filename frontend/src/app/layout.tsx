import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";
import { HealthBadge } from "@/components/HealthBadge";
import { NavLink } from "@/components/NavLink";

export const metadata: Metadata = {
  title: "Meet AGI",
  description: "Review what Meet AGI said in your meetings, and what it read before saying it.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="flex min-h-screen">
          <aside
            className="flex w-56 shrink-0 flex-col border-r px-4 py-5"
            style={{ borderColor: "var(--border)", background: "var(--bg-raised)" }}
          >
            <Link href="/" className="mb-7 flex items-baseline gap-2 px-2">
              <span className="text-[15px] font-semibold tracking-tight">Meet AGI</span>
              <span className="text-[10px] uppercase tracking-widest" style={{ color: "var(--text-faint)" }}>
                meet_AGI
              </span>
            </Link>

            <nav className="flex flex-col gap-0.5">
              <NavLink href="/">Sessions</NavLink>
              <NavLink href="/settings">Settings</NavLink>
            </nav>

            <div className="mt-auto">
              <HealthBadge />
            </div>
          </aside>

          <main className="min-w-0 flex-1">{children}</main>
        </div>
      </body>
    </html>
  );
}
