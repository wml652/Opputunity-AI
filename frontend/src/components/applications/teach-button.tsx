"use client";

import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api";
import { useFinishTeach, useTeach } from "@/lib/m4-queries";

/**
 * "Train Agent on This Portal (Teach Mode)": a visible browser opens at the portal; the agent
 * records your clicks and answers (never passwords; personal values only as profile paths) and
 * saves them to its brain when you press "Finish training" (here or in the browser window).
 */
export function TeachButton({ applicationId, compact = false }: { applicationId: string; compact?: boolean }) {
  const teach = useTeach();
  const finish = useFinishTeach();
  const error = teach.error ?? finish.error;
  if (teach.isSuccess && !finish.isSuccess) {
    return (
      <span className="inline-flex items-center gap-1">
        <span className="text-2xs text-warn">Teaching - apply in the opened browser</span>
        <Button size="sm" variant="primary" onClick={() => finish.mutate(applicationId)} disabled={finish.isPending}>
          Finish training &amp; save
        </Button>
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1">
      {error ? (
        <span className="max-w-60 truncate text-2xs text-err" title={error instanceof ApiError ? error.message : undefined}>
          {error instanceof ApiError ? error.message : "Teach Mode failed"}
        </span>
      ) : null}
      {finish.isSuccess ? <span className="text-2xs text-ok">Saved to the agent brain</span> : null}
      <Button size="sm" onClick={() => teach.mutate(applicationId)} disabled={teach.isPending} title="Open the portal in a visible browser and teach the agent">
        {teach.isPending ? "Opening" : compact ? "Teach" : "Train Agent (Teach Mode)"}
      </Button>
    </span>
  );
}
