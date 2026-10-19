import type { Cart } from "./api";
import { rs } from "./format";

type Props = {
  cart: Cart;
  expanded: boolean;
  busy: boolean;
  onToggle: () => void;
  onClear: () => void;
  onQty: (lineNo: number, qty: number) => void; // qty 0 removes the line
  onPlace: () => void; // asks the bot to place the order: it shows the summary and waits for "yes"
};

export default function CartBar({ cart, expanded, busy, onToggle, onClear, onQty, onPlace }: Props) {
  // The payment method is chosen in chat ("cash on delivery karunga"); the cart only shows it.
  const chosen = cart.checkout?.payment_method ?? null;
  return (
    <div className={`cz-cart ${expanded ? "is-expanded" : ""}`}>
      <button className="cz-cart-summary" onClick={onToggle} aria-expanded={expanded}>
        <span>
          Cart · {cart.item_count} {cart.item_count === 1 ? "item" : "items"}
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
          {chosen ? (
            <div className="cz-cart-line">
              <span>
                {chosen.name}
                {chosen.tax_rate != null && <span className="cz-muted"> · {chosen.tax_rate}% tax</span>}
              </span>
              <span>{chosen.total != null ? rs(chosen.total) : ""}</span>
            </div>
          ) : (
            <div className="cz-cart-note">Payment not chosen yet: tell me how you'd like to pay.</div>
          )}

          <div className="cz-cart-note">
            {cart.checkout?.note ?? "Estimate before delivery fee and discounts."} This cart is not an order yet.
          </div>
          {chosen?.pay_now && <div className="cz-cart-note">🔒 {cart.checkout?.secure_note}</div>}

          <button className="cz-review-btn" onClick={onPlace} disabled={busy}>
            Place order
          </button>
          <button className="cz-link" onClick={onClear}>
            Clear cart
          </button>
        </div>
      )}
    </div>
  );
}
