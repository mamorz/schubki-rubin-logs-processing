import type { AuthState, StatusResponse } from "../shared/events";

async function json<T>(response: Response): Promise<T> {
  const body = (await response.json().catch(() => ({}))) as T & { error?: string };
  if (!response.ok) throw new Error(body.error ?? `HTTP ${response.status}`);
  return body;
}

export const api = {
  status: () => fetch("/api/status").then((r) => json<StatusResponse>(r)),
  login: () => fetch("/api/auth/login", { method: "POST" }).then((r) => json<AuthState>(r)),
  cancelLogin: () => fetch("/api/auth/cancel", { method: "POST" }).then((r) => json<AuthState>(r)),
  logout: () => fetch("/api/auth/logout", { method: "POST" }).then((r) => json<AuthState>(r)),
};
