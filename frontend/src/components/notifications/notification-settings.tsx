"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/field";
import { EmptyRow, Panel, td, th } from "@/components/ui/panel";
import { Select } from "@/components/ui/select";
import { StatusPill, type Tone } from "@/components/ui/status";
import { cn, formatRelative } from "@/lib/cn";
import {
  useDeleteChannel,
  useNotificationSettings,
  useSaveChannel,
  useTestNotification,
  useVerifyChannel,
} from "@/lib/m4-queries";
import type { ChannelT } from "@/lib/m4-schemas";

const STATUS_TONE: Record<string, Tone> = { SENT: "ok", DELIVERED: "ok", QUEUED: "idle", FAILED: "err" };

const CHANNELS = [
  { value: "EMAIL", label: "Email" },
  { value: "WHATSAPP", label: "WhatsApp" },
];

export function NotificationSettings() {
  const { data } = useNotificationSettings();
  const save = useSaveChannel();
  const test = useTestNotification();
  const [channel, setChannel] = useState<"EMAIL" | "WHATSAPP">("EMAIL");
  const [address, setAddress] = useState("");
  const providers = data?.providers ?? {};
  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-5 gap-3">
        <Panel title="Add a channel" meta="verified by one-time code" className="col-span-2">
          <form
            className="flex flex-col gap-3 p-3"
            onSubmit={(e) => {
              e.preventDefault();
              save.mutate({ channel, address: address.trim() }, { onSuccess: () => setAddress("") });
            }}
          >
            <Field id="channel" label="Channel">
              <Select
                id="channel"
                value={channel}
                onValueChange={(v) => setChannel(v as "EMAIL" | "WHATSAPP")}
                options={CHANNELS}
                className="w-full"
              />
            </Field>
            <Field
              id="address"
              label={channel === "EMAIL" ? "Email address" : "WhatsApp number (E.164)"}
              hint={
                providers[channel]
                  ? `Delivered via ${providers[channel]}. The address is stored encrypted.`
                  : `No ${channel === "EMAIL" ? "email" : "WhatsApp"} provider is configured on the server.`
              }
            >
              <Input
                id="address"
                value={address}
                onChange={(e) => setAddress(e.target.value)}
                placeholder={channel === "EMAIL" ? "you@example.com" : "+923001234567"}
              />
            </Field>
            <Button type="submit" variant="primary" disabled={address.trim().length < 3 || save.isPending || !providers[channel]}>
              Send verification code
            </Button>
            {save.error ? <p className="text-xs text-err">{save.error.message}</p> : null}
            <p className="text-2xs text-faint">
              Alerts carry no personal details and never a one-tap submit: they link back to this
              console, where you review and approve.
            </p>
          </form>
        </Panel>
        <Panel
          title="Channels"
          meta={Object.entries(providers).map(([c, p]) => `${c}: ${p}`).join(" · ") || "no providers"}
          className="col-span-3"
          actions={
            <Button size="sm" onClick={() => test.mutate()} disabled={test.isPending || !data?.channels.some((c) => c.verified)}>
              Send test
            </Button>
          }
        >
          <table className="w-full table-fixed border-collapse">
            <thead>
              <tr>
                <th className={cn(th, "w-24")}>Channel</th>
                <th className={th}>Address</th>
                <th className={cn(th, "w-64")}>Status</th>
              </tr>
            </thead>
            <tbody>
              {!data?.channels.length ? (
                <EmptyRow colSpan={3}>No channels yet.</EmptyRow>
              ) : (
                data.channels.map((c) => <ChannelRow key={c.id} c={c} />)
              )}
            </tbody>
          </table>
          {test.data ? (
            <ul className="border-t border-line px-3 py-2 font-mono text-2xs">
              {test.data.map((r, i) => (
                <li key={i} className={r.status === "FAILED" ? "text-err" : "text-ok"}>
                  {r.channel} via {r.provider ?? "?"}: {r.status}
                  {r.error ? ` (${r.error})` : ""}
                </li>
              ))}
            </ul>
          ) : null}
          {test.error ? <p className="border-t border-line px-3 py-1.5 text-xs text-err">{test.error.message}</p> : null}
        </Panel>
      </div>
      <Panel title="Recent notifications" meta="deduplicated per template, application and state">
        <table className="w-full table-fixed border-collapse">
          <thead>
            <tr>
              <th className={cn(th, "w-24")}>Channel</th>
              <th className={th}>Template</th>
              <th className={cn(th, "w-28")}>Status</th>
              <th className={cn(th, "w-28")}>Provider</th>
              <th className={cn(th, "w-24")}>When</th>
            </tr>
          </thead>
          <tbody>
            {!data?.recent.length ? (
              <EmptyRow colSpan={5}>Nothing sent yet.</EmptyRow>
            ) : (
              data.recent.map((n, i) => (
                <tr key={`${n.created_at}-${i}`}>
                  <td className={cn(td, "font-mono text-2xs")}>{n.channel}</td>
                  <td className={cn(td, "text-xs")}>
                    {n.template}
                    {n.error ? <span className="ml-2 text-2xs text-err">{n.error}</span> : null}
                  </td>
                  <td className={td}><StatusPill tone={STATUS_TONE[n.status] ?? "idle"}>{n.status}</StatusPill></td>
                  <td className={cn(td, "font-mono text-2xs text-muted")}>{n.provider ?? "—"}</td>
                  <td className={cn(td, "font-mono text-2xs text-faint")}>{formatRelative(n.created_at)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}

function ChannelRow({ c }: { c: ChannelT }) {
  const verify = useVerifyChannel();
  const remove = useDeleteChannel();
  const [code, setCode] = useState("");
  return (
    <tr>
      <td className={cn(td, "font-mono text-2xs")}>{c.channel}</td>
      <td className={cn(td, "font-mono text-xs")}>{c.address_hint}</td>
      <td className={td}>
        <div className="flex items-center justify-end gap-1">
          {c.verified ? (
            <StatusPill tone="ok">verified</StatusPill>
          ) : (
            <form
              className="flex items-center gap-1"
              onSubmit={(e) => {
                e.preventDefault();
                verify.mutate({ id: c.id, code: code.trim() });
              }}
            >
              <Input
                aria-label="Verification code"
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
                placeholder="6-digit code"
                className={cn("h-6 w-24 font-mono text-xs", verify.error && "border-err")}
              />
              <Button type="submit" size="sm" variant="primary" disabled={code.length !== 6 || verify.isPending}>
                Verify
              </Button>
            </form>
          )}
          <Button size="sm" variant="ghost" onClick={() => remove.mutate(c.id)} disabled={remove.isPending}>
            Remove
          </Button>
        </div>
      </td>
    </tr>
  );
}
