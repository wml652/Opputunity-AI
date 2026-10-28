"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

import { api } from "./api";
import {
  connectionStatusSchema,
  fetchModelsSchema,
  inboxSummarySchema,
  presetSchema,
  promptTestSchema,
  providerConfigSchema,
  sessionSchema,
  userSchema,
  verifyResponseSchema,
  workerStatusSchema,
  type ProviderKind,
} from "./schemas";

export const keys = {
  me: ["me"] as const,
  presets: ["llm", "presets"] as const,
  providers: ["llm", "providers"] as const,
  status: ["llm", "status"] as const,
  workers: ["system", "workers"] as const,
  inbox: ["inbox", "summary"] as const,
};

export function useMe() {
  return useQuery({
    queryKey: keys.me,
    queryFn: () => api("/api/v1/auth/me", { schema: userSchema }),
    retry: false,
    staleTime: 5 * 60_000,
  });
}

export function useLogin(mode: "login" | "register") {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { email: string; password: string; display_name?: string }) =>
      api(`/api/v1/auth/${mode}`, { method: "POST", body, schema: sessionSchema }),
    onSuccess: (session) => {
      qc.clear();
      qc.setQueryData(keys.me, session.user);
    },
  });
}

export function useLogout() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api("/api/v1/auth/logout", { method: "POST" }),
    onSettled: () => qc.clear(),
  });
}

export function usePresets() {
  return useQuery({
    queryKey: keys.presets,
    queryFn: () => api("/api/v1/llm/presets", { schema: z.array(presetSchema) }),
    staleTime: Infinity,
  });
}

export function useProviders() {
  return useQuery({
    queryKey: keys.providers,
    queryFn: () => api("/api/v1/llm/providers", { schema: z.array(providerConfigSchema) }),
  });
}

export function useConnectionStatus() {
  return useQuery({
    queryKey: keys.status,
    queryFn: () => api("/api/v1/llm/status", { schema: connectionStatusSchema }),
  });
}

export function useWorkers() {
  return useQuery({
    queryKey: keys.workers,
    queryFn: () => api("/api/v1/system/workers", { schema: workerStatusSchema }),
    refetchInterval: 15_000,
  });
}

export function useInboxSummary() {
  return useQuery({
    queryKey: keys.inbox,
    queryFn: () => api("/api/v1/inbox/summary", { schema: inboxSummarySchema }),
    refetchInterval: 30_000,
  });
}

function useInvalidateLlm() {
  const qc = useQueryClient();
  return () => Promise.all([
    qc.invalidateQueries({ queryKey: keys.providers }),
    qc.invalidateQueries({ queryKey: keys.status }),
  ]);
}

export interface ProviderInput {
  kind: ProviderKind;
  label?: string;
  base_url?: string;
  model?: string;
  api_key: string;
  embedding_base_url?: string;
  embedding_model?: string;
  embedding_api_key?: string;
  activate?: boolean;
}

export function useCreateProvider() {
  const invalidate = useInvalidateLlm();
  return useMutation({
    mutationFn: (body: ProviderInput) =>
      api("/api/v1/llm/providers", { method: "POST", body, schema: providerConfigSchema }),
    onSuccess: invalidate,
  });
}

export function useActivateProvider() {
  const invalidate = useInvalidateLlm();
  return useMutation({
    mutationFn: (id: string) =>
      api(`/api/v1/llm/providers/${id}/activate`, {
        method: "POST",
        schema: providerConfigSchema,
      }),
    onSuccess: invalidate,
  });
}

export function useDeleteProvider() {
  const invalidate = useInvalidateLlm();
  return useMutation({
    mutationFn: (id: string) => api(`/api/v1/llm/providers/${id}`, { method: "DELETE" }),
    onSuccess: invalidate,
  });
}

export function useVerifyConnection() {
  const invalidate = useInvalidateLlm();
  return useMutation({
    mutationFn: (providerConfigId?: string) =>
      api("/api/v1/llm/verify", {
        method: "POST",
        body: { provider_config_id: providerConfigId ?? null },
        schema: verifyResponseSchema,
      }),
    onSettled: invalidate,
  });
}

export function useTestPrompt() {
  return useMutation({
    mutationFn: (body: { prompt: string; max_tokens: number; provider_config_id?: string }) =>
      api("/api/v1/llm/test-prompt", { method: "POST", body, schema: promptTestSchema }),
  });
}

export function useFetchModels() {
  return useMutation({
    mutationFn: (body: {
      provider_config_id?: string;
      kind?: ProviderKind;
      base_url?: string;
      api_key?: string;
    }) => api("/api/v1/llm/fetch-models", { method: "POST", body, schema: fetchModelsSchema }),
  });
}
