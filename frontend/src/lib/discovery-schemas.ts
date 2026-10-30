import { z } from "zod";

export const outcomeEnum = z.enum(["ELIGIBLE", "INELIGIBLE", "EXPIRED", "UNCERTAIN"]);
export type Outcome = z.infer<typeof outcomeEnum>;
export const scopeEnum = z.enum(["LOCAL", "INTERNATIONAL", "BOTH"]);
export type Scope = z.infer<typeof scopeEnum>;

export const progressSchema = z.object({
  phase: z.string(),
  current: z.object({
    label: z.string(),
    detail: z.string().nullable(),
    done: z.number(),
    total: z.number(),
    pct: z.number(),
  }),
  overall_pct: z.number(),
  crawled_pct: z.number(),
  evaluated_pct: z.number(),
  staged_pct: z.number(),
  crawled: z.number(),
  evaluated: z.number(),
  found: z.number(),
  staged: z.number(),
  stage_target: z.number(),
  auto_stage: z.boolean(),
});
export type ProgressT = z.infer<typeof progressSchema>;

export const campaignSchema = z.object({
  id: z.string().uuid(),
  name: z.string(),
  scope: scopeEnum,
  keywords: z.array(z.string()),
  status: z.enum(["DRAFT", "RUNNING", "PAUSED", "COMPLETED", "FAILED", "CANCELLED"]),
  phase: z.string(),
  counters: z.record(z.number()),
  settings: z.record(z.unknown()),
  error: z.string().nullable(),
  created_at: z.string(),
  started_at: z.string().nullable(),
  finished_at: z.string().nullable(),
  sources: z.array(z.object({ id: z.string(), source_type: z.string(), url: z.string().nullable() })),
  progress: progressSchema.optional(),
});
export type CampaignT = z.infer<typeof campaignSchema>;

export const crawledPageSchema = z.object({
  id: z.string(),
  url: z.string(),
  final_url: z.string().nullable(),
  http_status: z.number().nullable(),
  status: z.string(),
  title: z.string().nullable(),
  injection_flags: z.array(z.record(z.string())),
  error: z.string().nullable(),
  markdown_chars: z.number(),
  fetched_at: z.string(),
  error_stage: z.string().nullable().optional(),
  error_code: z.string().nullable().optional(),
  discovery_source: z.string().nullable().optional(),
  discovery_query: z.string().nullable().optional(),
});
export type CrawledPageT = z.infer<typeof crawledPageSchema>;

export const campaignDetailSchema = z.object({
  campaign: campaignSchema,
  pages: z.array(crawledPageSchema),
});

export const opportunitySchema = z.object({
  id: z.string().uuid(),
  campaign_id: z.string().nullable(),
  type: z.string(),
  title: z.string(),
  organization: z.string().nullable(),
  location: z.string().nullable(),
  is_international: z.boolean().nullable(),
  deadline_at: z.string().nullable(),
  deadline_raw: z.string().nullable(),
  portal_url: z.string().nullable(),
  source_url: z.string().nullable(),
  summary: z.string(),
  eligibility_rules: z.record(z.unknown()),
  ai_policy: z.string(),
  latest_outcome: outcomeEnum.nullable(),
  created_at: z.string(),
});
export type OpportunityT = z.infer<typeof opportunitySchema>;

export const verdictSchema = z.object({
  outcome: outcomeEnum,
  reasons: z.array(
    z.object({ rule: z.string(), status: z.string(), evidence: z.string(), source: z.string() }),
  ),
  model: z.string().nullable(),
  evaluated_at: z.string(),
});

export const opportunityDetailSchema = z.object({
  opportunity: opportunitySchema,
  verdict: verdictSchema.nullable(),
});

export const queueItemSchema = z.object({
  application_id: z.string(),
  state: z.string(),
  priority_rank: z.number().nullable(),
  deadline_at: z.string().nullable(),
  opportunity: opportunitySchema,
});
export type QueueItem = z.infer<typeof queueItemSchema>;

export const pipelineSummarySchema = z.object({
  websites_crawled: z.number(),
  opportunities_found: z.number(),
  eligible: z.number(),
  needs_input: z.number(),
  ready_for_ok: z.number(),
  submitted: z.number(),
  queued: z.number(),
  latest_campaign: campaignSchema.nullable(),
});
export type PipelineSummaryT = z.infer<typeof pipelineSummarySchema>;
