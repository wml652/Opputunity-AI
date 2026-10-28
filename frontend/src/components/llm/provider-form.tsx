"use client";

import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/field";
import { Panel } from "@/components/ui/panel";
import { Select } from "@/components/ui/select";
import { ApiError } from "@/lib/api";
import {
  useCreateProvider,
  useFetchModels,
  usePresets,
  useVerifyConnection,
  type ProviderInput,
} from "@/lib/queries";
import type { ProviderKind } from "@/lib/schemas";

interface Props {
  onDone: () => void;
  onCancel?: () => void;
}

function optional(value: FormDataEntryValue | null): string | undefined {
  const text = typeof value === "string" ? value.trim() : "";
  return text ? text : undefined;
}

/**
 * Presets only pre-fill the base URL. The chat model (required) and embedding model (optional;
 * blank = local deterministic embeddings) are always typed or picked by the user — the
 * "Load models" helper lists what the key can access but never selects anything itself.
 */
export function ProviderForm({ onDone, onCancel }: Props) {
  const { data: presets } = usePresets();
  const create = useCreateProvider();
  const verify = useVerifyConnection();
  const fetchModels = useFetchModels();
  const [kind, setKind] = useState<ProviderKind>("NVIDIA_NIM");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");
  const [embeddingModel, setEmbeddingModel] = useState("");
  const [showEmbedding, setShowEmbedding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const preset = presets?.find((p) => p.kind === kind);
  const isCustom = kind === "CUSTOM";
  const models = fetchModels.data?.models ?? [];

  function onKindChange(next: ProviderKind) {
    setKind(next);
    setModel("");
    setEmbeddingModel("");
    fetchModels.reset();
  }

  function loadModels() {
    setError(null);
    fetchModels.mutate(
      { kind, api_key: apiKey.trim(), base_url: isCustom ? baseUrl.trim() : undefined },
      {
        onError: (err) =>
          setError(
            err instanceof ApiError ? `Could not load models: ${err.message}` : "Could not load models",
          ),
      },
    );
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const formEl = event.currentTarget;
    const form = new FormData(formEl);
    if (!apiKey.trim()) return setError("API key is required");
    if (!model.trim()) return setError("Chat model is required: type it or load it from the API");
    const body: ProviderInput = {
      kind,
      label: optional(form.get("label")),
      base_url: isCustom ? baseUrl.trim() || undefined : undefined,
      model: model.trim(),
      api_key: apiKey.trim(),
      embedding_base_url: optional(form.get("embedding_base_url")),
      embedding_model: embeddingModel.trim() || undefined,
      embedding_api_key: optional(form.get("embedding_api_key")),
      activate: true,
    };
    setError(null);
    try {
      const created = await create.mutateAsync(body);
      formEl.reset();
      verify.mutate(created.id); // immediate live handshake with the exact saved model
      onDone();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save provider");
    }
  }

  return (
    <Panel title="Add provider">
      <form onSubmit={onSubmit} className="grid grid-cols-2 gap-3 p-3" autoComplete="off">
        <datalist id="provider-models">
          {models.map((m) => (
            <option key={m} value={m} />
          ))}
        </datalist>
        <Field id="kind" label="Preset">
          <Select
            id="kind"
            value={kind}
            onValueChange={(v) => onKindChange(v as ProviderKind)}
            options={(presets ?? []).map((p) => ({ value: p.kind, label: p.label }))}
          />
        </Field>
        <Field id="label" label="Label" hint="Optional display name">
          <Input id="label" name="label" placeholder={preset?.label} />
        </Field>
        <Field
          id="base_url"
          label="Base URL"
          hint={isCustom ? "Any OpenAI-compatible endpoint (HTTPS, public host)" : "Pre-filled by the preset"}
        >
          <Input
            id="base_url"
            value={isCustom ? baseUrl : (preset?.base_url ?? "")}
            onChange={(e) => setBaseUrl(e.target.value)}
            disabled={!isCustom}
            placeholder="https://gateway.example.com/v1"
            className="font-mono text-xs"
          />
        </Field>
        <Field id="api_key" label="API key" hint="Stored encrypted; never shown again">
          <Input
            id="api_key"
            type="password"
            autoComplete="new-password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={kind === "NVIDIA_NIM" ? "nvapi-…" : "sk-…"}
            className="font-mono text-xs"
          />
        </Field>

        <Field
          id="model"
          label="Chat model (required)"
          className="col-span-2"
          hint={
            models.length
              ? `${models.length} models available to this key: type to filter, or enter any id`
              : "Type the exact model id, or load the list your key can access"
          }
        >
          <div className="flex gap-2">
            <Input
              id="model"
              list="provider-models"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder="exact model id"
              className="font-mono text-xs"
              required
            />
            <Button
              onClick={loadModels}
              disabled={!apiKey.trim() || (isCustom && !baseUrl.trim()) || fetchModels.isPending}
            >
              {fetchModels.isPending ? "Loading" : "Load models from API"}
            </Button>
          </div>
        </Field>

        <div className="col-span-2 border-t border-line pt-2">
          <button
            type="button"
            className="text-xs text-muted hover:text-fg"
            onClick={() => setShowEmbedding((v) => !v)}
          >
            {showEmbedding ? "Hide" : "Show"} embedding model
          </button>
          <span className="ml-2 font-mono text-2xs text-faint">
            {embeddingModel.trim() || "blank = local/hashing-1024 (deterministic, offline)"}
          </span>
        </div>
        {showEmbedding ? (
          <>
            <Field
              id="embedding_model"
              label="Embedding model"
              hint="Optional. Must return 1024-dim vectors (requested with dimensions=1024)"
            >
              <Input
                id="embedding_model"
                list="provider-models"
                value={embeddingModel}
                onChange={(e) => setEmbeddingModel(e.target.value)}
                placeholder="exact embedding model id"
                className="font-mono text-xs"
              />
            </Field>
            <Field id="embedding_base_url" label="Embedding base URL" hint="Only if embeddings use a different host">
              <Input
                id="embedding_base_url"
                name="embedding_base_url"
                placeholder="same as chat host"
                className="font-mono text-xs"
              />
            </Field>
            <Field
              id="embedding_api_key"
              label="Embedding API key"
              className="col-span-2"
              hint="Only needed when the embedding host differs from the chat host"
            >
              <Input
                id="embedding_api_key"
                name="embedding_api_key"
                type="password"
                autoComplete="new-password"
                className="font-mono text-xs"
              />
            </Field>
          </>
        ) : null}

        {error ? (
          <p className="col-span-2 rounded-sm border border-err/30 bg-err-soft px-2 py-1 text-xs text-err">
            {error}
          </p>
        ) : null}
        <div className="col-span-2 flex justify-end gap-2">
          {onCancel ? (
            <Button variant="ghost" onClick={onCancel}>
              Cancel
            </Button>
          ) : null}
          <Button type="submit" variant="primary" disabled={create.isPending || !model.trim()}>
            {create.isPending ? "Saving" : "Save and verify"}
          </Button>
        </div>
      </form>
    </Panel>
  );
}
