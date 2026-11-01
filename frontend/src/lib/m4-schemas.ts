import { z } from "zod";

import { crawledPageSchema, opportunitySchema } from "./discovery-schemas";

// ---- Crawl inspector (flight recorder) -------------------------------------------------------

export const traceStepSchema = z.object({
  stage: z.string(),
  status: z.string(),
  started_at: z.union([z.string(), z.number()]).nullable().optional(),
  duration_ms: z.number().nullable().optional(),
  detail: z.record(z.unknown()).optional(),
});
export type TraceStep = z.infer<typeof traceStepSchema>;

export const pageDetailSchema = crawledPageSchema.extend({
  error_stage: z.string().nullable(),
  error_code: z.string().nullable(),
  discovery_source: z.string().nullable(),
  discovery_query: z.string().nullable(),
  trace: z.array(traceStepSchema),
  error_detail: z.string().nullable(),
  strategy: z.record(z.unknown()),
  markdown_preview: z.string(),
  log_file: z.string().nullable(),
});
export type PageDetailT = z.infer<typeof pageDetailSchema>;

// ---- Applications ----------------------------------------------------------------------------

export const applicationSchema = z.object({
  id: z.string(),
  state: z.string(),
  priority_rank: z.number().nullable(),
  deadline_at: z.string().nullable(),
  version: z.number(),
  // "[GATE_CODE] reason" when blocked: sign-in wall, email-only, closed, aggregator...
  last_error: z.string().nullable().optional(),
});

export const transitionSchema = z.object({
  from_state: z.string().nullable(),
  to_state: z.string(),
  actor: z.string(),
  reason: z.string(),
  at: z.string(),
});

export const fieldSchema = z.object({
  field_key: z.string(),
  step_index: z.number(),
  label: z.string(),
  field_type: z.string(),
  options: z.array(z.string()),
  required: z.boolean(),
  value: z.string().nullable(),
  source: z.string(),
  confidence: z.number(),
  essay_draft_id: z.string().nullable(),
  user_edited: z.boolean(),
  // PII-redacted CV / essay / memory snippet that grounded an auto-filled answer.
  evidence: z.string().nullable().optional(),
});
export type FieldT = z.infer<typeof fieldSchema>;

export const questionSchema = z.object({
  id: z.string(),
  application_id: z.string(),
  field_key: z.string(),
  step_index: z.number(),
  question: z.string(),
  context: z.string(),
  field_type: z.string(),
  options: z.array(z.string()),
  suggested_answer: z.string().nullable(),
  confidence: z.number(),
  status: z.string(),
  created_at: z.string(),
  answered_at: z.string().nullable(),
});

export const draftSchema = z.object({
  id: z.string(),
  application_id: z.string().nullable(),
  field_key: z.string().nullable(),
  prompt: z.string(),
  word_limit: z.number().nullable(),
  body: z.string(),
  critique: z.record(z.unknown()),
  voice_match_score: z.number().nullable(),
  burstiness_score: z.number().nullable(),
  status: z.string(),
  requires_manual: z.boolean(),
  model: z.string().nullable(),
  created_at: z.string().nullable(),
});
export type DraftT = z.infer<typeof draftSchema>;

export const checkpointSchema = z.object({
  id: z.string(),
  seq: z.number(),
  step_index: z.number(),
  url: z.string(),
  has_screenshot: z.boolean(),
  fields_filled: z.number(),
  created_at: z.string(),
});

export const applicationDetailSchema = z.object({
  application: applicationSchema,
  opportunity: opportunitySchema,
  transitions: z.array(transitionSchema),
  fields: z.array(fieldSchema),
  questions: z.array(questionSchema),
  drafts: z.array(draftSchema),
  checkpoints: z.array(checkpointSchema),
});
export type ApplicationDetailT = z.infer<typeof applicationDetailSchema>;

// ---- Inbox -----------------------------------------------------------------------------------

export const inboxQuestionSchema = questionSchema.extend({
  opportunity_title: z.string(),
  organization: z.string().nullable(),
  portal_url: z.string().nullable(),
});
export type InboxQuestionT = z.infer<typeof inboxQuestionSchema>;

const inboxAppSchema = z.object({ application: applicationSchema, opportunity: opportunitySchema });
export type InboxAppT = z.infer<typeof inboxAppSchema>;

export const inboxSchema = z.object({
  questions: z.array(inboxQuestionSchema),
  awaiting_verification: z.array(inboxAppSchema),
  blocked: z.array(inboxAppSchema),
});

export const answerSchema = z.object({
  application_id: z.string(),
  remaining_questions: z.number(),
  resumed: z.boolean(),
  memory_qa_id: z.string().nullable(),
});

// ---- Notifications ---------------------------------------------------------------------------

export const channelSchema = z.object({
  id: z.string(),
  channel: z.enum(["EMAIL", "WHATSAPP"]),
  address_hint: z.string(),
  enabled: z.boolean(),
  verified: z.boolean(),
  created_at: z.string(),
});
export type ChannelT = z.infer<typeof channelSchema>;

export const notificationSchema = z.object({
  channel: z.string(),
  template: z.string(),
  status: z.string(),
  provider: z.string().nullable(),
  error: z.string().nullable(),
  created_at: z.string(),
});

export const notificationSettingsSchema = z.object({
  channels: z.array(channelSchema),
  providers: z.record(z.string()),
  recent: z.array(notificationSchema),
});

export const deliverySchema = z.object({
  channel: z.string(),
  status: z.string(),
  provider: z.string().nullable(),
  error: z.string().nullable(),
});

// ---- Final verification gate (M5) -------------------------------------------------------------

export const verificationRecordSchema = z.object({
  id: z.string(),
  outcome: z.string(),
  preview_hash_seen: z.string(),
  approved_preview_hash: z.string().nullable(),
  observed_hash: z.string().nullable(),
  edited_fields: z.array(z.string()),
  mismatches: z.array(z.record(z.string())),
  approved_at: z.string().nullable(),
  rejected_at: z.string().nullable(),
  reject_reason: z.string().nullable(),
  submitted_at: z.string().nullable(),
  final_url: z.string().nullable(),
  error: z.string().nullable(),
  has_receipt: z.boolean(),
  created_at: z.string(),
});
export type VerificationRecordT = z.infer<typeof verificationRecordSchema>;

export const verificationSchema = z.object({
  application: applicationSchema,
  opportunity: opportunitySchema,
  fields: z.array(fieldSchema),
  drafts: z.array(draftSchema),
  checkpoint: checkpointSchema.nullable(),
  preview_hash: z.string(),
  latest: verificationRecordSchema.nullable(),
  receipt_text: z.string().nullable(),
});
export type VerificationT = z.infer<typeof verificationSchema>;
