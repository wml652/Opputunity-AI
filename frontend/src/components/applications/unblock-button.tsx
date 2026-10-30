"use client";

import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api";
import { useUnblockApplication } from "@/lib/m4-queries";

/**
 * Opens a visible browser at the portal so the student signs in (or passes a challenge) by
 * themselves; the saved cookies let the filler resume. Automation never types a password.
 */
export function UnblockButton({ applicationId, variant = "primary" }: { applicationId: string; variant?: "primary" | "secondary" }) {
  const unblock = useUnblockApplication();
  const message = unblock.error
    ? unblock.error instanceof ApiError
      ? unblock.error.message
      : "Could not open the browser"
    : unblock.data
      ? `Browser opened at ${unblock.data.domain}: sign in there, then close it; filling resumes automatically`
      : null;
  return (
    <span className="inline-flex items-center gap-2">
      {message ? (
        <span className={unblock.error ? "max-w-72 truncate text-2xs text-err" : "max-w-72 truncate text-2xs text-warn"} title={message}>
          {message}
        </span>
      ) : null}
      <Button
        size="sm"
        variant={variant}
        onClick={() => unblock.mutate(applicationId)}
        disabled={unblock.isPending || unblock.isSuccess}
        title="Sign in to the portal in a visible browser; the saved session resumes this application"
      >
        {unblock.isPending ? "Opening browser" : unblock.isSuccess ? "Waiting for sign-in" : "Sign In in Browser & Resume"}
      </Button>
    </span>
  );
}
