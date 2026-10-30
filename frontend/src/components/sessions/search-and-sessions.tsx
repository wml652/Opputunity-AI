"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/field";
import { EmptyRow, Panel, td, th } from "@/components/ui/panel";
import { StatusPill } from "@/components/ui/status";
import { cn, formatRelative } from "@/lib/cn";
import {
  useBrowserSessions,
  useCaptureSession,
  useDeleteSearchKey,
  useDeleteSession,
  useSaveSearchKey,
  useSearchKeys,
} from "@/lib/session-queries";

const SEARCH_PROVIDERS = [
  { id: "tavily", label: "Tavily", hint: "tvly-..." },
  { id: "brave", label: "Brave Search", hint: "BSA..." },
];

export function SearchAndSessions() {
  return (
    <div className="flex flex-col gap-3">
      <SearchKeys />
      <ChallengedDomains />
      <LinkedInSession />
    </div>
  );
}

function SearchKeys() {
  const { data: keys } = useSearchKeys();
  const save = useSaveSearchKey();
  const remove = useDeleteSearchKey();
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  return (
    <Panel title="Search engines" meta="your keys first, then the zero-key fallback">
      <table className="w-full table-fixed border-collapse">
        <thead>
          <tr>
            <th className={cn(th, "w-40")}>Provider</th>
            <th className={th}>API key</th>
            <th className={cn(th, "w-56")} />
          </tr>
        </thead>
        <tbody>
          {SEARCH_PROVIDERS.map((p) => {
            const saved = keys?.find((k) => k.provider === p.id);
            const draft = drafts[p.id] ?? "";
            return (
              <tr key={p.id}>
                <td className={cn(td, "text-xs font-medium")}>{p.label}</td>
                <td className={td}>
                  {saved ? (
                    <span className="font-mono text-2xs text-muted">
                      ••••{saved.key_last4} · saved {formatRelative(saved.updated_at)}
                    </span>
                  ) : (
                    <Input
                      type="password"
                      aria-label={`${p.label} API key`}
                      value={draft}
                      onChange={(e) => setDrafts({ ...drafts, [p.id]: e.target.value })}
                      placeholder={p.hint}
                      className="h-6 font-mono text-xs"
                    />
                  )}
                </td>
                <td className={cn(td, "text-right")}>
                  {saved ? (
                    <Button size="sm" variant="ghost" onClick={() => remove.mutate(p.id)} disabled={remove.isPending}>
                      Remove
                    </Button>
                  ) : (
                    <Button
                      size="sm"
                      variant="primary"
                      disabled={draft.trim().length < 8 || save.isPending}
                      onClick={() =>
                        save.mutate(
                          { provider: p.id, api_key: draft.trim() },
                          { onSuccess: () => setDrafts({ ...drafts, [p.id]: "" }) },
                        )
                      }
                    >
                      Save key
                    </Button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="border-t border-line px-3 py-2 text-2xs text-faint">
        Keys are stored encrypted and only the last four characters are ever shown. Without keys,
        discovery uses DuckDuckGo-backed search, which is rate-limited and sometimes returns
        nothing.
      </p>
      {save.error ? <p className="px-3 pb-2 text-xs text-err">{save.error.message}</p> : null}
    </Panel>
  );
}

function ChallengedDomains() {
  const { data } = useBrowserSessions();
  const capture = useCaptureSession();
  const remove = useDeleteSession();
  const challengeSessions = data?.sessions.filter((s) => s.kind === "CHALLENGE") ?? [];
  return (
    <Panel
      title="Bot-protected sites"
      meta="automation never solves challenges: you pass them once"
    >
      {data && !data.headful_available ? (
        <p className="border-b border-line bg-warn-soft px-3 py-1.5 text-xs text-warn">
          Solving challenges needs a visible browser on your own computer, so it is only available
          when the app runs locally.
        </p>
      ) : null}
      <table className="w-full table-fixed border-collapse">
        <thead>
          <tr>
            <th className={th}>Domain</th>
            <th className={cn(th, "w-32")}>Protection</th>
            <th className={cn(th, "w-40")}>Status</th>
            <th className={cn(th, "w-56")} />
          </tr>
        </thead>
        <tbody>
          {!data?.challenged.length && !challengeSessions.length ? (
            <EmptyRow colSpan={4}>No site has shown a bot challenge yet.</EmptyRow>
          ) : null}
          {data?.challenged.map((c) => (
            <tr key={c.domain}>
              <td className={cn(td, "font-mono text-xs")}>{c.domain}</td>
              <td className={cn(td, "text-xs text-muted")}>{c.vendor}</td>
              <td className={td}>
                <StatusPill tone="warn">skipped by discovery</StatusPill>
              </td>
              <td className={cn(td, "text-right")}>
                <Button
                  size="sm"
                  variant="primary"
                  disabled={!data.headful_available || capture.isPending || !c.url}
                  onClick={() => c.url && capture.mutate({ url: c.url })}
                  title="Opens a browser window on this computer; complete the check there"
                >
                  Solve challenge in browser
                </Button>
              </td>
            </tr>
          ))}
          {challengeSessions.map((s) => (
            <tr key={s.id}>
              <td className={cn(td, "font-mono text-xs")}>{s.domain}</td>
              <td className={cn(td, "text-xs text-muted")}>session saved</td>
              <td className={td}>
                <StatusPill tone="ok">crawled with your session</StatusPill>
                <div className="font-mono text-[10px] text-faint">
                  {s.expires_at ? `expires ${new Date(s.expires_at).toLocaleDateString()}` : ""}
                </div>
              </td>
              <td className={cn(td, "text-right")}>
                <Button size="sm" variant="ghost" onClick={() => remove.mutate(s.id)} disabled={remove.isPending}>
                  Forget
                </Button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {capture.data ? <p className="border-t border-line px-3 py-2 text-xs text-muted">{capture.data.message}</p> : null}
      {capture.error ? <p className="border-t border-line px-3 py-2 text-xs text-err">{capture.error.message}</p> : null}
    </Panel>
  );
}

function LinkedInSession() {
  const { data } = useBrowserSessions();
  const capture = useCaptureSession();
  const remove = useDeleteSession();
  const [ack, setAck] = useState(false);
  const [cap, setCap] = useState("25");
  const session = data?.sessions.find((s) => s.kind === "LINKEDIN");
  return (
    <Panel title="LinkedIn (opt-in)" meta="authenticated session, daily action cap">
      {session ? (
        <div className="flex items-center justify-between gap-3 px-3 py-3">
          <div className="text-xs">
            <StatusPill tone="ok">signed in</StatusPill>
            <span className="ml-2 font-mono text-2xs text-muted">
              {session.actions_today}/{session.daily_action_cap ?? "-"} actions today · ToS acknowledged{" "}
              {session.tos_acknowledged_at ? formatRelative(session.tos_acknowledged_at) : ""}
            </span>
          </div>
          <Button size="sm" variant="danger" onClick={() => remove.mutate(session.id)} disabled={remove.isPending}>
            Disconnect
          </Button>
        </div>
      ) : (
        <div className="flex flex-col gap-3 p-3">
          <p className="text-xs text-muted">
            Off by default. When enabled, discovery may read LinkedIn pages while signed in as you
            (never posting, messaging or applying). Automated activity can breach LinkedIn&apos;s
            User Agreement and may get your account restricted; a strict daily cap limits it.
          </p>
          <label className="flex items-start gap-2 text-xs">
            <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} className="mt-0.5" />
            I understand and accept this risk for my own LinkedIn account.
          </label>
          <div className="flex items-end gap-2">
            <Field id="li-cap" label="Daily action cap" className="w-40">
              <Input
                id="li-cap"
                inputMode="numeric"
                value={cap}
                onChange={(e) => setCap(e.target.value.replace(/\D/g, "").slice(0, 3))}
              />
            </Field>
            <Button
              variant="primary"
              disabled={!ack || !data?.headful_available || capture.isPending || !(Number(cap) >= 1 && Number(cap) <= 100)}
              onClick={() =>
                capture.mutate({ url: "", kind: "LINKEDIN", tos_acknowledged: true, daily_action_cap: Number(cap) })
              }
            >
              Sign in to LinkedIn in browser
            </Button>
          </div>
          {capture.error ? <p className="text-xs text-err">{capture.error.message}</p> : null}
          {capture.data ? <p className="text-xs text-muted">{capture.data.message}</p> : null}
        </div>
      )}
    </Panel>
  );
}
