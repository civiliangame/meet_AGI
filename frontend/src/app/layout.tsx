import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";
import { HealthBadge } from "@/components/HealthBadge";
import { NavLink } from "@/components/NavLink";

export const metadata: Metadata = {
  title: "Kindred",
  description: "Review what Kindred said in your meetings, and what it read before saying it.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="flex min-h-screen flex-col md:flex-row">
          <aside
            className="flex w-full shrink-0 flex-row flex-wrap items-center gap-3 border-b px-4 py-3 md:min-h-screen md:w-56 md:flex-col md:items-stretch md:border-r md:border-b-0 md:py-5"
            style={{ borderColor: "var(--border)", background: "var(--bg-raised)" }}
          >
            <Link href="/" className="flex items-baseline gap-2 px-2 md:mb-7">
              <span className="text-[15px] font-semibold tracking-tight">Kindred</span>
              <span className="text-[10px] uppercase tracking-widest" style={{ color: "var(--text-faint)" }}>
                meet_AGI
              </span>
            </Link>

            <nav className="ml-auto flex flex-row gap-0.5 md:ml-0 md:flex-col">
              <NavLink href="/">Sessions</NavLink>
              <NavLink href="/settings">Settings</NavLink>
            </nav>

            <div className="hidden md:mt-auto md:block">
              <HealthBadge />
            </div>
          </aside>

          <main className="min-w-0 flex-1">{children}</main>
        </div>
      </body>
    </html>
  );
}
