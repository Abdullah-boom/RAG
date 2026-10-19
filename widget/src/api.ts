// Typed client for app/api.py. Field names match the backend response dict exactly.

export type Size = { size: string; price: number };

export type Item = {
  item_id: string;
  name: string;
  type?: string | null; // what it is: "pizza", "burger", "deal"...
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

export type PaymentMethod = {
  code: string; // e.g. "COD", "XPAY_CARD" (the same codes the app's orders use)
  name: string;
  kind: string;
  when: "delivery" | "pickup" | "any";
  pay_now: boolean; // online: paid in the app's secure checkout
  tax_rate: number | null;
  tax: number | null;
  total: number | null; // estimate for the current cart
};

export type Checkout = { payment_method: PaymentMethod | null; note: string; secure_note: string };

export type Cart = {
  lines: CartLine[];
  item_count: number;
  subtotal: number;
  currency: string;
  note: string;
  checkout?: Checkout;
};

export type OrderProblem = { code: string; items?: string[]; minimum?: number; method?: string };

export type OrderReview = {
  ready: boolean;
  problems: OrderProblem[];
  summary: { lines: CartLine[]; subtotal: number; payment_method: PaymentMethod | null; note: string };
  confirm_token: string | null; // valid 15 min, for exactly this cart and payment choice
};

/** placed: order_no set, cart emptied. not_connected: complete in the app. rejected: message says why. failed: retry. */
export type OrderResult = {
  status: "placed" | "not_connected" | "rejected" | "failed";
  order_no: string | null;
  message: string | null;
  cart: Cart | null;
};

export type PaymentMethodsResponse = {
  methods: PaymentMethod[];
  chosen: string | null;
  cart_subtotal: number;
  note: string;
  secure_note: string;
};

export type Branch = {
  branch_id: string;
  name: string;
  city: string | null;
  open_now: boolean; // only open branches can be selected
  accepting_orders: boolean;
  pickup: boolean; // offers pickup (every branch delivers)
};

export type OrderType = "DELIVERY" | "PICKUP";

/** The customer's order choices, shown like the cart. Changed in chat or by choosing delivery / pickup. */
export type OrderView = {
  type: OrderType | null;
  type_name: string | null;
  payment_method: { code: string; name: string } | null;
};

/** Something the app must do before a switch asked for in chat can happen. */
export type ChatAction = { type: "need_location" | "pick_pickup_branch"; order_type: OrderType };

/** Where the device is, as a default branch. branch is null unless reason is "matched". */
export type LocateResponse = {
  reason: "matched" | "branch_closed" | "no_coverage";
  branch: Branch | null;
  covering: Branch[]; // branches whose delivery area contains the point, best first
};

export type SelectResponse = {
  branch: Branch;
  status: BranchStatus;
  guest_id: string | null;
  cart: Cart | null;
  order: OrderView;
};

export type OrderTypeResponse = SelectResponse & { cart_moved: boolean };

export type BranchStatus = {
  branch_id: string;
  branch: string;
  open_now: boolean;
  closed_reason: string | null;
  today_hours: string[];
};

export type ChatResponse = {
  session_id: string;
  guest_id: string | null;
  answer: string;
  items: Item[];
  suggestions: string[];
  cart: Cart | null;
  handoff: boolean;
  as_of: string;
  order: OrderView | null;
  action: ChatAction | null;
  confirm: boolean; // an order summary: show Confirm / Cancel (the server holds the confirm token)
};

export type CartActionResponse = {
  guest_id: string | null;
  added?: { name: string; size?: string | null; qty: number }[];
  removed?: { name: string; size?: string | null; qty: number };
  problems?: { error: string; item?: string }[];
  error?: string;
  cart: Cart | null;
};

export type SessionResponse = {
  session_id: string;
  messages: { role: "user" | "assistant"; text: string }[];
  cart: Cart | null;
  order: OrderView;
};

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

const TIMEOUT_MS = 45_000; // agent turns can take ~20 s plus a rate-limit wait

/** Returns the host app's current login JWT, or nothing for a guest. Called before every request,
 *  so the host can hand out a freshly refreshed token each time. */
export type TokenProvider = () => string | null | undefined | Promise<string | null | undefined>;

async function failure(res: Response): Promise<ApiError> {
  const detail = await res.json().then((j) => j.detail, () => res.statusText);
  const code = typeof detail === "string" ? detail : typeof detail?.code === "string" ? detail.code : res.statusText;
  return new ApiError(res.status, code);
}

/** Minimal Server-Sent Events reader for a fetch() body (EventSource can't POST or send headers). */
async function readEvents(res: Response, onEvent: (event: string, data: any) => void) {
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true });
    let end;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      let event = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (data) onEvent(event, JSON.parse(data));
    }
  }
}

/** The guest's cart owner id: minted by the server, stored by the app, sent back as X-Guest-Id. */
export type GuestIdStore = { get: () => string | null; set: (id: string) => void };

export function createApi(baseUrl: string, getToken: TokenProvider = () => null, guest?: GuestIdStore) {
  async function headers(json: boolean): Promise<Record<string, string>> {
    const h: Record<string, string> = json ? { "Content-Type": "application/json" } : {};
    const token = await Promise.resolve(getToken()).catch(() => null);
    if (token) h.Authorization = `Bearer ${token}`;
    const gid = guest?.get();
    if (gid) h["X-Guest-Id"] = gid;
    return h;
  }

  function keepGuest<T>(r: T): T {
    const gid = (r as { guest_id?: string | null })?.guest_id;
    if (gid && guest && gid !== guest.get()) guest.set(gid);
    return r;
  }

  async function request<T>(path: string, init?: RequestInit): Promise<T> {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
    try {
      const res = await fetch(baseUrl + path, { ...init, signal: ctrl.signal, headers: await headers(!!init?.body) });
      if (!res.ok) throw await failure(res);
      return keepGuest((await res.json()) as T);
    } catch (e) {
      if (e instanceof ApiError) throw e;
      throw new ApiError(0, ctrl.signal.aborted ? "timeout" : "network");
    } finally {
      clearTimeout(timer);
    }
  }

  /** POST /api/chat with stream=true: progress lines go to onStatus; resolves with the verified answer. */
  async function chatStream(
    message: string,
    branchId: string,
    sessionId: string | null,
    onStatus: (text: string) => void,
  ): Promise<ChatResponse> {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
    try {
      const res = await fetch(baseUrl + "/api/chat", {
        method: "POST",
        signal: ctrl.signal,
        headers: await headers(true),
        body: JSON.stringify({ message, branch_id: branchId, session_id: sessionId, stream: true }),
      });
      if (!res.ok) throw await failure(res);
      let answer: ChatResponse | null = null;
      let error: ApiError | null = null;
      await readEvents(res, (event, data) => {
        if (event === "status") onStatus(data.text);
        else if (event === "answer") answer = keepGuest(data as ChatResponse);
        else if (event === "error")
          error = new ApiError(
            data.code === "database_unavailable" || data.code === "cart_unavailable" ? 503 : data.code === "cart_busy" ? 409 : 500,
            data.code,
          );
      });
      if (error) throw error;
      if (!answer) throw new ApiError(0, "network"); // stream cut before the answer
      return answer;
    } catch (e) {
      if (e instanceof ApiError) throw e;
      throw new ApiError(0, ctrl.signal.aborted ? "timeout" : "network");
    } finally {
      clearTimeout(timer);
    }
  }

  const q = (params: Record<string, string>) => new URLSearchParams(params).toString();

  const post = <T>(path: string, body: object) => request<T>(path, { method: "POST", body: JSON.stringify(body) });

  return {
    branches: () => request<{ branches: Branch[] }>("/api/branches").then((r) => r.branches),
    branchStatus: (branchId: string) =>
      request<BranchStatus>(`/api/branches/${encodeURIComponent(branchId)}/status`),
    /** Default branch for these coordinates (from the branches' delivery areas). */
    locate: (lat: number, lng: number) => post<LocateResponse>("/api/branches/locate", { lat, lng }),
    /** Delivery (from the device's location) or pickup (from a branch). Sets the branch; 409 codes:
     *  no_coverage, branch_closed, pickup_not_offered. from_branch_id: its cart comes along. */
    setOrderType: (
      orderType: OrderType,
      where: { lat?: number; lng?: number; branch_id?: string; from_branch_id?: string | null },
    ) => post<OrderTypeResponse>("/api/order/type", { order_type: orderType, ...where }),
    /** Switch branch: the server refuses closed ones (409 branch_closed) and returns that branch's cart. */
    select: (branchId: string) => post<SelectResponse>("/api/branches/select", { branch_id: branchId }),
    session: (branchId: string, sessionId: string) =>
      request<SessionResponse>(`/api/session?${q({ branch_id: branchId, session_id: sessionId })}`),
    chatStream,
    /** The Confirm order / Cancel buttons under the summary: same as typing yes / no, never through the model.
     *  409 nothing_to_confirm when the summary no longer stands. */
    chatConfirm: (branchId: string, sessionId: string, answer: "yes" | "no") =>
      post<ChatResponse>("/api/chat/confirm", { branch_id: branchId, session_id: sessionId, answer }),
    // Cart: one per branch, separate from the chat. Direct data calls, no LLM.
    cartGet: (branchId: string) =>
      request<{ guest_id: string | null; cart: Cart | null; order: OrderView }>(`/api/cart?${q({ branch_id: branchId })}`),
    cartAdd: (branchId: string, itemId: string, size?: string | null) =>
      post<CartActionResponse>("/api/cart/add", { branch_id: branchId, item_id: itemId, size: size ?? null, qty: 1 }),
    cartQuantity: (branchId: string, lineNo: number, qty: number) =>
      post<CartActionResponse>("/api/cart/quantity", { branch_id: branchId, line_no: lineNo, qty }),
    cartRemove: (branchId: string, lineNo: number) =>
      post<CartActionResponse>("/api/cart/remove", { branch_id: branchId, line_no: lineNo }),
    clearCart: (branchId: string) => post<CartActionResponse>("/api/cart/clear", { branch_id: branchId }),
    // Payment: choose how to pay for this branch's cart (paid later in the app's checkout; never card details here).
    paymentMethods: (branchId: string) =>
      request<PaymentMethodsResponse>(`/api/payment-methods?${q({ branch_id: branchId })}`),
    // Ordering for apps with their own Confirm button. The widget orders in chat: the bot shows the summary and the
    // customer's "yes" places it.
    reviewOrder: (branchId: string) => post<OrderReview>("/api/checkout/review", { branch_id: branchId }),
    confirmOrder: (branchId: string, token: string) =>
      post<OrderResult>("/api/checkout/confirm", { branch_id: branchId, confirm_token: token }),
    setPayment: (branchId: string, method: string) =>
      post<{ payment_method: string; cart: Cart | null }>("/api/cart/payment", { branch_id: branchId, method }),
  };
}

export type Api = ReturnType<typeof createApi>;
