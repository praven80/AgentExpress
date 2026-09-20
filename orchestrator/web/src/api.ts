/** The one place a request leaves the browser.
 *
 *  Every call carries the IdP's ID token as a Bearer, because the API Gateway JWT
 *  authorizer on /api/* is what stops an unauthenticated request reaching the BFF.
 *  The token is set once by auth.ts at boot and read from here, so no component
 *  handles credentials. */

let idToken: string | null = null;

export function setToken(token: string | null): void {
  idToken = token;
}

export function getToken(): string | null {
  return idToken;
}

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (idToken) headers.set("Authorization", "Bearer " + idToken);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(path, { ...init, headers });
  if (!res.ok) {
    // The BFF returns {"error": "..."} on a refusal, and that message names the group
    // a 403 wanted — surfacing it beats "Request failed".
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { error?: string; message?: string };
      detail = body.error || body.message || detail;
    } catch {
      /* a non-JSON body is still a failure; keep the status text */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) }),
  del: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};
