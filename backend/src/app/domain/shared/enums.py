"""Persisted enum vocabulary for all subsystems. Created as PostgreSQL ENUM types in the baseline
migration so later milestones only add tables, not types."""

from __future__ import annotations

import enum


class Scope(enum.StrEnum):
    LOCAL = "LOCAL"
    INTERNATIONAL = "INTERNATIONAL"
    BOTH = "BOTH"


class SourceType(enum.StrEnum):
    WEBSITE = "WEBSITE"
    WEB_SEARCH = "WEB_SEARCH"
    LINKEDIN_PUBLIC = "LINKEDIN_PUBLIC"
    LINKEDIN_AUTH = "LINKEDIN_AUTH"


class CampaignStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class CrawlStatus(enum.StrEnum):
    PENDING = "PENDING"
    FETCHING = "FETCHING"
    DISTILLED = "DISTILLED"
    EXTRACTED = "EXTRACTED"
    BLOCKED_SSRF = "BLOCKED_SSRF"
    FAILED = "FAILED"


class OpportunityType(enum.StrEnum):
    SCHOLARSHIP = "SCHOLARSHIP"
    INTERNSHIP = "INTERNSHIP"
    RESEARCH = "RESEARCH"
    REMOTE_JOB = "REMOTE_JOB"
    OTHER = "OTHER"


class EligibilityOutcome(enum.StrEnum):
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    EXPIRED = "EXPIRED"
    UNCERTAIN = "UNCERTAIN"


class ApplicationState(enum.StrEnum):
    DISCOVERED = "DISCOVERED"
    ELIGIBILITY_PENDING = "ELIGIBILITY_PENDING"
    INELIGIBLE = "INELIGIBLE"
    EXPIRED = "EXPIRED"
    ELIGIBLE = "ELIGIBLE"
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    FILLING = "FILLING"
    PAUSED_NEEDS_USER_INPUT = "PAUSED_NEEDS_USER_INPUT"
    BLOCKED_NEEDS_HUMAN = "BLOCKED_NEEDS_HUMAN"
    RESUME_QUEUED = "RESUME_QUEUED"
    AWAITING_FINAL_VERIFICATION = "AWAITING_FINAL_VERIFICATION"
    SUBMIT_APPROVED = "SUBMIT_APPROVED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    SUBMISSION_FAILED = "SUBMISSION_FAILED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class QuestionStatus(enum.StrEnum):
    OPEN = "OPEN"
    ANSWERED = "ANSWERED"
    DISMISSED = "DISMISSED"


class DocKind(enum.StrEnum):
    CV = "CV"
    TRANSCRIPT = "TRANSCRIPT"
    ID = "ID"
    CERTIFICATE = "CERTIFICATE"
    OTHER = "OTHER"


class NotifChannel(enum.StrEnum):
    EMAIL = "EMAIL"
    WHATSAPP = "WHATSAPP"


class NotifStatus(enum.StrEnum):
    QUEUED = "QUEUED"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"


class EssayStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    CRITIQUED = "CRITIQUED"
    USER_EDITED = "USER_EDITED"
    APPROVED = "APPROVED"


class Actor(enum.StrEnum):
    USER = "USER"
    WORKER = "WORKER"
    SYSTEM = "SYSTEM"
