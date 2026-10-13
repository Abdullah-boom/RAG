import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, createApi, type Branch, type BranchStatus, type Cart, type Item } from "./api";
import { load, save } from "./storage";
import BranchPicker from "./BranchPicker";
import CartBar from "./CartBar";
import MessageView from "./MessageView";

export type WidgetConfig = { apiUrl: string; title: string; color: string; position: "left" | "right" };

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
  if (status === 429) return "You're sending messages quickly. Please wait a minute and try again.";
  if (status === 0) return "I couldn't reach the server. Check your connection and try again.";
  return "Something went wrong on our side. Please try again.";
}

export default function App({ config }: { config: WidgetConfig }) {
  const api = useMemo(() => createApi(config.apiUrl), [config.apiUrl]);
  const [open, setOpen] = useState(false);
  const [branch, setBranch] = useState<Branch | null>(() => {
    const saved = load("branch");
    try {
      return saved ? (JSON.parse(saved) as Branch) : null;
    } catch {
      return null;
    }
  });
  const [picking, setPicking] = useState(false);
  const [status, setStatus] = useState<BranchStatus | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [suggestions, setSuggestions] = useState<string[]>(STARTERS);
  const [cart, setCart] = useState<Cart | null>(null);
  const [busy, setBusy] = useState(false);
  const [input, setInput] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const restoredFor = useRef<string | null>(null);

  // Page API for the host site: e.g. an "Ask us" button can call window.CheeziousChat.open().
  useEffect(() => {
    (window as any).CheeziousChat = { open: () => setOpen(true), close: () => setOpen(false) };
  }, []);

  const resetConversation = useCallback(() => {
    setSessionId(null);
    setMessages([msg({ role: "assistant", text: WELCOME })]);
    setSuggestions(STARTERS);
    setCart(null);
  }, []);

  // When the panel opens on a branch: load its status and restore the saved conversation once.
  useEffect(() => {
    if (!open || !branch || restoredFor.current === branch.branch_id) return;
    restoredFor.current = branch.branch_id;
    api.branchStatus(branch.branch_id).then(setStatus, () => setStatus(null));
    const saved = load(`session:${branch.branch_id}`);
    resetConversation();
    if (!saved) return;
    api.session(branch.branch_id, saved).then(
      (s) => {
        setSessionId(s.session_id);
        if (s.messages.length) {
          setMessages([msg({ role: "assistant", text: WELCOME }), ...s.messages.map((m) => msg(m))]);
          setSuggestions([]);
        }
        setCart(s.cart);
      },
      () => save(`session:${branch.branch_id}`, null), // expired or unknown: start fresh
    );
  }, [open, branch, api, resetConversation]);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, busy]);

  useEffect(() => {
    if (open && branch && !picking) inputRef.current?.focus();
  }, [open, branch, picking]);

  function chooseBranch(b: Branch) {
    save("branch", JSON.stringify(b));
    setPicking(false);
    if (b.branch_id !== branch?.branch_id) {
      restoredFor.current = null; // prices and cart are per branch: load that branch's conversation
      setStatus(null);
      setBranch(b);
    }
  }

  async function send(text: string) {
    const question = text.trim();
    if (!question || busy || !branch) return;
    setInput("");
    setSuggestions([]);
    setMessages((m) => [...m.filter((x) => !x.error), msg({ role: "user", text: question })]);
    setBusy(true);
    try {
      const r = await api.chat(question, branch.branch_id, sessionId);
      setSessionId(r.session_id);
      save(`session:${branch.branch_id}`, r.session_id);
      setMessages((m) => [...m, msg({ role: "assistant", text: r.answer, items: r.items })]);
      setSuggestions(r.suggestions);
      setCart(r.cart);
    } catch (e) {
      if (e instanceof ApiError && e.message === "unknown_branch") {
        // saved branch was closed or removed since the last visit: ask again
        save("branch", null);
        setBranch(null);
        return;
      }
      setMessages((m) => [...m, msg({ role: "assistant", text: errorText(e), error: true, retry: question })]);
    } finally {
      setBusy(false);
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

  async function clearCart() {
    if (!branch || !sessionId) return;
    try {
      await api.clearCart(branch.branch_id, sessionId);
      setCart(null);
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
              {branch && (
                <button className="cz-branch" onClick={() => setPicking(true)} title="Change branch">
                  <span className={`cz-dot ${status ? (status.open_now ? "is-open" : "is-closed") : ""}`} />
                  {branch.name}
                  {status && <span className="cz-muted"> · {status.open_now ? "open now" : "closed now"}</span>}
                  <span className="cz-caret" aria-hidden>▾</span>
                </button>
              )}
            </div>
            {branch && !picking && (
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

          {!branch || picking ? (
            <BranchPicker
              api={api}
              current={branch}
              onPick={chooseBranch}
              onCancel={branch ? () => setPicking(false) : undefined}
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
                    onAsk={(q) => void send(q)}
                  />
                ))}
                {busy && (
                  <div className="cz-row cz-row-assistant">
                    <div className="cz-bubble cz-typing" aria-label="Assistant is typing">
                      <span />
                      <span />
                      <span />
                    </div>
                  </div>
                )}
              </div>

              {suggestions.length > 0 && !busy && (
                <div className="cz-suggestions">
                  {suggestions.map((s) => (
                    <button key={s} className="cz-chip" onClick={() => void send(s)} dir="auto">
                      {s}
                    </button>
                  ))}
                </div>
              )}

              {cart && cart.lines.length > 0 && <CartBar cart={cart} onClear={clearCart} />}

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
