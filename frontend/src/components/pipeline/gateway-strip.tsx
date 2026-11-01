"use client";

import Link from "next/link";

import { cn } from "@/lib/cn";
import { useActivateProvider, useProviders } from "@/lib/queries";
import { useBrowserSessions, useSearchKeys } from "@/lib/session-queries";

function Slot({ label, value, muted }: { label: string; value: string; muted?: boolean }) {
  return (
    <span className="inline-flex min-w-0 items-center gap-1 rounded-xs border border-line px-1.5 py-0.5">
      <span className="text-[10px] uppercase tracking-wide text-faint">{label}</span>
      <span className={cn("truncate font-mono text-[10px]", muted ? "text-faint" : "text-fg")} title={value}>
        {value}
      </span>
    </span>
  );
}

/**
 * Quick-access status of the LLM gateway (chat / fast extraction / rerank / vision / RPM) and of
 * Search & Sessions, right above the campaign launcher. Model ids are only ever what the user
 * saved; blank slots show their local fallback.
 */
export function GatewayStrip() {
  const { data: providers } = useProviders();
  const { data: keys } = useSearchKeys();
  const { data: sessions } = useBrowserSessions();
  const activate = useActivateProvider();
  const active = providers?.find((p) => p.is_active) ?? null;
  const portalSessions = sessions?.sessions.filter((s) => s.kind !== "LINKEDIN").length ?? 0;
  const linkedin = sessions?.sessions.some((s) => s.kind === "LINKEDIN") ?? false;
  const searchKeys = keys?.map((k) => k.provider).join(" + ") || "zero-key search";

  return (
    <div className="flex flex-wrap items-center gap-1.5 rounded-sm border border-line bg-surface px-3 py-1.5">
      <span className="text-2xs font-semibold uppercase tracking-wide text-muted">LLM gateway</span>
      {active ? (
        <>
          {providers && providers.length > 1 ? (
            <select
              aria-label="Active provider"
              value={active.id}
              disabled={activate.isPending}
              onChange={(e) => activate.mutate(e.target.value)}
              className="h-5 rounded-xs border border-line bg-surface px-1 text-[10px]"
            >
              {providers.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.label}
                </option>
              ))}
            </select>
          ) : (
            <span className="text-2xs">{active.label}</span>
          )}
          <Slot label="Chat" value={active.model} />
          <Slot label="Fast extraction" value={active.extraction_model ?? "= chat model"} muted={!active.extraction_model} />
          <Slot label="Rerank" value={active.rerank_model ?? "local hybrid"} muted={!active.rerank_model} />
          <Slot label="Vision" value={active.vision_model ?? "off"} muted={!active.vision_model} />
          <Slot label="RPM" value={String(active.rpm_limit ?? 36)} />
        </>
      ) : (
        <span className="text-2xs text-warn">no provider configured</span>
      )}
      <Link href="/settings/llm" className="text-[10px] text-muted underline-offset-2 hover:underline">
        edit
      </Link>
      <span className="mx-1 h-3 w-px bg-line" />
      <span className="text-2xs font-semibold uppercase tracking-wide text-muted">Search &amp; sessions</span>
      <Slot label="Search" value={searchKeys} muted={!keys?.length} />
      <Slot label="Portal sessions" value={String(portalSessions)} muted={portalSessions === 0} />
      <Slot label="LinkedIn" value={linkedin ? "signed in" : "public only"} muted={!linkedin} />
      <Link href="/settings/sessions" className="text-[10px] text-muted underline-offset-2 hover:underline">
        manage
      </Link>
    </div>
  );
}
