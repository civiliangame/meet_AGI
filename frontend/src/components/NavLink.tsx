"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

export function NavLink({ href, children }: { href: string; children: React.ReactNode }) {
  const pathname = usePathname();
  // "/" would otherwise match every route, so the root is an exact match and
  // everything else matches its subtree (a session detail keeps Sessions lit).
  const active = href === "/" ? pathname === "/" || pathname.startsWith("/sessions") : pathname.startsWith(href);

  return (
    <Link
      href={href}
      className="rounded-md px-2 py-1.5 text-[13px] transition-colors"
      style={{
        background: active ? "var(--bg-sunken)" : "transparent",
        color: active ? "var(--text)" : "var(--text-muted)",
        fontWeight: active ? 550 : 400,
      }}
    >
      {children}
    </Link>
  );
}
