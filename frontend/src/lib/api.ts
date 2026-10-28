import type { z } from "zod";

const CSRF_COOKIE = "csrf_token";
const MUTATING = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const NO_REFRESH = ["/api/v1/auth/login", "/api/v1/auth/register", "/api/v1/auth/refresh"];

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const match = document.cookie.split("; ").find((c) => c.startsWith(`${name}=`));
  return match ? decodeURIComponent(match.slice(name.length + 1)) : null;
}

let refreshInFlight: Promise<boolean> | null = null;

/** Rotate the refresh token once, shared by concurrent callers. */
export function refreshSession(): Promise<boolean> {
  refreshInFlight ??= fetch("/api/v1/auth/refresh", {
    method: "POST",
    credentials: "same-origin",
    headers: { "x-csrf-token": readCookie(CSRF_COOKIE) ?? "" },
  })
    .then((r) => r.ok)
    .catch(() => false)
    .finally(() => {
      refreshInFlight = null;
    });
  return refreshInFlight;
}

interface RequestOptions<T> {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  schema?: z.ZodType<T>;
}

async function toError(response: Response): Promise<ApiError> {
  try {
    const body = (await response.json()) as { error?: { code?: string; message?: string } };
    return new ApiError(
      response.status,
      body.error?.code ?? "http_error",
      body.error?.message ?? response.statusText,
    );
  } catch {
    return new ApiError(response.status, "http_error", response.statusText);
  }
}

export async function api<T = void>(path: string, options: RequestOptions<T> = {}): Promise<T> {
  const method = options.method ?? "GET";
  const send = () => {
    const headers: Record<string, string> = { accept: "application/json" };
    const isForm = options.body instanceof FormData;
    // FormData sets its own multipart boundary header.
    if (options.body !== undefined && !isForm) headers["content-type"] = "application/json";
    if (MUTATING.has(method)) headers["x-csrf-token"] = readCookie(CSRF_COOKIE) ?? "";
    return fetch(path, {
      method,
      headers,
      credentials: "same-origin",
      body:
        options.body === undefined
          ? undefined
          : isForm
            ? (options.body as FormData)
            : JSON.stringify(options.body),
    });
  };

  let response = await send();
  if (response.status === 401 && !NO_REFRESH.includes(path) && (await refreshSession())) {
    response = await send();
  }
  if (!response.ok) throw await toError(response);
  if (response.status === 204 || !options.schema) return undefined as T;
  return options.schema.parse(await response.json());
}
