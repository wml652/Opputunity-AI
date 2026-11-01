"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/field";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import { useFetchModels, useUpdateProvider, useVerifyConnection, type ProviderSlotsPatch } from "@/lib/queries";
import type { ModelInfo, ProviderConfig } from "@/lib/schemas";

type Slot = "model" | "extraction_model" | "embedding_model" | "rerank_model" | "vision_model";
const SLOTS: { key: Slot; label: string; hint: string; categories: string[] }[] = [
  { key: "model", label: "Chat", hint: "required - every chat call", categories: ["chat", "reasoning", "fast", "vision"] },
  { key: "extraction_model", label: "Fast extraction", hint: "blank = chat model", categories: ["fast", "chat"] },
  { key: "embedding_model", label: "Embedding", hint: "blank = local hashing-1024", categories: ["embedding"] },
  { key: "rerank_model", label: "Rerank", hint: "blank = local hybrid scorer", categories: ["rerank"] },
  { key: "vision_model", label: "Vision", hint: "blank = text / PDF only", categories: ["vision"] },
];
const CATEGORIES = ["all", "chat", "reasoning", "fast", "vision", "embedding", "rerank", "other"] as const;

/**
 * 1-click model switching on an existing provider: loads the full catalog the user's key can
 * access (categorised), lets the user assign any listed model - or type any id - to each slot,
 * saves in place and re-verifies. Nothing is pre-selected: values are only what the user picks.
 */
export function ModelSwitcher({ provider, open, onOpenChange }: { provider: ProviderConfig; open: boolean; onOpenChange: (v: boolean) => void }) {
  const fetchModels = useFetchModels();
  const update = useUpdateProvider();
  const verify = useVerifyConnection();
  const [slot, setSlot] = useState<Slot>("model");
  const [category, setCategory] = useState<(typeof CATEGORIES)[number]>("all");
  const [query, setQuery] = useState("");
  const [draft, setDraft] = useState<Record<Slot, string>>({
    model: provider.model,
    extraction_model: provider.extraction_model ?? "",
    embedding_model: provider.embedding_model ?? "",
    rerank_model: provider.rerank_model ?? "",
    vision_model: provider.vision_model ?? "",
  });
  const [error, setError] = useState<string | null>(null);
  const loaded = fetchModels.data?.catalog;
  const catalog: ModelInfo[] = useMemo(() => loaded ?? [], [loaded]);
  const counts = fetchModels.data?.counts ?? {};

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return catalog.filter((m) => (category === "all" || m.category === category) && (!q || m.id.toLowerCase().includes(q)));
  }, [catalog, category, query]);

  function load() {
    setError(null);
    fetchModels.mutate(
      { provider_config_id: provider.id },
      { onError: (e) => setError(e instanceof ApiError ? e.message : "Could not load the catalog") },
    );
  }

  function pick(m: ModelInfo) {
    if (slot === "model" && !m.chat_capable) {
      setError(`${m.id} is a ${m.category} model and cannot answer chat prompts`);
      return;
    }
    setError(null);
    setDraft((d) => ({ ...d, [slot]: m.id }));
  }

  async function save() {
    if (!draft.model.trim()) return setError("The chat model is required");
    setError(null);
    const body: ProviderSlotsPatch = {
      model: draft.model.trim(),
      extraction_model: draft.extraction_model.trim(),
      embedding_model: draft.embedding_model.trim(),
      rerank_model: draft.rerank_model.trim(),
      vision_model: draft.vision_model.trim(),
    };
    try {
      await update.mutateAsync({ id: provider.id, body });
      verify.mutate(provider.id);
      onOpenChange(false);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not save");
    }
  }

  return (
    <Dialog.Root
      open={open}
      onOpenChange={(v) => {
        onOpenChange(v);
        if (v && !fetchModels.data && !fetchModels.isPending) load();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/40" />
        <Dialog.Content className="fixed inset-x-0 top-12 z-50 mx-auto flex max-h-[80vh] w-[860px] max-w-full flex-col overflow-hidden rounded-sm border border-line-strong bg-surface">
          <header className="flex items-center gap-2 border-b border-line px-3 py-2">
            <Dialog.Title className="text-sm font-semibold">Switch models · {provider.label}</Dialog.Title>
            <span className="truncate font-mono text-2xs text-faint">{provider.base_url}</span>
            <Button size="sm" variant="ghost" className="ml-auto" onClick={load} disabled={fetchModels.isPending}>
              {fetchModels.isPending ? "Loading catalog" : "Reload catalog"}
            </Button>
          </header>
          <div className="grid grid-cols-5 gap-px border-b border-line bg-line">
            {SLOTS.map((s) => (
              <button
                key={s.key}
                type="button"
                onClick={() => {
                  setSlot(s.key);
                  setCategory((s.categories[0] as (typeof CATEGORIES)[number]) ?? "all");
                }}
                className={cn("flex flex-col items-start gap-0.5 bg-surface px-2 py-1.5 text-left", slot === s.key && "bg-surface-2")}
              >
                <span className="text-2xs font-semibold uppercase tracking-wide text-muted">{s.label}</span>
                <span className={cn("w-full truncate font-mono text-[10px]", draft[s.key] ? "text-fg" : "text-faint")} title={draft[s.key]}>
                  {draft[s.key] || s.hint}
                </span>
              </button>
            ))}
          </div>
          <div className="flex items-center gap-2 border-b border-line px-3 py-1.5">
            <Input
              value={draft[slot]}
              onChange={(e) => setDraft((d) => ({ ...d, [slot]: e.target.value }))}
              placeholder="exact model id (type any id, or pick below)"
              className="w-80 font-mono text-xs"
            />
            {slot !== "model" ? (
              <Button size="sm" variant="ghost" onClick={() => setDraft((d) => ({ ...d, [slot]: "" }))}>
                Clear (use fallback)
              </Button>
            ) : null}
            <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="search the catalog" className="ml-auto w-56 text-xs" />
          </div>
          <div className="flex gap-px border-b border-line bg-line">
            {CATEGORIES.map((c) => (
              <button
                key={c}
                type="button"
                onClick={() => setCategory(c)}
                className={cn("h-6 flex-1 bg-surface px-2 text-2xs", category === c ? "bg-inverse text-inverse-fg" : "text-muted hover:text-fg")}
              >
                {c} <span className="font-mono text-[10px] opacity-70">{c === "all" ? catalog.length : (counts[c] ?? 0)}</span>
              </button>
            ))}
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            {!catalog.length ? (
              <p className="px-3 py-3 text-xs text-faint">
                {fetchModels.isPending ? "Loading every model your key can access" : "Load the catalog to see every model your key can access."}
              </p>
            ) : (
              shown.map((m) => (
                <button
                  key={m.id}
                  type="button"
                  onClick={() => pick(m)}
                  className={cn("flex w-full items-center gap-2 border-b border-line px-3 py-1 text-left hover:bg-surface-2", draft[slot] === m.id && "bg-surface-2")}
                >
                  <span className="truncate font-mono text-xs">{m.id}</span>
                  <span className="ml-auto shrink-0 rounded-xs border border-line px-1 font-mono text-[10px] text-muted">{m.category}</span>
                </button>
              ))
            )}
          </div>
          <p className="border-t border-line px-3 py-1 text-[10px] text-faint">
            NVIDIA NIM lists the models served on its chat endpoint; rerank models are served from a separate retrieval endpoint - type the id and set the Rerank base URL in Slots. A listed model can still be disabled for your account (HTTP 404 &quot;Function not found&quot;): pick another.
          </p>
          {error ? <p className="border-t border-line bg-err-soft px-3 py-1 text-xs text-err">{error}</p> : null}
          <footer className="flex justify-end gap-2 border-t border-line px-3 py-2">
            <Dialog.Close asChild>
              <Button variant="ghost">Cancel</Button>
            </Dialog.Close>
            <Button variant="primary" onClick={save} disabled={update.isPending}>
              {update.isPending ? "Saving" : "Save and verify"}
            </Button>
          </footer>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
