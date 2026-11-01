"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

import { ApiError, api } from "./api";
import {
  documentSchema,
  documentTextSchema,
  essayMutationSchema,
  essaySchema,
  fingerprintStatusSchema,
  memoryPageSchema,
  memorySchema,
  profileSchema,
  recallSchema,
  type DocKind,
  type Experience,
  type LanguageEntry,
  type PII,
  type ProfileDetails,
} from "./schemas";

export const vaultKeys = {
  profile: ["vault", "profile"] as const,
  documents: ["vault", "documents"] as const,
  document: (id: string) => ["vault", "documents", id] as const,
  essays: ["vault", "essays"] as const,
  fingerprint: ["vault", "fingerprint"] as const,
  memory: (q: string) => ["vault", "memory", q] as const,
};

// ---- profile ------------------------------------------------------------------------------------

export function useProfile() {
  return useQuery({
    queryKey: vaultKeys.profile,
    queryFn: async () => {
      try {
        return await api("/api/v1/profile", { schema: profileSchema });
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return null; // no profile yet
        throw error;
      }
    },
  });
}

export interface ProfileInput {
  full_name: string;
  pii: PII;
  degree_level: string | null;
  major: string | null;
  institution: string | null;
  gpa: number | null;
  gpa_scale: number | null;
  nationality: string | null;
  country_of_residence: string | null;
  graduation_year: number | null;
  skills: string[];
  languages: LanguageEntry[];
  experiences: Experience[];
  details: ProfileDetails;
}

export function useSaveProfile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ProfileInput) =>
      api("/api/v1/profile", { method: "PUT", body, schema: profileSchema }),
    onSuccess: (profile) => qc.setQueryData(vaultKeys.profile, profile),
  });
}

// ---- documents ----------------------------------------------------------------------------------

export function useDocuments() {
  return useQuery({
    queryKey: vaultKeys.documents,
    queryFn: () => api("/api/v1/documents", { schema: z.array(documentSchema) }),
  });
}

export function useDocumentText(id: string | null) {
  return useQuery({
    queryKey: vaultKeys.document(id ?? "none"),
    queryFn: () => api(`/api/v1/documents/${id}`, { schema: documentTextSchema }),
    enabled: id !== null,
  });
}

export function useUploadDocument() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ file, kind }: { file: File; kind: DocKind }) => {
      const form = new FormData();
      form.append("file", file);
      form.append("kind", kind);
      return api("/api/v1/documents", { method: "POST", body: form, schema: documentSchema });
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: vaultKeys.documents }),
  });
}

export function useDeleteDocument() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api(`/api/v1/documents/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: vaultKeys.documents }),
  });
}

// ---- essays -------------------------------------------------------------------------------------

export function useEssays() {
  return useQuery({
    queryKey: vaultKeys.essays,
    queryFn: () => api("/api/v1/essays", { schema: z.array(essaySchema) }),
  });
}

export function useFingerprint() {
  return useQuery({
    queryKey: vaultKeys.fingerprint,
    queryFn: () => api("/api/v1/essays/fingerprint", { schema: fingerprintStatusSchema }),
  });
}

interface EssayInput {
  title: string;
  body: string;
  prompt_context: string | null;
}

function useEssayInvalidate() {
  const qc = useQueryClient();
  return () =>
    Promise.all([
      qc.invalidateQueries({ queryKey: vaultKeys.essays }),
      qc.invalidateQueries({ queryKey: vaultKeys.fingerprint }),
    ]);
}

export function useSaveEssay() {
  const invalidate = useEssayInvalidate();
  return useMutation({
    mutationFn: ({ id, ...body }: EssayInput & { id?: string }) =>
      api(id ? `/api/v1/essays/${id}` : "/api/v1/essays", {
        method: id ? "PUT" : "POST",
        body,
        schema: essayMutationSchema,
      }),
    onSuccess: invalidate,
  });
}

export function useDeleteEssay() {
  const invalidate = useEssayInvalidate();
  return useMutation({
    mutationFn: (id: string) =>
      api(`/api/v1/essays/${id}`, { method: "DELETE", schema: essayMutationSchema }),
    onSuccess: invalidate,
  });
}

// ---- memory -------------------------------------------------------------------------------------

export function useMemory(search: string) {
  return useQuery({
    queryKey: vaultKeys.memory(search),
    queryFn: () =>
      api(`/api/v1/memory?limit=200${search ? `&q=${encodeURIComponent(search)}` : ""}`, {
        schema: memoryPageSchema,
      }),
  });
}

function useMemoryInvalidate() {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: ["vault", "memory"] });
}

export function useLearnMemory() {
  const invalidate = useMemoryInvalidate();
  return useMutation({
    mutationFn: (body: { question: string; answer: string }) =>
      api("/api/v1/memory", { method: "POST", body, schema: memorySchema }),
    onSuccess: invalidate,
  });
}

export function useUpdateMemory() {
  const invalidate = useMemoryInvalidate();
  return useMutation({
    mutationFn: ({ id, ...body }: { id: string; question?: string; answer?: string }) =>
      api(`/api/v1/memory/${id}`, { method: "PATCH", body, schema: memorySchema }),
    onSuccess: invalidate,
  });
}

export function useDeleteMemory() {
  const invalidate = useMemoryInvalidate();
  return useMutation({
    mutationFn: (id: string) => api(`/api/v1/memory/${id}`, { method: "DELETE" }),
    onSuccess: invalidate,
  });
}

export function useRecall() {
  return useMutation({
    mutationFn: (body: { question: string; k: number }) =>
      api("/api/v1/memory/recall", { method: "POST", body, schema: recallSchema }),
  });
}
