"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

import { api } from "./api";

export const hitlSessionSchema = z.object({
  id: z.string().uuid(),
  kind: z.string(),
  title: z.string(),
  url: z.string(),
  application_id: z.string().uuid().nullable(),
  started_at: z.string(),
});
export type HitlSessionT = z.infer<typeof hitlSessionSchema>;

/** Server -> viewer control messages on the stream (frames arrive as binary messages). */
export const hitlControlSchema = z.discriminatedUnion("type", [
  z.object({
    type: z.literal("meta"),
    viewport: z.object({ width: z.number(), height: z.number() }),
    kind: z.string(),
    title: z.string(),
  }),
  z.object({ type: z.literal("closed") }),
]);
export type HitlControlT = z.infer<typeof hitlControlSchema>;

export const hitlKeys = { sessions: ["hitl", "sessions"] as const };

export function useHitlSessions() {
  return useQuery({
    queryKey: hitlKeys.sessions,
    queryFn: () => api("/api/v1/hitl/sessions", { schema: z.array(hitlSessionSchema) }),
    refetchInterval: 30_000,
  });
}

export function useCloseHitlSession() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api(`/api/v1/hitl/sessions/${id}/close`, { method: "POST" }),
    onSettled: () => qc.invalidateQueries({ queryKey: hitlKeys.sessions }),
  });
}

/** Same-origin WebSocket URL (the console proxies /api to the backend; cookies ride along). */
export function hitlStreamUrl(sessionId: string): string {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}/api/v1/hitl/sessions/${sessionId}/stream`;
}
