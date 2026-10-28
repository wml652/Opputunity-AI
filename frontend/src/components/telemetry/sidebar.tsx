"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/cn";
import { useInboxSummary } from "@/lib/queries";

interface NavItem {
  href: string;
  label: string;
  milestone?: string; // not yet available
  badge?: boolean; // shows the live inbox count
}

const SECTIONS: { title: string; items: NavItem[] }[] = [
  {
    title: "Operate",
    items: [
      { href: "/", label: "Pipeline" },
      { href: "/inbox", label: "Action Inbox", badge: true },
      { href: "/campaigns", label: "Campaigns" },
      { href: "/opportunities", label: "Opportunities" },
    ],
  },
  {
    title: "Vault",
    items: [
      { href: "/vault/profile", label: "Student Profile" },
      { href: "/vault/documents", label: "Documents" },
      { href: "/vault/essays", label: "Reference Essays" },
      { href: "/vault/memory", label: "Q&A Memory" },
    ],
  },
  {
    title: "System",
    items: [
      { href: "/settings/llm", label: "LLM Gateway" },
      { href: "/settings/notifications", label: "Notifications" },
    ],
  },
];

export function Sidebar() {
  const pathname = usePathname();
  const { data: inbox } = useInboxSummary();
  const inboxTotal = inbox?.total ?? 0;
  return (
    <nav className="flex w-48 shrink-0 flex-col gap-4 border-r border-line bg-surface px-2 py-3">
      {SECTIONS.map((section) => (
        <div key={section.title} className="flex flex-col gap-px">
          <span className="px-2 pb-1 text-2xs font-medium uppercase tracking-wide text-faint">
            {section.title}
          </span>
          {section.items.map((item) => {
            const active = pathname === item.href || (item.href !== "/" && pathname.startsWith(`${item.href}/`));
            if (item.milestone) {
              return (
                <span
                  key={item.href}
                  className="flex h-7 items-center justify-between rounded-sm px-2 text-sm text-faint"
                  title={`Available in milestone ${item.milestone}`}
                >
                  {item.label}
                  <span className="font-mono text-2xs">{item.milestone}</span>
                </span>
              );
            }
            return (
              <Link
                key={item.href}
                href={item.href}
                className={cn(
                  "flex h-7 items-center justify-between rounded-sm px-2 text-sm",
                  active ? "bg-surface-3 font-medium text-fg" : "text-muted hover:bg-surface-2 hover:text-fg",
                )}
              >
                {item.label}
                {item.badge && inboxTotal > 0 ? (
                  <span className="rounded-xs border border-warn/40 bg-warn-soft px-1 font-mono text-2xs text-warn tabular">
                    {inboxTotal}
                  </span>
                ) : null}
              </Link>
            );
          })}
        </div>
      ))}
    </nav>
  );
}
