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
]);

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
    let refreshTimer: ReturnType<typeof setTimeout> | undefined;
    // Counters arrive per page; coalesce cache refreshes to at most one every 400 ms.
    const scheduleDiscoveryRefresh = () => {
      if (refreshTimer) return;
      refreshTimer = setTimeout(() => {
        refreshTimer = undefined;
        void qc.invalidateQueries({ queryKey: discoveryKeys.all });
      }, 400);
    };

    const onEvent = (raw: MessageEvent<string>) => {
      const parsed = pipelineEventSchema.safeParse(JSON.parse(raw.data));
      if (!parsed.success) return;
      const event = parsed.data;
      if (event.seq <= lastSeq.current) return;
      lastSeq.current = event.seq;
      setEvents((prev) => [event, ...prev].slice(0, MAX_EVENTS));
      if (event.type === "llm.status" || event.type === "llm.config_changed") {
        void qc.invalidateQueries({ queryKey: keys.status });
        void qc.invalidateQueries({ queryKey: keys.providers });
      }
      if (DISCOVERY_EVENTS.has(event.type)) scheduleDiscoveryRefresh();
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
      if (refreshTimer) clearTimeout(refreshTimer);
      source?.close();
    };
  }, [qc]);

  return (
    <EventStreamContext.Provider value={{ state, events, lastSeq: events[0]?.seq ?? 0 }}>
      {children}
    </EventStreamContext.Provider>
  );
}
