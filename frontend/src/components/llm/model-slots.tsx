"use client";

import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/field";
import { ApiError } from "@/lib/api";
import { useUpdateProvider } from "@/lib/queries";
import type { ProviderConfig } from "@/lib/schemas";

export const DEFAULT_RPM = 36;

export interface SlotValues {
  rerankModel: string;
  rerankBaseUrl: string;
  visionModel: string;
  rpm: string;
}

export const EMPTY_SLOTS: SlotValues = {
  rerankModel: "",
  rerankBaseUrl: "",
  visionModel: "",
  rpm: String(DEFAULT_RPM),
};

export function parseRpm(value: string): number | null {
  const n = Number(value.trim());
  return Number.isInteger(n) && n >= 1 && n <= 10_000 ? n : null;
}

/**
 * Optional rerank / vision model slots and the request pacing limit. Every model field is blank
 * by default and only ever holds what the user typed: blank rerank = local hybrid BM25 +
 * embedding scorer; blank vision = text/PDF parsing only.
 */
export function ModelSlotFields({
  values,
  onChange,
}: {
  values: SlotValues;
  onChange: (next: SlotValues) => void;
}) {
  const set = (key: keyof SlotValues) => (e: { target: { value: string } }) =>
    onChange({ ...values, [key]: e.target.value });
  return (
    <>
      <Field
        id="rerank_model"
        label="Rerank model (optional)"
        hint="Blank = local hybrid scorer (BM25 + signals + embeddings) filters long pages"
      >
        <Input
          id="rerank_model"
          list="provider-models"
          value={values.rerankModel}
          onChange={set("rerankModel")}
          placeholder="optional: exact model id"
          className="font-mono text-xs"
        />
      </Field>
      <Field
        id="rerank_base_url"
        label="Rerank base URL (optional)"
        hint="Only if the ranking endpoint is on a different host"
      >
        <Input
          id="rerank_base_url"
          value={values.rerankBaseUrl}
          onChange={set("rerankBaseUrl")}
          placeholder="same as chat host"
          className="font-mono text-xs"
        />
      </Field>
      <Field
        id="vision_model"
        label="Vision model (optional)"
        hint="Blank = text and PDF parsing only. Set to read image-only pages and scanned PDFs"
      >
        <Input
          id="vision_model"
          list="provider-models"
          value={values.visionModel}
          onChange={set("visionModel")}
          placeholder="optional: exact model id"
          className="font-mono text-xs"
        />
      </Field>
      <Field
        id="rpm_limit"
        label="Requests per minute"
        hint="Shared pacing for every call on this key (chat, extraction, rerank, vision)"
      >
        <Input
          id="rpm_limit"
          inputMode="numeric"
          value={values.rpm}
          onChange={set("rpm")}
          className="font-mono text-xs tabular"
        />
      </Field>
    </>
  );
}

export function ProviderSlotsEditor({
  provider,
  onDone,
}: {
  provider: ProviderConfig;
  onDone: () => void;
}) {
  const update = useUpdateProvider();
  const [values, setValues] = useState<SlotValues>({
    rerankModel: provider.rerank_model ?? "",
    rerankBaseUrl: provider.rerank_base_url ?? "",
    visionModel: provider.vision_model ?? "",
    rpm: String(provider.rpm_limit ?? DEFAULT_RPM),
  });
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const rpm = parseRpm(values.rpm);
    if (rpm === null) return setError("Requests per minute must be a whole number from 1 to 10000");
    setError(null);
    try {
      await update.mutateAsync({
        id: provider.id,
        body: {
          rerank_model: values.rerankModel.trim(),
          rerank_base_url: values.rerankBaseUrl.trim(),
          vision_model: values.visionModel.trim(),
          rpm_limit: rpm,
        },
      });
      onDone();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save");
    }
  }

  return (
    <form onSubmit={onSubmit} className="grid grid-cols-4 gap-3 p-3" autoComplete="off">
      <ModelSlotFields values={values} onChange={setValues} />
      {error ? <p className="col-span-4 text-xs text-err">{error}</p> : null}
      <div className="col-span-4 flex justify-end gap-2">
        <Button variant="ghost" onClick={onDone}>
          Cancel
        </Button>
        <Button type="submit" variant="primary" disabled={update.isPending}>
          {update.isPending ? "Saving" : "Save"}
        </Button>
      </div>
    </form>
  );
}
