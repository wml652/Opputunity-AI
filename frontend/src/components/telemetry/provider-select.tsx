"use client";

import Link from "next/link";

import { Select } from "@/components/ui/select";
import { useActivateProvider, useProviders } from "@/lib/queries";

const KIND_LABEL = { NVIDIA_NIM: "NIM", OPENROUTER: "OpenRouter", CUSTOM: "Custom" } as const;

export function ProviderSelect() {
  const { data: providers } = useProviders();
  const activate = useActivateProvider();

  if (!providers || providers.length === 0) {
    return (
      <Link href="/settings/llm" className="text-xs text-muted underline-offset-2 hover:underline">
        Add provider
      </Link>
    );
  }

  const active = providers.find((p) => p.is_active);
  return (
    <Select
      aria-label="Active LLM provider"
      value={active?.id}
      onValueChange={(id) => {
        if (id !== active?.id) activate.mutate(id);
      }}
      disabled={activate.isPending}
      placeholder="Select provider"
      className="w-[200px]"
      options={providers.map((p) => ({
        value: p.id,
        label: (
          <span className="flex items-center gap-2">
            <span className="font-mono text-2xs text-faint">{KIND_LABEL[p.kind]}</span>
            <span className="truncate">{p.label}</span>
          </span>
        ),
      }))}
    />
  );
}
