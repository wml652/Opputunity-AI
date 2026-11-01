"use client";

import { useState } from "react";

import { ModelSwitcher } from "@/components/llm/model-switcher";
import { useProviders } from "@/lib/queries";

/** The active chat model in the telemetry bar; one click opens the catalog switcher. */
export function QuickModel() {
  const { data: providers } = useProviders();
  const [open, setOpen] = useState(false);
  const active = providers?.find((p) => p.is_active);
  if (!active) return null;
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="max-w-64 truncate rounded-xs border border-line px-1.5 py-0.5 font-mono text-[10px] text-muted hover:text-fg"
        title="Switch model"
      >
        {active.model}
      </button>
      <ModelSwitcher provider={active} open={open} onOpenChange={setOpen} />
    </>
  );
}
