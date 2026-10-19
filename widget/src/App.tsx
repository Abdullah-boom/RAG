import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  createApi,
  type Branch,
  type BranchStatus,
  type Cart,
  type Item,
  type OrderTypeResponse,
  type OrderView,
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

const STARTERS = ["Show deals", "Kuch teekha 800 se kam", "Payment options"];
const WELCOME =
  "Hi! Ask me about the menu, prices, deals or branch timings, in English, Roman Urdu or اردو. " +
  "I can build your cart, take your payment choice and place your order.";
const PLACE_ORDER = "Place my order";

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

// choosing: delivery or pickup? locating: finding the branch that delivers to the customer. picking: a pickup branch.
type Phase = "choosing" | "locating" | "picking" | "ready";

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
  if (code === 1) return "Location is off for this site. Turn it on for delivery, or choose pickup.";
  return "We couldn't get your location for delivery. Try again, or choose pickup.";
}

function orderTypeErrorText(e: unknown): string {
  const code = e instanceof ApiError ? e.message : "";
  if (code === "no_coverage") return "We don't deliver to your current location yet. You can choose pickup instead.";
  if (code === "branch_closed") return "That branch is closed right now. Please choose another option.";
  if (code === "pickup_not_offered") return "That branch doesn't offer pickup. Please pick another branch.";
  return errorText(e);
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
  const [phase, setPhase] = useState<Phase>("choosing");
  const [notice, setNotice] = useState<string | null>(null); // why the chooser / picker is showing
  const [switching, setSwitching] = useState(false);
  const [order, setOrder] = useState<OrderView | null>(null); // delivery / pickup and payment, shown like the cart
  const [status, setStatus] = useState<BranchStatus | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [suggestions, setSuggestions] = useState<string[]>(STARTERS);
  const [cart, setCart] = useState<Cart | null>(null);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  const [cartOpen, setCartOpen] = useState(false);
  const [input, setInput] = useState("");
  // The bot message whose order summary can be confirmed with the buttons; any other step clears it.
  const [confirmId, setConfirmId] = useState<number | null>(null);
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
    setConfirmId(null);
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

  /** The server set the order type and branch: show that branch, its cart and the order choices. */
  const applyOrder = useCallback(
    (r: OrderTypeResponse, ticket: number) => {
      const changedBranch = branchRef.current?.branch_id !== r.branch.branch_id;
      setBranch(r.branch);
      setStatus(r.status);
      setCart(r.cart);
      setOrder(r.order);
      setNotice(null);
      setPhase("ready");
      save("branch", r.branch.branch_id); // only shown as "last used" in the picker
      if (changedBranch) {
        setCartOpen(false);
        restoreConversation(r.branch, ticket); // conversations are per branch
      }
    },
    [restoreConversation],
  );

  /** Delivery / pickup couldn't be set: back to the chooser with the reason (the current order, if any, stays;
   *  "Back to chat" returns to it). */
  const failSwitch = useCallback((text: string) => {
    setNotice(text);
    setPhase("choosing");
  }, []);

  /** Delivery: the branch whose delivery area contains the customer's current location. */
  const startDelivery = useCallback(async () => {
    const ticket = ++decision.current;
    const from = branchRef.current?.branch_id ?? null;
    setPhase("locating");
    setNotice(null);
    let pos: GeolocationPosition;
    try {
      pos = await currentPosition();
    } catch (e) {
      if (decision.current === ticket) failSwitch(locationErrorText(e));
      return;
    }
    if (decision.current !== ticket) return;
    try {
      const r = await api.setOrderType("DELIVERY", {
        lat: pos.coords.latitude,
        lng: pos.coords.longitude,
        from_branch_id: from,
      });
      if (decision.current === ticket) applyOrder(r, ticket);
    } catch (e) {
      if (decision.current === ticket) failSwitch(orderTypeErrorText(e));
    }
  }, [api, applyOrder, failSwitch]);

  /** Pickup: the customer picks one of the open branches that offer it. */
  const choosePickupBranch = useCallback(
    async (b: Branch) => {
      const ticket = ++decision.current;
      setSwitching(true);
      try {
        const r = await api.setOrderType("PICKUP", { branch_id: b.branch_id, from_branch_id: branchRef.current?.branch_id });
        if (decision.current === ticket) applyOrder(r, ticket);
      } catch (e) {
        if (decision.current === ticket) setNotice(orderTypeErrorText(e));
      } finally {
        if (decision.current === ticket) setSwitching(false);
      }
    },
    [api, applyOrder],
  );

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
          if (b)
            api.cartGet(b.branch_id).then(
              (r) => {
                if (!isCurrent(b.branch_id)) return;
                setCart(r.cart);
                setOrder(r.order);
              },
              () => setCart(null),
            );
        }
      },
    };
  }, [api, resetConversation]);

  // The first question on opening: delivery or pickup? (Kept while the page is open; changeable any time.)
  useEffect(() => {
    if (open && !branchRef.current) setPhase("choosing");
  }, [open]);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, busy]);

  useEffect(() => {
    if (open && phase === "ready") inputRef.current?.focus();
  }, [open, phase]);

  async function send(text: string) {
    const question = text.trim();
    if (!question || busy || !branch) return;
    const b = branch;
    setInput("");
    setSuggestions([]);
    setConfirmId(null); // a typed "yes" still works (the server checks it); the buttons belong to the summary only
    setMessages((m) => [...m.filter((x) => !x.error), msg({ role: "user", text: question })]);
    setBusy(true);
    setProgress(null);
    try {
      const r = await api.chatStream(question, b.branch_id, sessionId, setProgress);
      save(`session:${b.branch_id}`, r.session_id);
      if (!isCurrent(b.branch_id)) return; // switched branch while this was running
      setSessionId(r.session_id);
      const reply = msg({ role: "assistant", text: r.answer, items: r.items });
      setMessages((m) => [...m, reply]);
      setConfirmId(r.confirm ? reply.id : null);
      setSuggestions(r.suggestions);
      setCart(r.cart);
      if (r.order) setOrder(r.order);
      // A switch the bot can't finish alone: delivery needs the location, pickup here may need another branch.
      if (r.action?.type === "need_location") void startDelivery();
      else if (r.action?.type === "pick_pickup_branch") {
        setNotice(`${b.name} doesn't offer pickup. Pick a branch for pickup:`);
        setPhase("picking");
      }
    } catch (e) {
      if (e instanceof ApiError && e.message === "unknown_branch") {
        // branch was closed down or removed since the panel opened: ask again
        setBranch(null);
        setNotice("That branch isn't available any more. Please choose delivery or pickup again.");
        setPhase("choosing");
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
    setConfirmId(null); // the summary was for the cart as it was
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

  /** Confirm order / Cancel under the summary: the customer's own tap, sent straight to the server. */
  async function answerSummary(answer: "yes" | "no") {
    if (!branch || !sessionId || busy) return;
    const b = branch;
    setConfirmId(null);
    setSuggestions([]);
    setMessages((m) => [...m, msg({ role: "user", text: answer === "yes" ? "Confirm order" : "Cancel" })]);
    setBusy(true);
    setProgress(answer === "yes" ? "Placing your order…" : null);
    try {
      const r = await api.chatConfirm(b.branch_id, sessionId, answer);
      if (!isCurrent(b.branch_id)) return;
      const reply = msg({ role: "assistant", text: r.answer });
      setMessages((m) => [...m, reply]);
      setConfirmId(r.confirm ? reply.id : null);
      setSuggestions(r.suggestions);
      setCart(r.cart);
      if (r.order) setOrder(r.order);
    } catch (e) {
      say(
        e instanceof ApiError && e.message === "nothing_to_confirm"
          ? "That summary is out of date. Say “place my order” to see your order again."
          : errorText(e),
        true,
      );
    } finally {
      setBusy(false);
      setProgress(null);
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
                <button className="cz-branch" onClick={() => setPhase("choosing")} title="Change delivery / pickup">
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

          {phase === "choosing" ? (
            <div className="cz-choose">
              <div className="cz-bubble cz-choose-ask">How would you like to get your order?</div>
              {notice && (
                <div className="cz-notice" role="status">
                  {notice}
                </div>
              )}
              <div className="cz-choose-options">
                <button
                  className={`cz-choose-btn ${order?.type === "DELIVERY" ? "is-current" : ""}`}
                  onClick={() => void startDelivery()}
                >
                  <strong>Delivery</strong>
                  <span>To your current location</span>
                </button>
                <button
                  className={`cz-choose-btn ${order?.type === "PICKUP" ? "is-current" : ""}`}
                  onClick={() => {
                    setNotice(null);
                    setPhase("picking");
                  }}
                >
                  <strong>Pickup</strong>
                  <span>From an open branch</span>
                </button>
              </div>
              {branch && (
                <button className="cz-picker-cancel" onClick={() => { setNotice(null); setPhase("ready"); }}>
                  Back to chat
                </button>
              )}
            </div>
          ) : phase === "locating" ? (
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
                  setNotice(null);
                  setPhase("picking");
                }}
              >
                Choose pickup instead
              </button>
            </div>
          ) : phase === "picking" || !branch ? (
            <BranchPicker
              api={api}
              current={order?.type === "PICKUP" ? branch : null}
              notice={notice}
              lastUsed={load("branch")}
              switching={switching}
              onPick={(b) => void choosePickupBranch(b)}
              onCancel={() => { setNotice(null); setPhase(branch ? "ready" : "choosing"); }}
            />
          ) : (
            <>
              <div className="cz-orderbar" aria-label="Your order">
                <span>
                  <strong>{order?.type_name ?? "Delivery or pickup?"}</strong> · {branch.name}
                </span>
                <span className={order?.payment_method ? "" : "cz-muted"}>
                  {order?.payment_method ? `Pay: ${order.payment_method.name}` : "Payment not chosen"}
                </span>
              </div>
              <div className="cz-messages" ref={listRef} aria-live="polite">
                {messages.map((m) => (
                  <MessageView
                    key={m.id}
                    message={m}
                    disabled={busy}
                    onRetry={retry}
                    onAdd={(item, size) => void addToCart(item, size)}
                    confirm={
                      m.id === confirmId
                        ? { onConfirm: () => void answerSummary("yes"), onCancel: () => void answerSummary("no") }
                        : undefined
                    }
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
                        void (/^show my cart$/i.test(s) ? showCart() : send(s))
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
                  busy={busy}
                  onToggle={() => setCartOpen((o) => !o)}
                  onClear={clearCart}
                  onQty={(n, qty) => void changeQty(n, qty)}
                  onPlace={() => {
                    setCartOpen(false);
                    void send(PLACE_ORDER);
                  }}
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
              <div className="cz-footer">Live menu prices. An order is placed only when you reply “yes” to its summary.</div>
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
