"use client";

import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";
import { StatusDot } from "@/components/ui/status";
import { cn } from "@/lib/cn";
import { useEventStream } from "@/lib/event-stream";
import { useInboxSummary, useLogout, useMe, useWorkers } from "@/lib/queries";

import { ConnectionBadge } from "./connection-badge";
import { ProviderSelect } from "./provider-select";

function Divider() {
  return <span className="h-4 w-px bg-line" aria-hidden="true" />;
}

function Metric({ label, value, tone }: { label: string; value: string; tone?: "warn" | "ok" | "err" }) {
  return (
    <span className="flex items-center gap-1.5 text-xs text-muted">
      {tone ? <StatusDot tone={tone} /> : null}
      {label}
      <span className={cn("font-mono text-2xs tabular", tone === "warn" ? "text-warn" : "text-fg")}>
        {value}
      </span>
    </span>
  );
}

export function TelemetryBar() {
  const router = useRouter();
  const { data: me } = useMe();
  const { data: workers } = useWorkers();
  const { data: inbox } = useInboxSummary();
  const { state } = useEventStream();
  const logout = useLogout();

  const activeWorkers = workers?.active ?? 0;
  const inboxTotal = inbox?.total ?? 0;

  return (
    <header className="flex h-10 shrink-0 items-center gap-3 border-b border-line bg-surface px-3">
      <span className="font-mono text-xs font-semibold tracking-tight text-fg">OPPORTUNITY/CTL</span>
      <Divider />
      <ProviderSelect />
      <ConnectionBadge />
      <div className="ml-auto flex items-center gap-3">
        <Metric
          label="Workers"
          value={String(activeWorkers)}
          tone={activeWorkers > 0 ? "ok" : "warn"}
        />
        <Divider />
        <Metric label="Inbox" value={String(inboxTotal)} tone={inboxTotal > 0 ? "warn" : undefined} />
        <Divider />
        <Metric
          label="Stream"
          value={state === "live" ? "live" : state}
          tone={state === "live" ? "ok" : "warn"}
        />
        <Divider />
        <span className="max-w-[180px] truncate text-xs text-muted">{me?.email}</span>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => logout.mutate(undefined, { onSettled: () => router.replace("/login") })}
        >
          Sign out
        </Button>
      </div>
    </header>
  );
}
