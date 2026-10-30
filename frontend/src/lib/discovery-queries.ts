"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

import { api } from "./api";
import {
  campaignDetailSchema,
  campaignSchema,
  opportunityDetailSchema,
  opportunitySchema,
  pipelineSummarySchema,
  queueItemSchema,
  type Outcome,
  type Scope,
} from "./discovery-schemas";

export const discoveryKeys = {
  all: ["discovery"] as const,
  summary: ["discovery", "summary"] as const,
  campaigns: ["discovery", "campaigns"] as const,
  campaign: (id: string) => ["discovery", "campaigns", id] as const,
  opportunities: (outcome: string) => ["discovery", "opportunities", outcome] as const,
  opportunity: (id: string) => ["discovery", "opportunity", id] as const,
  queue: ["discovery", "queue"] as const,
};

export function usePipelineSummary() {
  return useQuery({
    queryKey: discoveryKeys.summary,
    queryFn: () => api("/api/v1/pipeline/summary", { schema: pipelineSummarySchema }),
  });
}

export function useCampaigns() {
  return useQuery({
    queryKey: discoveryKeys.campaigns,
    queryFn: () => api("/api/v1/campaigns", { schema: z.array(campaignSchema) }),
  });
}

export function useCampaign(id: string | null) {
  return useQuery({
    queryKey: discoveryKeys.campaign(id ?? "none"),
    queryFn: () => api(`/api/v1/campaigns/${id}`, { schema: campaignDetailSchema }),
    enabled: id !== null,
  });
}

export interface CampaignInput {
  name: string;
  scope: Scope;
  keywords: string[];
  target_urls: string[];
  use_websites: boolean;
  use_search: boolean;
  use_linkedin: boolean;
  use_linkedin_auth?: boolean;
  max_pages_per_site: number;
  max_depth: number;
  home_country: string | null;
  auto_stage?: boolean;
  auto_stage_limit?: number;
}

export function useCreateCampaign() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: CampaignInput) =>
      api("/api/v1/campaigns", { method: "POST", body, schema: campaignSchema }),
    onSuccess: () => qc.invalidateQueries({ queryKey: discoveryKeys.all }),
  });
}

export function useCancelCampaign() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api(`/api/v1/campaigns/${id}/cancel`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: discoveryKeys.all }),
  });
}

export function useOpportunities(outcome: Outcome | "ALL") {
  return useQuery({
    queryKey: discoveryKeys.opportunities(outcome),
    queryFn: () =>
      api(`/api/v1/opportunities${outcome === "ALL" ? "" : `?outcome=${outcome}`}`, {
        schema: z.array(opportunitySchema),
      }),
  });
}

export function useOpportunity(id: string | null) {
  return useQuery({
    queryKey: discoveryKeys.opportunity(id ?? "none"),
    queryFn: () => api(`/api/v1/opportunities/${id}`, { schema: opportunityDetailSchema }),
    enabled: id !== null,
  });
}

export function useQueue() {
  return useQuery({
    queryKey: discoveryKeys.queue,
    queryFn: () => api("/api/v1/applications/queue", { schema: z.array(queueItemSchema) }),
  });
}
