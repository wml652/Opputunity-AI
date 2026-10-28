import type { ReactNode } from "react";

export default function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-full flex-col">
      <header className="flex h-10 items-center border-b border-line bg-surface px-3">
        <span className="font-mono text-xs font-semibold tracking-tight">OPPORTUNITY/CTL</span>
      </header>
      <main className="flex flex-1 items-start justify-center px-4 pt-24">
        <div className="w-full max-w-[360px]">{children}</div>
      </main>
    </div>
  );
}
