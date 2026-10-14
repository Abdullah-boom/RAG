import type { Cart, OrderProblem, OrderReview, PaymentMethod } from "./api";
import { rs } from "./format";

type Props = {
  cart: Cart;
  expanded: boolean;
  methods: PaymentMethod[] | null; // loaded when the cart opens; null while loading
  onToggle: () => void;
  onClear: () => void;
  onQty: (lineNo: number, qty: number) => void; // qty 0 removes the line
  onPay: (code: string) => void;
  review: OrderReview | null; // shown after "Review & confirm order"
  ordering: boolean;
  onReview: () => void;
  onConfirm: () => void;
};

function problemText(p: OrderProblem): string {
  switch (p.code) {
    case "empty_cart":
      return "Your cart is empty.";
    case "unavailable_items":
      return `Not available right now: ${(p.items ?? []).join(", ")}. Remove them first.`;
    case "branch_closed":
      return "This branch isn't taking orders right now.";
    case "below_minimum_order":
      return `The minimum order here is ${rs(p.minimum ?? 0)}.`;
    case "no_payment_method":
      return "Choose how you'll pay.";
    case "payment_method_unavailable":
      return "Your payment choice isn't available any more. Please pick another.";
    case "login_required":
      return "Please log in to place your order.";
    default:
      return "Something needs fixing before you can order.";
  }
}

export default function CartBar(props: Props) {
  const { cart, expanded, methods, onToggle, onClear, onQty, onPay, review, ordering, onReview, onConfirm } = props;
  const chosen = cart.checkout?.payment_method ?? null;
  const cheapest = methods?.length ? Math.min(...methods.map((m) => m.total ?? Infinity)) : null;
  return (
    <div className={`cz-cart ${expanded ? "is-expanded" : ""}`}>
      <button className="cz-cart-summary" onClick={onToggle} aria-expanded={expanded}>
        <span>
          Cart · {cart.item_count} {cart.item_count === 1 ? "item" : "items"}
          {chosen && <span className="cz-muted"> · {chosen.name}</span>}
        </span>
        <span className="cz-cart-total">
          {rs(chosen?.total ?? cart.subtotal)} <span aria-hidden>{expanded ? "▾" : "▴"}</span>
        </span>
      </button>
      {expanded && (
        <div className="cz-cart-body">
          {cart.lines.map((l) => (
            <div key={l.line_no} className={`cz-cart-line ${l.available ? "" : "is-unavailable"}`}>
              <span className="cz-qty">
                <button className="cz-qty-btn" onClick={() => onQty(l.line_no, l.qty - 1)} aria-label={`One less ${l.name}`}>
                  −
                </button>
                <span aria-label="quantity">{l.qty}</span>
                <button className="cz-qty-btn" onClick={() => onQty(l.line_no, l.qty + 1)} aria-label={`One more ${l.name}`}>
                  +
                </button>
                <span dir="auto">
                  {l.name}
                  {l.size ? ` (${l.size})` : ""}
                </span>
              </span>
              <span className="cz-cart-right">
                {l.available && l.line_total != null ? rs(l.line_total) : "not available"}
                <button className="cz-remove" onClick={() => onQty(l.line_no, 0)} aria-label={`Remove ${l.name}`}>
                  ×
                </button>
              </span>
            </div>
          ))}
          <div className="cz-cart-line">
            <span>Subtotal</span>
            <span>{rs(cart.subtotal)}</span>
          </div>

          <fieldset className="cz-pay">
            <legend>How will you pay?</legend>
            {methods === null && <div className="cz-muted">Loading payment options…</div>}
            {methods?.length === 0 && <div className="cz-muted">Payment options are shown at checkout in the app.</div>}
            {methods?.map((m) => (
              <label key={m.code} className={`cz-pay-option ${chosen?.code === m.code ? "is-chosen" : ""}`}>
                <input type="radio" name="cz-pay" checked={chosen?.code === m.code} onChange={() => onPay(m.code)} />
                <span className="cz-pay-name">
                  {m.name}
                  {m.when !== "any" && <span className="cz-muted"> · {m.when}</span>}
                  {m.total != null && m.total === cheapest && methods.some((o) => (o.total ?? 0) > m.total!) && (
                    <span className="cz-pay-save"> lower tax</span>
                  )}
                </span>
                <span className="cz-pay-total">
                  {m.total != null ? rs(m.total) : ""}
                  {m.tax_rate != null && <span className="cz-muted"> incl. {m.tax_rate}% tax</span>}
                </span>
              </label>
            ))}
          </fieldset>

          <div className="cz-cart-note">
            {cart.checkout?.note ?? "Estimate before delivery fee and discounts."} This cart is not an order.
          </div>
          {chosen?.pay_now && <div className="cz-cart-note">🔒 {cart.checkout?.secure_note}</div>}

          {review && !review.ready && (
            <ul className="cz-problems" role="alert">
              {review.problems.map((p) => (
                <li key={p.code}>{problemText(p)}</li>
              ))}
            </ul>
          )}
          {review?.ready ? (
            <div className="cz-confirm">
              <div>
                Order {cart.item_count} {cart.item_count === 1 ? "item" : "items"} ·{" "}
                {review.summary.payment_method?.name} ·{" "}
                <strong>{rs(review.summary.payment_method?.total ?? review.summary.subtotal)}</strong> (estimate)
              </div>
              <button className="cz-confirm-btn" onClick={onConfirm} disabled={ordering}>
                {ordering ? "Placing order…" : "Confirm order"}
              </button>
            </div>
          ) : (
            <button className="cz-review-btn" onClick={onReview} disabled={ordering}>
              Review &amp; confirm order
            </button>
          )}
          <button className="cz-link" onClick={onClear}>
            Clear cart
          </button>
        </div>
      )}
    </div>
  );
}
