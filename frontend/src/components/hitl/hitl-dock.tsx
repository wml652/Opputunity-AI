"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { useCloseHitlSession, useHitlSessions } from "@/lib/hitl-queries";

import { RemoteBrowser } from "./remote-browser";

/**
 * Remote browser sessions on the server (Teach Mode, portal sign-in, verification challenges).
 * A new session opens the viewer automatically; closed ones can be reopened from the strip.
 */
export function HitlDock() {
  const { data: sessions = [] } = useHitlSessions();
  const close = useCloseHitlSession();
  const [openId, setOpenId] = useState<string | null>(null);
  const seen = useRef<Set<string>>(new Set());

  useEffect(() => {
    const fresh = sessions.find((s) => !seen.current.has(s.id));
    for (const s of sessions) seen.current.add(s.id);
    if (fresh) setOpenId(fresh.id);
    if (openId && !sessions.some((s) => s.id === openId)) setOpenId(null);
  }, [sessions, openId]);

  if (sessions.length === 0) return null;
  const active = sessions.find((s) => s.id === openId) ?? null;
  return (
    <>
      <div className="flex items-center gap-2 border-b border-line bg-warn-soft px-3 py-1">
        <span className="text-2xs font-medium uppercase tracking-wide text-warn">Your action needed</span>
        {sessions.map((s) => (
          <Button key={s.id} size="sm" onClick={() => setOpenId(s.id)} title={s.url}>
            {s.kind === "TEACH" ? "Teach" : "Sign in"}: {s.title.slice(0, 40)}
          </Button>
        ))}
      </div>
      <Dialog.Root open={active !== null} onOpenChange={(o) => !o && setOpenId(null)}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-40 bg-black/30" />
          <Dialog.Content
            className="fixed inset-4 z-50 flex flex-col overflow-hidden rounded-sm border border-line bg-surface"
            aria-describedby={undefined}
          >
            <Dialog.Title className="sr-only">Remote browser</Dialog.Title>
            {active ? (
              <RemoteBrowser
                session={active}
                onClose={() => {
                  close.mutate(active.id);
                  setOpenId(null);
                }}
              />
            ) : null}
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </>
  );
}
