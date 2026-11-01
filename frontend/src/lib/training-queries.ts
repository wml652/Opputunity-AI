"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

import { api } from "./api";

const actionSchema = z.record(z.unknown());

export const trajectorySchema = z.object({
  id: z.string(),
  source: z.string(),
  domain: z.string(),
  dom_signature: z.string(),
  chosen_action: actionSchema,
  rejected_action: actionSchema.nullable(),
  created_at: z.string().nullable(),
});
export type TrajectoryT = z.infer<typeof trajectorySchema>;

const overviewSchema = z.object({
  stats: z.object({
    total: z.number(),
    by_source: z.record(z.number()),
    sft_records: z.number(),
    dpo_pairs: z.number(),
    rag_decisions: z.number(),
    llm_decisions: z.number(),
    rag_hit_rate: z.number(),
  }),
  trajectories: z.array(trajectorySchema),
});
export type TrainingOverviewT = z.infer<typeof overviewSchema>;

const backfillSchema = z.object({ from_workflows: z.number(), from_fields: z.number() });

export const trainingKeys = { overview: ["training", "overview"] as const };

export function useTrainingOverview() {
  return useQuery({
    queryKey: trainingKeys.overview,
    queryFn: () => api("/api/v1/training/overview?limit=100", { schema: overviewSchema }),
    refetchInterval: 15_000,
  });
}

export function useBackfill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api("/api/v1/training/backfill", { method: "POST", schema: backfillSchema }),
    onSuccess: () => qc.invalidateQueries({ queryKey: trainingKeys.overview }),
  });
}

/** Authenticated file download (the cookie session rides along on a same-origin GET). */
export async function downloadFile(path: string, filename: string): Promise<void> {
  const res = await fetch(path, { credentials: "include" });
  if (!res.ok) throw new Error(`download failed (HTTP ${res.status})`);
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
