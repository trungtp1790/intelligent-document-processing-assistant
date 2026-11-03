export const API_URL = import.meta.env.VITE_API_URL ?? "/api/v1";

export type Session = {
  access_token: string;
  user_id: string;
  org_id: string;
  org_slug: string;
  org_name: string;
  role: "operator" | "reviewer";
  expires_in: number;
};

export class ApiError extends Error {
  code: string;
  status: number;
  details: Record<string, unknown>;
  constructor(status: number, error: { code?: string; message?: string; details?: Record<string, unknown> }) {
    super(error.message ?? "Request failed");
    this.status = status;
    this.code = error.code ?? "HTTP_ERROR";
    this.details = error.details ?? {};
  }
}

export function idempotencyKey(): string {
  return crypto.randomUUID();
}

export async function api<T>(session: Session | null, path: string, init: RequestInit = {}, key?: string): Promise<T> {
  const headers = new Headers(init.headers);
  if (session) headers.set("Authorization", `Bearer ${session.access_token}`);
  if (key) headers.set("Idempotency-Key", key);
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(`${API_URL}${path}`, { ...init, headers });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ error: { message: `HTTP ${response.status}` } }));
    throw new ApiError(response.status, body.error ?? {});
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function protectedBlob(session: Session, path: string): Promise<string> {
  const response = await fetch(`${API_URL}${path}`, {
    headers: { Authorization: `Bearer ${session.access_token}` },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const error = body.error ?? {};
    throw new ApiError(response.status, {
      ...error,
      code: error.code ?? `HTTP_${response.status}`,
      message: error.message ?? `Không tải được chứng từ. HTTP ${response.status}.`,
    });
  }
  return URL.createObjectURL(await response.blob());
}

