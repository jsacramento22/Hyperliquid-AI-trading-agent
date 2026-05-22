import type { Metadata } from "next";
import Link from "next/link";
import { ErrorBanner } from "@/components/ErrorBanner";
import { VersionBadge } from "@/components/VersionBadge";
import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "hl-agent",
  description: "Hyperliquid AI trading agent dashboard",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className="h-full">
      <body className="min-h-full flex flex-col">
        <Providers>
          <header className="sticky top-0 z-20 border-b border-[var(--panel-border)] bg-[var(--panel)]/95 backdrop-blur-sm">
            <div className="max-w-6xl mx-auto px-6 py-3 flex items-center gap-6">
              <h1 className="text-lg font-semibold">hl-agent</h1>
              <nav className="flex gap-4 text-sm text-[var(--muted)]">
                <Link
                  href="/"
                  className="hover:text-[var(--foreground)] transition-colors"
                >
                  Dashboard
                </Link>
                <Link
                  href="/trades"
                  className="hover:text-[var(--foreground)] transition-colors"
                >
                  Trades
                </Link>
                <Link
                  href="/settings"
                  className="hover:text-[var(--foreground)] transition-colors"
                >
                  Settings
                </Link>
              </nav>
              <div className="ml-auto">
                <VersionBadge />
              </div>
            </div>
          </header>
          <ErrorBanner />
          <main className="flex-1 max-w-6xl mx-auto w-full px-6 py-6">
            {children}
          </main>
        </Providers>
      </body>
    </html>
  );
}
