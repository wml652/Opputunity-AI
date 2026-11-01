"use client";

const GATE_LABEL: Record<string, string> = {
  PORTAL_LOGIN_OR_REGISTER_REQUIRED: "Sign-in required",
  EMAIL_OR_PACKAGE_SUBMISSION_ONLY: "Apply by email",
  APPLICATIONS_NOT_OPEN_OR_CLOSED: "Applications closed",
  AGGREGATOR_WITHOUT_OFFICIAL_LINK: "Aggregator page",
  BOT_CHALLENGE_NEEDS_HUMAN: "Bot check",
  NO_APPLICATION_FORM: "No form found",
};

/** Splits "[GATE] reason" into a compact amber gate tag and the human-readable reason. */
export function parseBlockedReason(raw: string | null | undefined): { gate: string | null; text: string } {
  if (!raw) return { gate: null, text: "" };
  const m = /^\[([A-Z_]+)\]\s*(.*)$/s.exec(raw);
  return m ? { gate: m[1] ?? null, text: m[2] ?? "" } : { gate: null, text: raw };
}

export function BlockedReason({ reason, className }: { reason: string | null | undefined; className?: string }) {
  const { gate, text } = parseBlockedReason(reason);
  if (!text && !gate) return null;
  return (
    <div className={className}>
      {gate ? (
        <span className="mr-1.5 inline-flex h-4 items-center rounded-xs border border-warn/40 px-1 font-mono text-[10px] text-warn">
          {GATE_LABEL[gate] ?? gate}
        </span>
      ) : null}
      <span className="text-2xs text-muted" title={text}>
        {text}
      </span>
    </div>
  );
}
