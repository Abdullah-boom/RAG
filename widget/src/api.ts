// Typed client for app/api.py. Field names match the backend response dict exactly.

export type Size = { size: string; price: number };

export type Item = {
  item_id: string;
  name: string;
  category?: string | null;
  price?: number | null;
  price_from?: number | null;
  sizes?: Size[] | null;
  currency?: string | null;
  available?: boolean | null;
  image_url?: string | null;
};

export type CartLine = {
  line_no: number;
  name: string;
  size?: string | null;
  qty: number;
  unit_price: number | null;
  line_total: number | null;
  available: boolean;
};

export type Cart = { lines: CartLine[]; item_count: number; subtotal: number; currency: string; note: string };

export type Branch = { branch_id: string; name: string; city: string | null };

export type BranchStatus = {
  branch_id: string;
  branch: string;
  open_now: boolean;
  closed_reason: string | null;
  today_hours: string[];
};

export type ChatResponse = {
  session_id: string;
  answer: string;
  items: Item[];
  suggestions: string[];
  cart: Cart | null;
  handoff: boolean;
  as_of: string;
};

export type SessionResponse = {
  session_id: string;
  messages: { role: "user" | "assistant"; text: string }[];
  cart: Cart | null;
};

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

const TIMEOUT_MS = 45_000; // agent turns can take ~20 s plus a rate-limit wait

export function createApi(baseUrl: string) {
  async function request<T>(path: string, init?: RequestInit): Promise<T> {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
    try {
      const res = await fetch(baseUrl + path, {
        ...init,
        signal: ctrl.signal,
        headers: init?.body ? { "Content-Type": "application/json" } : undefined,
      });
      if (!res.ok) {
        const detail = await res.json().then((j) => j.detail, () => res.statusText);
        throw new ApiError(res.status, typeof detail === "string" ? detail : res.statusText);
      }
      return (await res.json()) as T;
    } catch (e) {
      if (e instanceof ApiError) throw e;
      throw new ApiError(0, ctrl.signal.aborted ? "timeout" : "network");
    } finally {
      clearTimeout(timer);
    }
  }

  const q = (params: Record<string, string>) => new URLSearchParams(params).toString();

  return {
    branches: () => request<{ branches: Branch[] }>("/api/branches").then((r) => r.branches),
    branchStatus: (branchId: string) =>
      request<BranchStatus>(`/api/branches/${encodeURIComponent(branchId)}/status`),
    session: (branchId: string, sessionId: string) =>
      request<SessionResponse>(`/api/session?${q({ branch_id: branchId, session_id: sessionId })}`),
    chat: (message: string, branchId: string, sessionId: string | null) =>
      request<ChatResponse>("/api/chat", {
        method: "POST",
        body: JSON.stringify({ message, branch_id: branchId, session_id: sessionId }),
      }),
    clearCart: (branchId: string, sessionId: string) =>
      request<{ cart: null }>("/api/cart/clear", {
        method: "POST",
        body: JSON.stringify({ branch_id: branchId, session_id: sessionId }),
      }),
  };
}

export type Api = ReturnType<typeof createApi>;
