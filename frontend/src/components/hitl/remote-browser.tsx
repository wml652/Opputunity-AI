"use client";

import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type PointerEvent, type WheelEvent } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";
import { hitlControlSchema, hitlStreamUrl, type HitlSessionT } from "@/lib/hitl-queries";

type ConnState = "connecting" | "live" | "closed" | "error";
type InputEvent =
  | { type: "mouse"; action: "move" | "down" | "up" | "click" | "wheel"; x: number; y: number; button?: "left" | "right" | "middle"; dx?: number; dy?: number }
  | { type: "key"; action: "down" | "up"; key: string }
  | { type: "text"; text: string }
  | { type: "nav"; action: "back" | "forward" | "reload" }
  | { type: "finish" }
  | { type: "close" };

const MOVE_EVERY_MS = 40;
const BUTTON: Record<number, "left" | "middle" | "right"> = { 0: "left", 1: "middle", 2: "right" };
const KIND_LABEL: Record<string, string> = {
  TEACH: "Teach Mode",
  PORTAL: "Portal sign-in",
  CHALLENGE: "Verification challenge",
  LINKEDIN: "LinkedIn sign-in",
};

/**
 * A browser running on the server, shown and driven from here: frames stream in over a
 * WebSocket; clicks, scrolling, typing and paste go back. Typed text goes only to the page -
 * it is never stored or logged (it may be a portal password).
 */
export function RemoteBrowser({ session, onClose }: { session: HitlSessionT; onClose: () => void }) {
  const socket = useRef<WebSocket | null>(null);
  const image = useRef<HTMLImageElement | null>(null);
  const lastMove = useRef(0);
  const frameUrl = useRef<string | null>(null);
  const [src, setSrc] = useState<string | null>(null);
  const [viewport, setViewport] = useState({ width: 1280, height: 900 });
  const [conn, setConn] = useState<ConnState>("connecting");

  useEffect(() => {
    const ws = new WebSocket(hitlStreamUrl(session.id));
    ws.binaryType = "blob";
    socket.current = ws;
    ws.onopen = () => setConn("live");
    ws.onerror = () => setConn("error");
    ws.onclose = () => setConn((c) => (c === "error" ? c : "closed"));
    ws.onmessage = (msg) => {
      if (typeof msg.data === "string") {
        const parsed = hitlControlSchema.safeParse(JSON.parse(msg.data));
        if (!parsed.success) return;
        if (parsed.data.type === "meta") setViewport(parsed.data.viewport);
        else setConn("closed");
        return;
      }
      const url = URL.createObjectURL(msg.data as Blob);
      if (frameUrl.current) URL.revokeObjectURL(frameUrl.current);
      frameUrl.current = url;
      setSrc(url);
    };
    return () => {
      ws.close();
      socket.current = null;
      if (frameUrl.current) URL.revokeObjectURL(frameUrl.current);
      frameUrl.current = null;
    };
  }, [session.id]);

  const send = useCallback((event: InputEvent) => {
    const ws = socket.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(event));
  }, []);

  /** Viewer pixels -> page viewport coordinates. */
  const point = (clientX: number, clientY: number) => {
    const rect = image.current?.getBoundingClientRect();
    if (!rect || rect.width === 0 || rect.height === 0) return null;
    const x = ((clientX - rect.left) / rect.width) * viewport.width;
    const y = ((clientY - rect.top) / rect.height) * viewport.height;
    return { x: Math.max(0, Math.min(viewport.width, x)), y: Math.max(0, Math.min(viewport.height, y)) };
  };

  const onPointer = (action: "down" | "up" | "move") => (e: PointerEvent<HTMLImageElement>) => {
    const p = point(e.clientX, e.clientY);
    if (!p) return;
    if (action === "move") {
      const now = performance.now();
      if (now - lastMove.current < MOVE_EVERY_MS) return;
      lastMove.current = now;
    } else {
      e.preventDefault();
      (e.currentTarget.parentElement as HTMLElement | null)?.focus();
    }
    send({ type: "mouse", action, ...p, button: BUTTON[e.button] ?? "left" });
  };

  const onWheel = (e: WheelEvent<HTMLImageElement>) => {
    const p = point(e.clientX, e.clientY);
    if (p) send({ type: "mouse", action: "wheel", ...p, dx: e.deltaX, dy: e.deltaY });
  };

  const onKey = (action: "down" | "up") => (e: KeyboardEvent<HTMLDivElement>) => {
    e.preventDefault();
    const printable = e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey;
    if (printable) {
      if (action === "down") send({ type: "text", text: e.key });
      return;
    }
    if (e.key === "Unidentified" || e.key.length > 32) return;
    send({ type: "key", action, key: e.key });
  };

  const live = conn === "live";
  const kind = KIND_LABEL[session.kind] ?? session.kind;
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-1.5 border-b border-line px-2 py-1.5">
        <span
          className={cn("h-1.5 w-1.5 rounded-full", live ? "bg-ok" : conn === "connecting" ? "bg-warn" : "bg-err")}
          aria-hidden
        />
        <span className="text-2xs font-medium uppercase tracking-wide text-faint">{kind}</span>
        <span className="min-w-0 truncate text-xs" title={session.url}>
          {session.title}
        </span>
        <span className="ml-auto flex items-center gap-1">
          <Button size="sm" variant="ghost" onClick={() => send({ type: "nav", action: "back" })} disabled={!live}>
            Back
          </Button>
          <Button size="sm" variant="ghost" onClick={() => send({ type: "nav", action: "forward" })} disabled={!live}>
            Forward
          </Button>
          <Button size="sm" variant="ghost" onClick={() => send({ type: "nav", action: "reload" })} disabled={!live}>
            Reload
          </Button>
          {session.kind === "TEACH" ? (
            <Button size="sm" variant="primary" onClick={() => send({ type: "finish" })} disabled={!live}>
              Finish training &amp; save
            </Button>
          ) : null}
          <Button size="sm" onClick={onClose} title="Close the remote browser (signed-in cookies are kept)">
            Done
          </Button>
        </span>
      </div>
      <div
        tabIndex={0}
        role="application"
        aria-label={`Remote browser: ${session.title}`}
        className="relative flex min-h-0 flex-1 items-center justify-center bg-surface-2 outline-none focus-visible:ring-1 focus-visible:ring-line-strong"
        onKeyDown={onKey("down")}
        onKeyUp={onKey("up")}
        onPaste={(e) => {
          const text = e.clipboardData.getData("text").slice(0, 4000);
          if (text) send({ type: "text", text });
          e.preventDefault();
        }}
      >
        {src ? (
          // eslint-disable-next-line @next/next/no-img-element -- a live frame stream, not a static asset
          <img
            ref={image}
            src={src}
            alt="Live view of the remote browser"
            draggable={false}
            className="max-h-full max-w-full cursor-default select-none border border-line"
            onPointerDown={onPointer("down")}
            onPointerUp={onPointer("up")}
            onPointerMove={onPointer("move")}
            onWheel={onWheel}
            onContextMenu={(e) => e.preventDefault()}
          />
        ) : (
          <span className="font-mono text-2xs text-faint">
            {conn === "closed" ? "session ended" : conn === "error" ? "stream unavailable" : "waiting for the first frame"}
          </span>
        )}
      </div>
      <p className="border-t border-line px-2 py-1 text-2xs text-faint">
        Click into the page and type as usual. What you type goes to the portal only and is never stored; passwords are
        never recorded. The agent does nothing in this browser on its own.
      </p>
    </div>
  );
}
