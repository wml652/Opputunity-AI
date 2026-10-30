"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

import { api } from "./api";
import { discoveryKeys } from "./discovery-queries";
import {
  answerSchema,
  applicationDetailSchema,
  channelSchema,
  deliverySchema,
  draftSchema,
  inboxSchema,
  notificationSettingsSchema,
  pageDetailSchema,
  verificationRecordSchema,
  verificationSchema,
} from "./m4-schemas";
import { keys } from "./queries";

export const m4Keys = {
  inbox: ["m4", "inbox"] as const,
  application: (id: string) => ["m4", "application", id] as const,
  applications: ["m4", "application"] as const,
  page: (campaignId: string, pageId: string) => ["m4", "page", campaignId, pageId] as const,
  drafts: ["m4", "drafts"] as const,
  notifications: ["m4", "notifications"] as const,
};

// ---- Crawl inspector -------------------------------------------------------------------------

export function usePageDetail(campaignId: string | null, pageId: string | null) {
  return useQuery({
    queryKey: m4Keys.page(campaignId ?? "none", pageId ?? "none"),
    queryFn: () => api(`/api/v1/campaigns/${campaignId}/pages/${pageId}`, { schema: pageDetailSchema }),
    enabled: campaignId !== null && pageId !== null,
  });
}

export function useRetryPage() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ campaignId, pageId }: { campaignId: string; pageId: string }) =>
      api(`/api/v1/campaigns/${campaignId}/pages/${pageId}/retry`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: discoveryKeys.all }),
  });
}

// ---- Applications & inbox --------------------------------------------------------------------

export function useInbox() {
  return useQuery({ queryKey: m4Keys.inbox, queryFn: () => api("/api/v1/inbox", { schema: inboxSchema }) });
}

export function useApplication(id: string) {
  return useQuery({
    queryKey: m4Keys.application(id),
    queryFn: () => api(`/api/v1/applications/${id}`, { schema: applicationDetailSchema }),
  });
}

function useInvalidateApplications() {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: ["m4"] });
    void qc.invalidateQueries({ queryKey: keys.inbox });
    void qc.invalidateQueries({ queryKey: discoveryKeys.all });
  };
}

export function useStartFilling() {
  const invalidate = useInvalidateApplications();
  return useMutation({
    mutationFn: (applicationId: string) =>
      api(`/api/v1/applications/${applicationId}/start`, { method: "POST" }),
    onSuccess: invalidate,
  });
}

const batchResultSchema = z.object({
  started: z.array(z.string().uuid()),
  skipped_no_portal: z.number().int(),
});
export type BatchResult = z.infer<typeof batchResultSchema>;

/** Fill the top-N QUEUED applications (closest deadline first) up to the final OK gate. Never
 * submits: each one still needs the human "OK - Verify & Submit". */
export function useBatchPrepare() {
  const invalidate = useInvalidateApplications();
  return useMutation({
    mutationFn: (limit: number) =>
      api("/api/v1/applications/batch-prepare", {
        method: "POST",
        body: { limit },
        schema: batchResultSchema,
      }),
    onSuccess: invalidate,
  });
}

export function useAnswerQuestion() {
  const invalidate = useInvalidateApplications();
  return useMutation({
    mutationFn: (v: { id: string; answer: string; save_to_memory: boolean }) =>
      api(`/api/v1/inbox/questions/${v.id}/answer`, {
        method: "POST",
        body: { answer: v.answer, save_to_memory: v.save_to_memory },
        schema: answerSchema,
      }),
    onSuccess: invalidate,
  });
}

// ---- Essay drafts ----------------------------------------------------------------------------

export function useDrafts() {
  return useQuery({
    queryKey: m4Keys.drafts,
    queryFn: () => api("/api/v1/essays/drafts", { schema: z.array(draftSchema) }),
  });
}

export function useSynthesizeDraft() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { prompt: string; word_limit: number | null }) =>
      api("/api/v1/essays/drafts", { method: "POST", body, schema: draftSchema }),
    onSuccess: () => qc.invalidateQueries({ queryKey: m4Keys.drafts }),
  });
}

export function useEditDraft() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: string }) =>
      api(`/api/v1/essays/drafts/${id}`, { method: "PUT", body: { body }, schema: draftSchema }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["m4"] }),
  });
}

// ---- Notifications ---------------------------------------------------------------------------

export function useNotificationSettings() {
  return useQuery({
    queryKey: m4Keys.notifications,
    queryFn: () => api("/api/v1/notifications", { schema: notificationSettingsSchema }),
  });
}

function useNotificationMutation<V, T>(fn: (v: V) => Promise<T>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSettled: () => qc.invalidateQueries({ queryKey: m4Keys.notifications }),
  });
}

export function useSaveChannel() {
  return useNotificationMutation((body: { channel: "EMAIL" | "WHATSAPP"; address: string }) =>
    api("/api/v1/notifications/channels", { method: "POST", body, schema: channelSchema }),
  );
}

export function useVerifyChannel() {
  return useNotificationMutation(({ id, code }: { id: string; code: string }) =>
    api(`/api/v1/notifications/channels/${id}/verify`, { method: "POST", body: { code }, schema: channelSchema }),
  );
}

export function useDeleteChannel() {
  return useNotificationMutation((id: string) =>
    api(`/api/v1/notifications/channels/${id}`, { method: "DELETE" }),
  );
}

export function useTestNotification() {
  return useNotificationMutation(() =>
    api("/api/v1/notifications/test", { method: "POST", schema: z.array(deliverySchema) }),
  );
}

// ---- Final verification gate (M5) -------------------------------------------------------------

export function useVerification(id: string | null) {
  return useQuery({
    queryKey: ["m4", "verification", id ?? "none"],
    queryFn: () => api(`/api/v1/applications/${id}/verification`, { schema: verificationSchema }),
    enabled: id !== null,
  });
}

export function useApprove() {
  const invalidate = useInvalidateApplications();
  return useMutation({
    mutationFn: (v: { id: string; preview_hash: string; edits: Record<string, string> }) =>
      api(`/api/v1/applications/${v.id}/approve`, {
        method: "POST",
        body: { preview_hash: v.preview_hash, edits: v.edits },
        schema: verificationRecordSchema,
      }),
    onSettled: invalidate,
  });
}

export function useReject() {
  const invalidate = useInvalidateApplications();
  return useMutation({
    mutationFn: (v: { id: string; preview_hash: string; reason: string }) =>
      api(`/api/v1/applications/${v.id}/reject`, {
        method: "POST",
        body: { preview_hash: v.preview_hash, reason: v.reason },
      }),
    onSettled: invalidate,
  });
}
