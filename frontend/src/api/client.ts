import { useSessionStore } from "@/stores/session";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

export async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? "GET").toUpperCase();
  const session = useSessionStore();
  const csrf = session.csrfToken;
  const response = await fetch(`/api/admin${path}`, {
    credentials: "same-origin",
    ...init,
    headers: {
      Accept: "application/json",
      ...(method !== "GET" && csrf ? { "X-CSRF-Token": csrf } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    let message = `Request failed with status ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: string; error?: string };
      message = body.detail ?? body.error ?? message;
    } catch {
      // Keep the HTTP status when the server did not return JSON.
    }
    throw new ApiError(message, response.status);
  }
  return response.json() as Promise<T>;
}

export async function pollOperation(operationId: string): Promise<Record<string, unknown>> {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    const result = await apiRequest<Record<string, unknown>>(`/service/operations/${operationId}`);
    if (result.status !== "running") return result;
    await new Promise((resolve) => window.setTimeout(resolve, 500));
  }
  throw new ApiError("操作仍在运行，请稍后查看服务状态", 408);
}
