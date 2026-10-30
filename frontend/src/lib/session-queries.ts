"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

import { api } from "./api";

const sessionSchema = z.object({
  id: z.string(),
  domain: z.string(),
  kind: z.string(),
  created_at: z.string(),
  last_used_at: z.string().nullable(),
  expires_at: z.string().nullable(),
  tos_acknowledged_at: z.string().nullable(),
  daily_action_cap: z.number().nullable(),
  actions_today: z.number(),
});
export type SessionT = z.infer<typeof sessionSchema>;

const challengedSchema = z.object({
  domain: z.string(),
  vendor: z.string(),
  url: z.string().nullable(),
  campaign_id: z.string().nullable(),
  solved: z.boolean(),
});
export type ChallengedT = z.infer<typeof challengedSchema>;

const sessionsSchema = z.object({
  sessions: z.array(sessionSchema),
  challenged: z.array(challengedSchema),
  headful_available: z.boolean(),
});

const captureSchema = z.object({ domain: z.string(), message: z.string() });

const searchKeySchema = z.object({
  provider: z.string(),
  key_last4: z.string(),
  updated_at: z.string(),
});
export type SearchKeyT = z.infer<typeof searchKeySchema>;

export const sessionKeys = {
  sessions: ["sessions", "browser"] as const,
  search: ["sessions", "search"] as const,
};

export function useBrowserSessions() {
  return useQuery({
    queryKey: sessionKeys.sessions,
    queryFn: () => api("/api/v1/browser-sessions", { schema: sessionsSchema }),
  });
}

export interface CaptureInput {
  url: string;
  kind?: "CHALLENGE" | "LINKEDIN" | "PORTAL";
  tos_acknowledged?: boolean;
  daily_action_cap?: number;
}

export function useCaptureSession() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: CaptureInput) =>
      api("/api/v1/browser-sessions/capture", { method: "POST", body, schema: captureSchema }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sessions"] }),
  });
}

export function useDeleteSession() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api(`/api/v1/browser-sessions/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sessions"] }),
  });
}

export function useSearchKeys() {
  return useQuery({
    queryKey: sessionKeys.search,
    queryFn: () => api("/api/v1/search/credentials", { schema: z.array(searchKeySchema) }),
  });
}

export function useSaveSearchKey() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ provider, api_key }: { provider: string; api_key: string }) =>
      api(`/api/v1/search/credentials/${provider}`, {
        method: "PUT",
        body: { api_key },
        schema: searchKeySchema,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: sessionKeys.search }),
  });
}

export function useDeleteSearchKey() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (provider: string) => api(`/api/v1/search/credentials/${provider}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: sessionKeys.search }),
  });
}
