import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  createApi,
  type Branch,
  type BranchStatus,
  type Cart,
  type Item,
  type OrderReview,
  type PaymentMethod,
  type TokenProvider,
} from "./api";
import { load, save } from "./storage";
import BranchPicker from "./BranchPicker";
import CartBar from "./CartBar";
import MessageView from "./MessageView";

export type WidgetConfig = {
  apiUrl: string;
  title: string;
  color: string;
  position: "left" | "right";
  getToken?: TokenProvider; // from window.CheeziousChatConfig, set by the host app before the script loads
};

export type Message = {
  id: number;
  role: "user" | "assistant";
  text: string;
  items?: Item[];
  error?: boolean;
  retry?: string; // the question to resend after an error
};

const STARTERS = ["Show deals", "Is my branch open?", "Kuch teekha 800 se kam"];
const WELCOME =
  "Hi! Ask me about the menu, prices, deals or branch timings, in English, Roman Urdu or اردو. " +
  "I can also build a cart for you.";

let nextId = 1;
const msg = (m: Omit<Message, "id">): Message => ({ id: nextId++, ...m });

function errorText(e: unknown): string {
  const status = e instanceof ApiError ? e.status : 0;
  if (status === 401) {
    const code = e instanceof ApiError ? e.message : "";
    return code === "login_required"
      ? "Please log in to chat with us."
      : "Your login has expired. Please refresh the page and sign in again.";
  }
  if (status === 429) return "You're sending messages quickly. Please wait a minute and try again.";
  if (status === 0) return "I couldn't reach the server. Check your connection and try again.";
  if (status === 503) return "Sorry, I can't reach our live menu right now. Please try again in a minute.";
  return "Something went wrong on our side. Please try again.";
}

type Phase = "locating" | "picking" | "ready";

const LOCATION_TIMEOUT_MS = 10_000;

/** The device's position, if the customer allows it. Rejects when denied, unavailable or slow. */
function currentPosition(): Promise<GeolocationPosition> {
  return new Promise((resolve, reject) => {
    if (!("geolocation" in navigator)) return reject(new Error("unsupported"));
    navigator.geolocation.getCurrentPosition(resolve, reject, {
      enableHighAccuracy: false, // Wi-Fi / cell accuracy is enough to find the delivery area, and faster
      timeout: LOCATION_TIMEOUT_MS,
      maximumAge: 5 * 60_000,
    });
  });
}

function locationErrorText(e: unknown): string {
  const code = (e as GeolocationPositionError)?.code;
  if (code === 1) return "Location is off for this site, so pick your branch below.";
  return "We couldn't get your location, so pick your branch below.";
}

export default function App({ config }: { config: WidgetConfig }) {
  // Read on every request, so the host can swap or refresh the token without remounting the widget.
  const tokenRef = useRef<TokenProvider | null>(config.getToken ?? null);
  const api = useMemo(
    () =>
      createApi(config.apiUrl, () => tokenRef.current?.(), {
        get: () => load("guest"),
        set: (id) => save("guest", id),
      }),
    [config.apiUrl],
  );
  const [open, setOpen] = useState(false);
  const [branch, setBranch] = useState<Branch | null>(null);
  const [phase, setPhase] = useState<Phase>("picking");
  const [notice, setNotice] = useState<string | null>(null); // why the picker is showing
  const [nearby, setNearby] = useState<string[]>([]); // branches delivering to the customer's spot
  const [switching, setSwitching] = useState(false);
  const [status, setStatus] = useState<BranchStatus | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [suggestions, setSuggestions] = useState<string[]>(STARTERS);
  const [cart, setCart] = useState<Cart | null>(null);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  const [cartOpen, setCartOpen] = useState(false);
  const [payMethods, setPayMethods] = useState<PaymentMethod[] | null>(null);
  const [review, setReview] = useState<OrderReview | null>(null);
  const [ordering, setOrdering] = useState(false);
  const [input, setInput] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  // Every branch decision (location on open, or a manual pick) takes a new number; a reply that arrives
  // after a newer decision is ignored, so a slow location lookup can't undo the customer's own choice.
  const decision = useRef(0);

  const branchRef = useRef(branch);
  branchRef.current = branch;
  const isCurrent = (branchId: string) => branchRef.current?.branch_id === branchId;

  // Conversation only: the cart belongs to the branch and survives a new chat.
  const resetConversation = useCallback(() => {
    setSessionId(null);
    setMessages([msg({ role: "assistant", text: WELCOME })]);
    setSuggestions(STARTERS);
  }, []);

  const restoreConversation = useCallback(
    (b: Branch, ticket: number) => {
      resetConversation();
      const saved = load(`session:${b.branch_id}`);
      if (!saved) return;
      api.session(b.branch_id, saved).then(
        (s) => {
          if (decision.current !== ticket) return;
          setSessionId(s.session_id);
          if (s.messages.length) {
            setMessages([msg({ role: "assistant", text: WELCOME }), ...s.messages.map((m) => msg(m))]);
            setSuggestions([]);
          }
        },
        () => save(`session:${b.branch_id}`, null), // expired or unknown: start fresh
      );
    },
    [api, resetConversation],
  );

  /** Switch to a branch through the server (it refuses closed ones) and load that branch's own cart. */
  const activate = useCallback(
    async (b: Branch, ticket: number, manual: boolean) => {
      setSwitching(true);
      try {
        const r = await api.select(b.branch_id);
        if (decision.current !== ticket) return;
        setBranch(r.branch);
        setStatus(r.status);
        setCart(r.cart);
        setCartOpen(false);
        setNotice(null);
        setPhase("ready");
        save("branch", r.branch.branch_id); // only shown as "last used" in the picker
        restoreConversation(r.branch, ticket);
      } catch (e) {
        if (decision.current !== ticket) return;
        const closed = e instanceof ApiError && e.status === 409;
        setNotice(
          closed
            ? `${b.name} is closed right now. Please pick an open branch.`
            : manual
              ? errorText(e)
              : "We couldn't open your nearest branch, so pick one below.",
        );
        setPhase("picking");
      } finally {
        if (decision.current === ticket) setSwitching(false);
      }
    },
    [api, restoreConversation],
  );

  /** On every open: default to the branch whose delivery area contains the customer, if any is open. */
  const chooseByLocation = useCallback(async () => {
    const ticket = ++decision.current;
    setPhase("locating");
    setNotice(null);
    let pos: GeolocationPosition;
    try {
      pos = await currentPosition();
    } catch (e) {
      if (decision.current !== ticket) return;
      setNearby([]);
      setNotice(locationErrorText(e));
      setPhase("picking");
      return;
    }
    if (decision.current !== ticket) return;
    try {
      const r = await api.locate(pos.coords.latitude, pos.coords.longitude);
      if (decision.current !== ticket) return;
      setNearby(r.covering.map((b) => b.branch_id));
      if (r.branch) return void activate(r.branch, ticket, false);
      setNotice(
        r.reason === "branch_closed"
          ? "The branch that delivers to you is closed right now. You can pick another open branch."
          : "No branch delivers to your current location. You can still pick a branch below.",
      );
      setPhase("picking");
    } catch {
      if (decision.current !== ticket) return;
      setNotice("We couldn't find your branch automatically, so pick one below.");
      setPhase("picking");
    }
  }, [api, activate]);

  // Page API for the host site / app:
  //   open(), close()   e.g. from an "Ask us" button
  //   setAuth(getToken) give the widget the login token (a function returning the current JWT, a string, or null)
  //   reset()           clear the conversation, e.g. when a different customer logs in
  useEffect(() => {
    const forget = () => {
      const b = branchRef.current;
      if (b) save(`session:${b.branch_id}`, null);
      resetConversation();
    };
    (window as any).CheeziousChat = {
      open: () => setOpen(true),
      close: () => setOpen(false),
      reset: forget,
      setAuth: (p: TokenProvider | string | null) => {
        const wasLoggedIn = tokenRef.current !== null;
        tokenRef.current = typeof p === "string" ? () => p : p ?? null;
        // logging in or out switches to that identity's conversation and carts; never show one customer's to another
        if (wasLoggedIn !== (tokenRef.current !== null)) {
          forget();
          const b = branchRef.current;
          if (b) api.cartGet(b.branch_id).then((r) => isCurrent(b.branch_id) && setCart(r.cart), () => setCart(null));
        }
      },
    };
  }, [api, resetConversation]);

  // Each time the panel opens, the default branch is worked out again from where the customer is now.
  useEffect(() => {
    if (open) void chooseByLocation();
  }, [open, chooseByLocation]);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, busy]);

  // A review is for one exact cart: any change (items, payment, branch) means review again.
  useEffect(() => setReview(null), [cart, branch]);

  // Payment options for the open cart; estimates depend on the subtotal, so reload when it changes.
  const subtotal = cart?.subtotal ?? null;
  useEffect(() => {
    if (!cartOpen || !branch) return;
    const branchId = branch.branch_id;
    setPayMethods(null);
    api.paymentMethods(branchId).then(
      (r) => isCurrent(branchId) && setPayMethods(r.methods),
      () => isCurrent(branchId) && setPayMethods([]),
    );
  }, [api, cartOpen, branch, subtotal]);

  useEffect(() => {
    if (open && phase === "ready") inputRef.current?.focus();
  }, [open, phase]);

  function chooseBranch(b: Branch) {
    const ticket = ++decision.current; // a manual pick wins over a location lookup still in flight
    if (b.branch_id === branch?.branch_id && phase === "picking" && !notice) {
      setPhase("ready");
      return;
    }
    void activate(b, ticket, true);
  }

  async function send(text: string) {
    const question = text.trim();
    if (!question || busy || !branch) return;
    const b = branch;
    setInput("");
    setSuggestions([]);
    setMessages((m) => [...m.filter((x) => !x.error), msg({ role: "user", text: question })]);
    setBusy(true);
    setProgress(null);
    try {
      const r = await api.chatStream(question, b.branch_id, sessionId, setProgress);
      save(`session:${b.branch_id}`, r.session_id);
      if (!isCurrent(b.branch_id)) return; // switched branch while this was running
      setSessionId(r.session_id);
      setMessages((m) => [...m, msg({ role: "assistant", text: r.answer, items: r.items })]);
      setSuggestions(r.suggestions);
      setCart(r.cart);
    } catch (e) {
      if (e instanceof ApiError && e.message === "unknown_branch") {
        // branch was closed down or removed since the panel opened: ask again
        setBranch(null);
        setNotice("That branch isn't available any more. Please pick another.");
        setPhase("picking");
        return;
      }
      setMessages((m) => [...m, msg({ role: "assistant", text: errorText(e), error: true, retry: question })]);
    } finally {
      setBusy(false);
      setProgress(null);
    }
  }

  function retry(question: string) {
    setMessages((m) => {
      // drop the failed question and its error; send() adds the question back
      const i = m.map((x) => x.role === "user" && x.text === question).lastIndexOf(true);
      return i >= 0 ? m.slice(0, i) : m;
    });
    void send(question);
  }

  const say = (text: string, error = false) => setMessages((m) => [...m, msg({ role: "assistant", text, error })]);
  const subtotalLine = (c: Cart | null) =>
    c ? ` Subtotal: Rs ${c.subtotal.toLocaleString("en-US")} (tax and delivery are added in the app at checkout).` : "";

  /** Run a cart call for the current branch; drop its result if the customer switched branch meanwhile. */
  async function cartCall<T extends { cart: Cart | null }>(call: (branchId: string) => Promise<T>): Promise<T | null> {
    if (!branch) return null;
    const branchId = branch.branch_id;
    const r = await call(branchId);
    if (!isCurrent(branchId)) return null;
    setCart(r.cart);
    return r;
  }

  // Card buttons: straight to the cart API, no LLM.
  async function addToCart(item: Item, size?: string) {
    if (!branch || busy) return;
    setBusy(true);
    try {
      const r = await cartCall((b) => api.cartAdd(b, item.item_id, size));
      if (!r) return;
      if (r.added?.length) {
        const a = r.added[0];
        say(`Added ${a.qty} × ${a.name}${a.size ? ` (${a.size})` : ""} to your cart.${subtotalLine(r.cart)}`);
        setSuggestions(["Show my cart", "Show deals"]);
      } else {
        say(r.problems?.some((p) => p.error === "not_available_at_branch")
          ? `Sorry, ${item.name} isn't available at this branch right now.`
          : `I couldn't add ${item.name} to your cart. Please try again.`, true);
      }
    } catch (e) {
      say(errorText(e), true);
    } finally {
      setBusy(false);
    }
  }

  async function showCart() {
    try {
      const r = await cartCall((b) => api.cartGet(b));
      if (!r) return;
      if (r.cart && r.cart.lines.length) {
        setCartOpen(true);
        const lines = r.cart.lines.map((l) => `- ${l.qty} × ${l.name}${l.size ? ` (${l.size})` : ""}` +
          (l.available && l.line_total != null ? ` – Rs ${l.line_total.toLocaleString("en-US")}` : " – not available now"));
        say(`Your cart:\n\n${lines.join("\n")}\n\n${subtotalLine(r.cart).trim()}`);
      } else {
        say("Your cart is empty. Tap “Add to cart” on any item, or tell me what you'd like.");
      }
    } catch (e) {
      say(errorText(e), true);
    }
  }

  async function changeQty(lineNo: number, qty: number) {
    try {
      await cartCall((b) => (qty < 1 ? api.cartRemove(b, lineNo) : api.cartQuantity(b, lineNo, qty)));
    } catch (e) {
      say(errorText(e), true);
    }
  }

  async function choosePayment(code: string) {
    try {
      await cartCall((b) => api.setPayment(b, code));
    } catch (e) {
      say(
        e instanceof ApiError && e.status === 409
          ? "That payment option isn't available at this branch right now. Please pick another."
          : errorText(e),
        true,
      );
    }
  }

  async function reviewOrder() {
    if (!branch) return;
    if (!cart?.lines.length) {
      say("Your cart is empty. Add something first, then you can review and confirm your order.");
      return;
    }
    const branchId = branch.branch_id;
    setCartOpen(true);
    try {
      const r = await api.reviewOrder(branchId);
      if (isCurrent(branchId)) setReview(r);
    } catch (e) {
      say(errorText(e), true);
    }
  }

  /** The customer's explicit tap: hand the cart to the order backend and show what it answered. */
  async function confirmOrder() {
    if (!branch || !review?.confirm_token || ordering) return;
    const branchId = branch.branch_id;
    setOrdering(true);
    try {
      const r = await api.confirmOrder(branchId, review.confirm_token);
      if (!isCurrent(branchId)) return;
      setCart(r.cart);
      setReview(null);
      if (r.status === "placed") {
        setCartOpen(false);
        say(`Your order is placed${r.order_no ? ` (order ${r.order_no})` : ""}. ${r.message ?? "You can track it in the app."}`);
        setSuggestions(["Where is my order?"]);
      } else if (r.status === "not_connected") {
        say("Your cart and payment choice are saved. Please complete your order in the app to place it.");
      } else if (r.status === "rejected") {
        say(r.message ?? "The order couldn't be placed. Please check your cart and try again.", true);
      } else {
        say("We couldn't reach our ordering system. Your cart is saved, so please try again in a moment.", true);
      }
    } catch (e) {
      const code = e instanceof ApiError ? e.message : "";
      if (code === "cart_changed" || code === "expired") {
        say("Your cart changed since you reviewed it. Please review it again before confirming.", true);
        setReview(null);
      } else {
        say(errorText(e), true);
      }
    } finally {
      setOrdering(false);
    }
  }

  async function clearCart() {
    try {
      await cartCall((b) => api.clearCart(b));
    } catch {
      /* leave the cart shown; the next message will return the real one */
    }
  }

  function newChat() {
    if (branch) save(`session:${branch.branch_id}`, null);
    resetConversation();
  }

  const side = config.position === "left" ? "left" : "right";
  const cartCount = cart?.item_count ?? 0;

  return (
    <div className={`cz cz-${side}`} style={{ ["--cz-accent" as any]: config.color }}>
      {open && (
        <section
          className="cz-panel"
          role="dialog"
          aria-label={config.title}
          onKeyDown={(e) => e.key === "Escape" && setOpen(false)}
        >
          <header className="cz-header">
            <div className="cz-header-text">
              <div className="cz-title">{config.title}</div>
              {branch && phase === "ready" && (
                <button className="cz-branch" onClick={() => setPhase("picking")} title="Change branch">
                  <span className={`cz-dot ${status ? (status.open_now ? "is-open" : "is-closed") : ""}`} />
                  {branch.name}
                  {status && <span className="cz-muted"> · {status.open_now ? "open now" : "closed now"}</span>}
                  <span className="cz-caret" aria-hidden>▾</span>
                </button>
              )}
            </div>
            {branch && phase === "ready" && (
              <button className="cz-icon" onClick={newChat} aria-label="New chat" title="New chat">
                <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden>
                  <path d="M12 5v14M5 12h14" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                </svg>
              </button>
            )}
            <button className="cz-icon" onClick={() => setOpen(false)} aria-label="Close chat" title="Close">
              <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden>
                <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
              </svg>
            </button>
          </header>

          {phase === "locating" ? (
            <div className="cz-locating" role="status">
              <span className="cz-typing" aria-hidden>
                <span />
                <span />
                <span />
              </span>
              Finding the branch that delivers to you…
              <button
                className="cz-link"
                onClick={() => {
                  decision.current++; // stop waiting for the location
                  setPhase("picking");
                }}
              >
                Pick a branch myself
              </button>
            </div>
          ) : phase === "picking" || !branch ? (
            <BranchPicker
              api={api}
              current={branch}
              notice={notice}
              nearby={nearby}
              lastUsed={load("branch")}
              switching={switching}
              onPick={chooseBranch}
              onCancel={branch ? () => { setNotice(null); setPhase("ready"); } : undefined}
            />
          ) : (
            <>
              <div className="cz-messages" ref={listRef} aria-live="polite">
                {messages.map((m) => (
                  <MessageView
                    key={m.id}
                    message={m}
                    disabled={busy}
                    onRetry={retry}
                    onAdd={(item, size) => void addToCart(item, size)}
                  />
                ))}
                {busy && (
                  <div className="cz-row cz-row-assistant">
                    <div className="cz-bubble cz-working" aria-label={progress ?? "Assistant is typing"}>
                      <span className="cz-typing" aria-hidden>
                        <span />
                        <span />
                        <span />
                      </span>
                      {progress && <span className="cz-progress">{progress}</span>}
                    </div>
                  </div>
                )}
              </div>

              {suggestions.length > 0 && !busy && (
                <div className="cz-suggestions">
                  {suggestions.map((s) => (
                    <button
                      key={s}
                      className="cz-chip"
                      dir="auto"
                      onClick={() =>
                        void (/^show my cart$/i.test(s) ? showCart() : /^review & confirm/i.test(s) ? reviewOrder() : send(s))
                      }
                    >
                      {s}
                    </button>
                  ))}
                </div>
              )}

              {cart && cart.lines.length > 0 && (
                <CartBar
                  cart={cart}
                  expanded={cartOpen}
                  onToggle={() => setCartOpen((o) => !o)}
                  onClear={clearCart}
                  methods={payMethods}
                  onQty={(n, qty) => void changeQty(n, qty)}
                  onPay={(code) => void choosePayment(code)}
                  review={review}
                  ordering={ordering}
                  onReview={() => void reviewOrder()}
                  onConfirm={() => void confirmOrder()}
                />
              )}

              <form
                className="cz-input"
                onSubmit={(e) => {
                  e.preventDefault();
                  void send(input);
                }}
              >
                <textarea
                  ref={inputRef}
                  rows={1}
                  dir="auto"
                  value={input}
                  maxLength={1000}
                  placeholder="Type your question…"
                  aria-label="Message"
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                      e.preventDefault();
                      void send(input);
                    }
                  }}
                />
                <button type="submit" className="cz-send" disabled={busy || !input.trim()} aria-label="Send">
                  <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden>
                    <path d="M4 12l16-8-6 16-2.5-6.5L4 12z" fill="currentColor" />
                  </svg>
                </button>
              </form>
              <div className="cz-footer">Answers use live menu prices. Orders are placed in the app.</div>
            </>
          )}
        </section>
      )}

      <button
        className="cz-launcher"
        onClick={() => setOpen((o) => !o)}
        aria-label={open ? "Close chat" : `Open ${config.title}`}
        aria-expanded={open}
      >
        {open ? (
          <svg viewBox="0 0 24 24" width="24" height="24" aria-hidden>
            <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
          </svg>
        ) : (
          <svg viewBox="0 0 24 24" width="26" height="26" aria-hidden>
            <path
              d="M4 5.5A2.5 2.5 0 016.5 3h11A2.5 2.5 0 0120 5.5v8a2.5 2.5 0 01-2.5 2.5H10l-4.2 3.6A.5.5 0 015 19.2V16h0a2.5 2.5 0 01-1-2V5.5z"
              fill="currentColor"
            />
          </svg>
        )}
        {!open && cartCount > 0 && <span className="cz-badge">{cartCount}</span>}
      </button>
    </div>
  );
}
