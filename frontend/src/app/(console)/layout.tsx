"use client";

import { useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";

import { Sidebar } from "@/components/telemetry/sidebar";
import { TelemetryBar } from "@/components/telemetry/telemetry-bar";
import { ApiError } from "@/lib/api";
import { EventStreamProvider } from "@/lib/event-stream";
import { useMe } from "@/lib/queries";

export default function ConsoleLayout({ children }: { children: ReactNode }) {
  const router = useRouter();
  const me = useMe();
  const unauthenticated = me.error instanceof ApiError && me.error.status === 401;

  useEffect(() => {
    if (unauthenticated) router.replace("/login");
  }, [unauthenticated, router]);

  if (!me.data) {
    return (
      <div className="flex h-full items-center justify-center font-mono text-xs text-faint">
        {me.isError && !unauthenticated ? "API unreachable — is the backend running?" : "Loading"}
      </div>
    );
  }

  return (
    <EventStreamProvider>
      <div className="flex h-full flex-col">
        <TelemetryBar />
        <div className="flex min-h-0 flex-1">
          <Sidebar />
          <main className="min-w-0 flex-1 overflow-y-auto p-3">{children}</main>
        </div>
      </div>
    </EventStreamProvider>
  );
}
