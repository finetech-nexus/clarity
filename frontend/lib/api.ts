export const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8095";

export function authHeader(): HeadersInit {
  const token = typeof window === "undefined" ? "" : sessionStorage.getItem("clarity_token") || "";
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export async function searchPage<T>(path: string, query: string, page: number, size = 25): Promise<{ items: T[]; total: number }> {
  const params = new URLSearchParams({ q: query, limit: String(size), offset: String(page * size) });
  const joiner = path.includes("?") ? "&" : "?";
  return api(`${path}${joiner}${params}`);
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...authHeader(),
      ...(init.headers || {}),
    },
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || response.statusText);
  }
  return response.json() as Promise<T>;
}
