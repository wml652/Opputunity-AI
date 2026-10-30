"use client";

import { useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";

import { refreshSession } from "./api";
import { discoveryKeys } from "./discovery-queries";
import { keys } from "./queries";
import { pipelineEventSchema, type PipelineEvent } from "./schemas";

const MAX_EVENTS = 300;
const STREAM_URL = "/api/v1/events/stream";
const DISCOVERY_EVENTS = new Set([
  "campaign.created",
  "campaign.phase",
  "campaign.counters",
  "page.crawled",
  "search.results",
  "opportunity.found",
  "eligibility.verdict",
  "application.queued",
  "application.state",
  "page.skipped",
  "page.adapted",
  "page.retrying",
  "page.retried",
  "source.failed",
]);
// Filler, inbox, essay and memory events refresh the application/inbox caches.
const APPLICATION_EVENTS = new Set([
  "application.state",
  "application.submission",
  "essay.draft_created",
  "memory.learned",
]);
const SESSION_EVENTS = new Set(["session.captured", "session.failed"]);

export type StreamState = "connecting" | "live" | "reconnecting";

interface EventStreamValue {
  state: StreamState;
  events: PipelineEvent[];
  lastSeq: number;
}

const EventStreamContext = createContext<EventStreamValue>({
  state: "connecting",
  events: [],
  lastSeq: 0,
});

export function useEventStream(): EventStreamValue {
  return useContext(EventStreamContext);
}

/**
 * One EventSource per console session. The browser resends `Last-Event-ID` on its own
 * reconnects; after an auth failure (access token expired) we refresh the session and reopen
 * with `after_seq` so no event is lost.
 */
export function EventStreamProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const [state, setState] = useState<StreamState>("connecting");
  const [events, setEvents] = useState<PipelineEvent[]>([]);
  const lastSeq = useRef(0);

  useEffect(() => {
    let source: EventSource | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    // Events arrive in bursts (counters per page, or dozens replayed after a reconnect).
    // Coalesce cache refreshes per group to at most one per window, and never cancel a fetch
    // that is already in flight: invalidating per event fired ~20 identical GETs per second.
    const pending = new Map<string, ReturnType<typeof setTimeout>>();
    const coalesce = (group: string, queryKeys: readonly (readonly unknown[])[], ms: number) => {
      if (pending.has(group)) return;
      pending.set(
        group,
        setTimeout(() => {
          pending.delete(group);
          for (const queryKey of queryKeys) {
            void qc.invalidateQueries({ queryKey }, { cancelRefetch: false });
          }
        }, ms),
      );
    };
    const scheduleDiscoveryRefresh = () => coalesce("discovery", [discoveryKeys.all], 400);

    const onEvent = (raw: MessageEvent<string>) => {
      const parsed = pipelineEventSchema.safeParse(JSON.parse(raw.data));
      if (!parsed.success) return;
      const event = parsed.data;
      if (event.seq <= lastSeq.current) return;
      lastSeq.current = event.seq;
      setEvents((prev) => [event, ...prev].slice(0, MAX_EVENTS));
      if (event.type === "llm.status" || event.type === "llm.config_changed") {
        coalesce("llm", [keys.status, keys.providers], 300);
      }
      if (DISCOVERY_EVENTS.has(event.type)) scheduleDiscoveryRefresh();
      if (APPLICATION_EVENTS.has(event.type)) coalesce("applications", [["m4"], keys.inbox], 300);
      if (SESSION_EVENTS.has(event.type)) coalesce("sessions", [["sessions"]], 200);
    };

    const open = () => {
      if (stopped) return;
      const url = lastSeq.current > 0 ? `${STREAM_URL}?after_seq=${lastSeq.current}` : STREAM_URL;
      source = new EventSource(url, { withCredentials: true });
      source.onopen = () => setState("live");
      source.onmessage = onEvent;
      source.onerror = () => {
        if (!source || source.readyState !== EventSource.CLOSED) {
          setState("reconnecting"); // browser retries by itself
          return;
        }
        setState("reconnecting");
        source = null;
        retryTimer = setTimeout(async () => {
          await refreshSession();
          open();
        }, 2000);
      };
    };

    open();
    // Keep the 15-minute access token fresh while the console is open.
    const keepAlive = setInterval(() => void refreshSession(), 10 * 60_000);

    return () => {
      stopped = true;
      clearInterval(keepAlive);
      if (retryTimer) clearTimeout(retryTimer);
      for (const timer of pending.values()) clearTimeout(timer);
      source?.close();
    };
  }, [qc]);

  return (
    <EventStreamContext.Provider value={{ state, events, lastSeq: events[0]?.seq ?? 0 }}>
      {children}
    </EventStreamContext.Provider>
  );
}
