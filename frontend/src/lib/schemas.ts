import { z } from "zod";

export const providerKind = z.enum(["NVIDIA_NIM", "OPENROUTER", "CUSTOM"]);
export type ProviderKind = z.infer<typeof providerKind>;

export const userSchema = z.object({
  id: z.string().uuid(),
  email: z.string(),
  display_name: z.string(),
  timezone: z.string(),
});
export type User = z.infer<typeof userSchema>;

export const sessionSchema = z.object({
  user: userSchema,
  access_expires_at: z.string(),
  csrf_token: z.string(),
});

/** Presets only pre-fill the base URL; models are always entered or picked by the user. */
export const presetSchema = z.object({
  kind: providerKind,
  label: z.string(),
  base_url: z.string().nullable(),
});

export const modelInfoSchema = z.object({
  id: z.string(),
  category: z.string(),
  publisher: z.string(),
  chat_capable: z.boolean(),
});
export type ModelInfo = z.infer<typeof modelInfoSchema>;
export const fetchModelsSchema = z.object({
  base_url: z.string(),
  models: z.array(z.string()),
  catalog: z.array(modelInfoSchema).optional(),
  counts: z.record(z.number()).optional(),
});
export type Preset = z.infer<typeof presetSchema>;

export const connectionCheckSchema = z.object({
  ok: z.boolean(),
  status: z.enum(["Connection Established", "Connection Failed"]),
  latency_ms: z.number().int().nullable(),
  model: z.string(),
  model_listed: z.boolean().nullable(),
  error_code: z.string().nullable(),
  error_message: z.string().nullable(),
  checked_at: z.string(),
});
export type ConnectionCheck = z.infer<typeof connectionCheckSchema>;

export const providerConfigSchema = z.object({
  id: z.string().uuid(),
  kind: providerKind,
  label: z.string(),
  base_url: z.string(),
  model: z.string(),
  api_key_last4: z.string(),
  embedding_base_url: z.string().nullable(),
  embedding_model: z.string().nullable(),
  extraction_model: z.string().nullable().optional(),
  rerank_model: z.string().nullable().optional(),
  rerank_base_url: z.string().nullable().optional(),
  vision_model: z.string().nullable().optional(),
  rpm_limit: z.number().int().positive().optional(),
  has_embedding_key: z.boolean(),
  effective_embedding_model: z.string(),
  is_active: z.boolean(),
  last_check: connectionCheckSchema.nullable(),
});
export type ProviderConfig = z.infer<typeof providerConfigSchema>;

export const verifyResponseSchema = z.object({
  provider: providerConfigSchema,
  check: connectionCheckSchema,
});

export const connectionStatusSchema = z.object({
  configured: z.boolean(),
  provider: providerConfigSchema.nullable(),
});
export type ConnectionStatus = z.infer<typeof connectionStatusSchema>;

export const workerStatusSchema = z.object({
  active: z.number().int(),
  workers: z.array(
    z.object({
      worker_id: z.string(),
      queue: z.string().nullable().optional(),
      host: z.string().nullable().optional(),
      pid: z.number().nullable().optional(),
      seen_at: z.number().nullable().optional(),
    }),
  ),
});
export type WorkerStatus = z.infer<typeof workerStatusSchema>;

export const inboxSummarySchema = z.object({
  open_questions: z.number().int(),
  awaiting_verification: z.number().int(),
  total: z.number().int(),
});

export const pipelineEventSchema = z.object({
  seq: z.number().int(),
  type: z.string(),
  payload: z.record(z.unknown()),
  campaign_id: z.string().nullable(),
  application_id: z.string().nullable(),
  created_at: z.string(),
});
export type PipelineEvent = z.infer<typeof pipelineEventSchema>;

export const llmStatusPayloadSchema = z.object({
  provider_config_id: z.string(),
  kind: providerKind,
  label: z.string(),
  model: z.string(),
  ok: z.boolean().nullable(),
  latency_ms: z.number().nullable(),
  error_code: z.string().nullable(),
  checked_at: z.string().nullable(),
});
export type LlmStatusPayload = z.infer<typeof llmStatusPayloadSchema>;

export const promptTestSchema = z.object({
  provider_config_id: z.string().uuid(),
  model: z.string(),
  text: z.string(),
  reasoning: z.string().nullable(),
  finish_reason: z.string().nullable(),
  latency_ms: z.number().int(),
  prompt_tokens: z.number().int().nullable(),
  completion_tokens: z.number().int().nullable(),
});
export type PromptTest = z.infer<typeof promptTestSchema>;

// ---- vault -------------------------------------------------------------------------------------

export const degreeLevel = z.enum(["HIGH_SCHOOL", "BACHELOR", "MASTER", "PHD", "OTHER"]);
export const docKind = z.enum(["CV", "TRANSCRIPT", "ID", "CERTIFICATE", "OTHER"]);
export type DocKind = z.infer<typeof docKind>;

export const refereeSchema = z.object({
  name: z.string().nullable(),
  title: z.string().nullable(),
  department: z.string().nullable(),
  organization: z.string().nullable(),
  email: z.string().nullable(),
  phone: z.string().nullable(),
  relationship: z.string().nullable(),
});
export type Referee = z.infer<typeof refereeSchema>;

export const piiSchema = z.object({
  home_address: z.string().nullable().optional(),
  phone: z.string().nullable().optional(),
  date_of_birth: z.string().nullable().optional(),
  national_id: z.string().nullable().optional(),
  passport_number: z.string().nullable().optional(),
  city: z.string().nullable().optional(),
  state_or_province: z.string().nullable().optional(),
  postal_code: z.string().nullable().optional(),
  gender: z.string().nullable().optional(),
  marital_status: z.string().nullable().optional(),
  linkedin_url: z.string().nullable().optional(),
  github_url: z.string().nullable().optional(),
  portfolio_url: z.string().nullable().optional(),
  orcid_or_scholar_url: z.string().nullable().optional(),
  emergency_contact: z
    .object({ name: z.string().nullable(), phone: z.string().nullable(), relationship: z.string().nullable() })
    .nullable()
    .optional(),
  referees: z.array(refereeSchema).optional(),
});
export type PII = z.infer<typeof piiSchema>;

export const englishTypes = ["IELTS", "TOEFL", "DUOLINGO", "PTE", "MOI_EXEMPT", "NATIVE", "NONE"] as const;
export const workModes = ["REMOTE", "HYBRID", "ONSITE", "ANY"] as const;
export const profileDetailsSchema = z.object({
  current_semester_or_year: z.string().nullable(),
  cgpa_percentage: z.number().nullable(),
  secondary_school_qualification: z.string().nullable(),
  secondary_school_percentage: z.number().nullable(),
  minor_or_specialization: z.string().nullable(),
  thesis_or_fyp_title: z.string().nullable(),
  academic_honors: z.array(z.string()),
  english_proficiency_type: z.enum(englishTypes).nullable(),
  english_proficiency_score: z.string().nullable(),
  standardized_tests: z.record(z.string()),
  publications_count: z.number().int().nullable(),
  research_interests: z.array(z.string()),
  preferred_work_mode: z.enum(workModes).nullable(),
  earliest_start_date: z.string().nullable(),
  requires_visa_sponsorship: z.boolean().nullable(),
  has_valid_passport: z.boolean().nullable(),
  financial_need_eligible: z.boolean().nullable(),
  first_generation_student: z.boolean().nullable(),
});
export type ProfileDetails = z.infer<typeof profileDetailsSchema>;

export const languageSchema = z.object({ name: z.string(), level: z.string() });
export const experienceSchema = z.object({
  title: z.string(),
  organization: z.string(),
  start: z.string().nullable(),
  end: z.string().nullable(),
  description: z.string(),
});

export const profileSchema = z.object({
  id: z.string().uuid(),
  full_name: z.string(),
  pii: piiSchema,
  degree_level: degreeLevel.nullable(),
  major: z.string().nullable(),
  institution: z.string().nullable(),
  gpa: z.number().nullable(),
  gpa_scale: z.number().nullable(),
  gpa_normalized_4: z.number().nullable(),
  nationality: z.string().nullable(),
  country_of_residence: z.string().nullable(),
  graduation_year: z.number().int().nullable(),
  skills: z.array(z.string()),
  languages: z.array(languageSchema),
  experiences: z.array(experienceSchema),
  details: profileDetailsSchema.optional(),
  pii_encrypted: z.boolean(),
  pii_key_version: z.number().int().nullable(),
  updated_at: z.string().nullable(),
});
export type Profile = z.infer<typeof profileSchema>;
export type Experience = z.infer<typeof experienceSchema>;
export type LanguageEntry = z.infer<typeof languageSchema>;

export const documentSchema = z.object({
  id: z.string().uuid(),
  kind: docKind,
  filename: z.string(),
  mime: z.string(),
  size_bytes: z.number().int(),
  sha256: z.string(),
  page_count: z.number().int().nullable(),
  text_chars: z.number().int(),
  created_at: z.string(),
});
export type VaultDocument = z.infer<typeof documentSchema>;

export const documentTextSchema = z.object({
  document: documentSchema,
  text: z.string(),
  truncated: z.boolean(),
});

export const essaySchema = z.object({
  id: z.string().uuid(),
  title: z.string(),
  body: z.string(),
  prompt_context: z.string().nullable(),
  word_count: z.number().int(),
  created_at: z.string(),
  updated_at: z.string(),
});
export type Essay = z.infer<typeof essaySchema>;

const countItem = z.object({ count: z.number() }).passthrough();
export const fingerprintMetricsSchema = z
  .object({
    word_count: z.number(),
    sentence_count: z.number(),
    paragraph_count: z.number(),
    sentence_length: z.object({
      mean: z.number(),
      std: z.number(),
      burstiness: z.number(),
      min: z.number(),
      max: z.number(),
      median: z.number(),
      histogram: z.array(z.object({ bin: z.string(), count: z.number() })),
    }),
    lexical: z.object({
      ttr: z.number(),
      mtld: z.number(),
      unique_words: z.number(),
      avg_word_length: z.number(),
    }),
    readability: z.object({
      flesch_reading_ease: z.number(),
      flesch_kincaid_grade: z.number(),
      syllables_per_word: z.number(),
    }),
    paragraphs: z.object({ words_mean: z.number(), words_std: z.number(), sentences_mean: z.number() }),
    punctuation_per_1k: z.record(z.number()),
    function_words_per_1k: z.record(z.number()),
    transitions: z.object({
      per_1k: z.number(),
      top: z.array(z.object({ phrase: z.string(), count: z.number(), per_1k: z.number() })),
      openers: z.array(countItem.extend({ word: z.string() })),
    }),
  })
  .passthrough();
export type FingerprintMetrics = z.infer<typeof fingerprintMetricsSchema>;

export const fingerprintSchema = z.object({
  version: z.number().int(),
  essay_count: z.number().int(),
  total_words: z.number().int(),
  metrics: fingerprintMetricsSchema,
  feature_names: z.array(z.string()),
  feature_vector: z.array(z.number()),
  created_at: z.string().nullable(),
});

export const fingerprintStatusSchema = z.object({
  fingerprint: fingerprintSchema.nullable(),
  essay_count: z.number().int(),
  chunk_count: z.number().int(),
  max_essays: z.number().int(),
  recommended_essays: z.number().int(),
});
export type FingerprintStatus = z.infer<typeof fingerprintStatusSchema>;

export const essayMutationSchema = z.object({
  essay: essaySchema.nullable(),
  fingerprint: fingerprintSchema.nullable(),
});

export const memorySchema = z.object({
  id: z.string().uuid(),
  question: z.string(),
  answer: z.string(),
  source: z.enum(["USER", "PROFILE", "INFERRED"]),
  embedding_model: z.string(),
  times_reused: z.number().int(),
  last_used_at: z.string().nullable(),
  created_at: z.string(),
  updated_at: z.string(),
});
export type MemoryEntry = z.infer<typeof memorySchema>;

export const memoryPageSchema = z.object({ items: z.array(memorySchema), total: z.number().int() });

const recallMatchSchema = z.object({
  memory: memorySchema,
  similarity: z.number(),
  auto_fill: z.boolean(),
});
export const recallSchema = z.object({
  query: z.string(),
  embedding_model: z.string(),
  threshold: z.number(),
  auto_fill: z.boolean(),
  best: recallMatchSchema.nullable(),
  matches: z.array(recallMatchSchema),
  searched: z.number().int(),
  stale: z.number().int(),
});
export type RecallResult = z.infer<typeof recallSchema>;
