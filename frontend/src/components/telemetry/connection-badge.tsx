"use client";

import Link from "next/link";

import { StatusDot, type Tone } from "@/components/ui/status";
import { cn, formatRelative } from "@/lib/cn";
import { useConnectionStatus, useVerifyConnection } from "@/lib/queries";

export function ConnectionBadge() {
  const { data, isLoading } = useConnectionStatus();
  const verify = useVerifyConnection();
  const provider = data?.provider ?? null;
  const check = provider?.last_check ?? null;

  if (isLoading) {
    return <BadgeShell tone="idle" label="Checking" />;
  }
  if (!provider) {
    return (
      <Link href="/settings/llm" className="rounded-sm hover:bg-surface-2">
        <BadgeShell tone="idle" label="No provider configured" />
      </Link>
    );
  }

  let tone: Tone = "idle";
  let label = "Not verified";
  let detail: string | null = null;
  if (verify.isPending) {
    tone = "warn";
    label = "Verifying";
  } else if (check?.ok) {
    tone = "ok";
    label = "Connection Established";
    detail = `${check.latency_ms ?? "?"} ms`;
  } else if (check) {
    tone = "err";
    label = "Connection Failed";
    detail = check.error_code ?? "error";
  }

  const title = check
    ? `Last handshake ${formatRelative(check.checked_at)} — click to re-verify`
    : "Click to run a live handshake";

  return (
    <button
      type="button"
      onClick={() => verify.mutate(provider.id)}
      disabled={verify.isPending}
      title={title}
      className="rounded-sm text-left hover:bg-surface-2 disabled:cursor-wait"
    >
      <BadgeShell tone={tone} label={label} detail={detail} model={provider.model} pulse={verify.isPending} />
    </button>
  );
}

function BadgeShell({
  tone,
  label,
  detail,
  model,
  pulse,
}: {
  tone: Tone;
  label: string;
  detail?: string | null;
  model?: string;
  pulse?: boolean;
}) {
  const text = { ok: "text-ok", warn: "text-warn", err: "text-err", idle: "text-muted" }[tone];
  return (
    <span className="flex h-7 items-center gap-2 px-2">
      <StatusDot tone={tone} pulse={pulse} />
      <span className={cn("text-xs font-medium", text)}>{label}</span>
      {detail ? <span className="font-mono text-2xs text-fg tabular">{detail}</span> : null}
      {model ? (
        <span className="max-w-[220px] truncate font-mono text-2xs text-faint">{model}</span>
      ) : null}
    </span>
  );
}
