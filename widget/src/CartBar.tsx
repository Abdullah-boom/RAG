import { useState } from "react";
import type { Cart } from "./api";
import { rs } from "./format";

export default function CartBar({ cart, onClear }: { cart: Cart; onClear: () => void }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className={`cz-cart ${expanded ? "is-expanded" : ""}`}>
      <button className="cz-cart-summary" onClick={() => setExpanded((e) => !e)} aria-expanded={expanded}>
        <span>
          Cart · {cart.item_count} {cart.item_count === 1 ? "item" : "items"}
        </span>
        <span className="cz-cart-total">
          {rs(cart.subtotal)} <span aria-hidden>{expanded ? "▾" : "▴"}</span>
        </span>
      </button>
      {expanded && (
        <div className="cz-cart-body">
          {cart.lines.map((l) => (
            <div key={l.line_no} className={`cz-cart-line ${l.available ? "" : "is-unavailable"}`}>
              <span dir="auto">
                {l.qty} × {l.name}
                {l.size ? ` (${l.size})` : ""}
              </span>
              <span>{l.available && l.line_total != null ? rs(l.line_total) : "not available"}</span>
            </div>
          ))}
          <div className="cz-cart-note">Subtotal before tax and delivery. This cart is not an order.</div>
          <button className="cz-link" onClick={onClear}>
            Clear cart
          </button>
        </div>
      )}
    </div>
  );
}
