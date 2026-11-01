"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { EmptyRow, Panel, td, th } from "@/components/ui/panel";
import { StatusPill } from "@/components/ui/status";
import { ApiError } from "@/lib/api";
import { cn, formatRelative } from "@/lib/cn";
import {
  useActivateProvider,
  useDeleteProvider,
  useProviders,
  useVerifyConnection,
} from "@/lib/queries";
import type { ConnectionCheck, ProviderConfig } from "@/lib/schemas";

import { ProviderSlotsEditor } from "./model-slots";
import { ModelSwitcher } from "./model-switcher";
import { PromptTest } from "./prompt-test";
import { ProviderForm } from "./provider-form";

const KIND_LABEL = { NVIDIA_NIM: "NVIDIA NIM", OPENROUTER: "OpenRouter", CUSTOM: "Custom" } as const;

function CheckCell({ check }: { check: ConnectionCheck | null }) {
  if (!check) return <StatusPill tone="idle">Not verified</StatusPill>;
  return (
    <span className="flex items-center gap-2">
      <StatusPill tone={check.ok ? "ok" : "err"}>
        {check.ok ? "Connection Established" : "Connection Failed"}
      </StatusPill>
      <span className="font-mono text-2xs text-muted tabular">
        {check.ok ? `${check.latency_ms} ms` : check.error_code}
      </span>
      <span className="text-2xs text-faint">{formatRelative(check.checked_at)}</span>
    </span>
  );
}

function ProviderRow({ provider }: { provider: ProviderConfig }) {
  const verify = useVerifyConnection();
  const activate = useActivateProvider();
  const remove = useDeleteProvider();
  const error = verify.error ?? activate.error ?? remove.error;
  const [editing, setEditing] = useState(false);
  const [switching, setSwitching] = useState(false);

  return (
    <>
      <tr className="hover:bg-surface-2">
        <td className={td}>
          <div className="flex items-center gap-2">
            <span className="font-medium">{provider.label}</span>
            {provider.is_active ? (
              <span className="rounded-xs border border-line-strong px-1 font-mono text-2xs text-muted">
                ACTIVE
              </span>
            ) : null}
          </div>
          <div className="font-mono text-2xs text-faint">{provider.base_url}</div>
        </td>
        <td className={cn(td, "text-xs text-muted")}>{KIND_LABEL[provider.kind]}</td>
        <td className={cn(td, "font-mono text-2xs")}>
          {provider.model}
          {provider.extraction_model ? (
            <div className="text-faint" title="Fast extraction model (discovery only)">
              extraction: {provider.extraction_model}
            </div>
          ) : null}
          <div className="text-faint" title="Optional rerank / vision slots and request pacing">
            rerank: {provider.rerank_model ?? "local hybrid"} · vision: {provider.vision_model ?? "off"} ·{" "}
            <span className="tabular">{provider.rpm_limit ?? 36}</span> rpm
          </div>
        </td>
        <td className={cn(td, "font-mono text-2xs text-muted")}>
          ••••{provider.api_key_last4}
        </td>
        <td className={cn(td, "font-mono text-2xs text-muted")}>
          {provider.effective_embedding_model}
          {provider.effective_embedding_model.startsWith("local/") ? (
            <span className="ml-1 text-warn" title="Degraded: no embedding endpoint configured">
              fallback
            </span>
          ) : null}
        </td>
        <td className={td}>
          <CheckCell check={verify.isPending ? null : provider.last_check} />
          {verify.isPending ? <StatusPill tone="warn">Verifying</StatusPill> : null}
        </td>
        <td className={cn(td, "text-right")}>
          <div className="flex justify-end gap-1">
            <Button size="sm" onClick={() => verify.mutate(provider.id)} disabled={verify.isPending}>
              Verify
            </Button>
            <Button size="sm" variant="primary" onClick={() => setSwitching(true)} title="Switch any model slot in place">
              Models
            </Button>
            <Button size="sm" onClick={() => setEditing((v) => !v)}>
              Slots
            </Button>
            <ModelSwitcher provider={provider} open={switching} onOpenChange={setSwitching} />
            {!provider.is_active ? (
              <Button size="sm" onClick={() => activate.mutate(provider.id)} disabled={activate.isPending}>
                Activate
              </Button>
            ) : null}
            <Button
              size="sm"
              variant="danger"
              disabled={remove.isPending}
              onClick={() => {
                if (window.confirm(`Delete provider "${provider.label}"? The stored key is destroyed.`)) {
                  remove.mutate(provider.id);
                }
              }}
            >
              Delete
            </Button>
          </div>
        </td>
      </tr>
      {editing ? (
        <tr>
          <td colSpan={7} className="border-b border-line bg-surface-2">
            <ProviderSlotsEditor provider={provider} onDone={() => setEditing(false)} />
          </td>
        </tr>
      ) : null}
      {provider.last_check && !provider.last_check.ok && provider.last_check.error_message ? (
        <tr>
          <td colSpan={7} className="border-b border-line bg-err-soft px-3 py-1 font-mono text-2xs text-err">
            {provider.last_check.error_message}
          </td>
        </tr>
      ) : null}
      {error ? (
        <tr>
          <td colSpan={7} className="border-b border-line px-3 py-1 text-xs text-err">
            {error instanceof ApiError ? error.message : "Request failed"}
          </td>
        </tr>
      ) : null}
    </>
  );
}

export function LlmSettings() {
  const { data: providers, isLoading } = useProviders();
  const [adding, setAdding] = useState(false);
  const showForm = adding || (!isLoading && (providers?.length ?? 0) === 0);

  return (
    <div className="flex flex-col gap-3">
      <Panel
        title="LLM providers"
        meta={providers ? `${providers.length} configured` : undefined}
        actions={
          !showForm ? (
            <Button size="sm" variant="primary" onClick={() => setAdding(true)}>
              Add provider
            </Button>
          ) : null
        }
      >
        <table className="w-full border-collapse">
          <thead>
            <tr>
              <th className={th}>Provider</th>
              <th className={th}>Kind</th>
              <th className={th}>Chat model</th>
              <th className={th}>Key</th>
              <th className={th}>Embeddings (1024-d)</th>
              <th className={th}>Handshake</th>
              <th className={cn(th, "text-right")}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <EmptyRow colSpan={7}>Loading</EmptyRow>
            ) : !providers?.length ? (
              <EmptyRow colSpan={7}>No providers yet. Add one below.</EmptyRow>
            ) : (
              providers.map((p) => <ProviderRow key={p.id} provider={p} />)
            )}
          </tbody>
        </table>
      </Panel>

      <PromptTest activeModel={providers?.find((p) => p.is_active)?.model ?? null} />

      {showForm ? (
        <ProviderForm
          onDone={() => setAdding(false)}
          onCancel={providers?.length ? () => setAdding(false) : undefined}
        />
      ) : null}

      <p className="text-xs text-faint">
        Keys are encrypted at rest (AES-256-GCM, row-bound) and never returned by the API — only
        the last four characters are shown. Verification sends a 1-token chat completion and a
        <span className="font-mono"> /models </span> probe; latency is the chat round trip.
      </p>
    </div>
  );
}
