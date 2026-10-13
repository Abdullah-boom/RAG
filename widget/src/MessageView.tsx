import { useState } from "react";
import Markdown from "react-markdown";
import type { Message } from "./App";
import type { Item } from "./api";
import { priceLabel } from "./format";

type Props = { message: Message; disabled: boolean; onRetry: (q: string) => void; onAsk: (q: string) => void };

// react-markdown renders no raw HTML, so model output can't inject markup into the host page.
const mdComponents = {
  a: (props: React.AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a href={props.href} target="_blank" rel="noopener noreferrer">
      {props.children}
    </a>
  ),
};

function ItemCard({ item, disabled, onAsk }: { item: Item; disabled: boolean; onAsk: (q: string) => void }) {
  const [imgOk, setImgOk] = useState(Boolean(item.image_url));
  const available = item.available !== false;
  return (
    <div className={`cz-card ${available ? "" : "is-unavailable"}`}>
      {imgOk && item.image_url && (
        <img className="cz-card-img" src={item.image_url} alt="" loading="lazy" onError={() => setImgOk(false)} />
      )}
      <div className="cz-card-body">
        <div className="cz-card-name" dir="auto">
          {item.name}
        </div>
        {item.category && <div className="cz-card-cat">{item.category}</div>}
        <div className="cz-card-price">{priceLabel(item)}</div>
        {available ? (
          <button className="cz-card-add" disabled={disabled} onClick={() => onAsk(`Add 1 ${item.name} to my cart`)}>
            Add to cart
          </button>
        ) : (
          <div className="cz-card-na">Not available now</div>
        )}
      </div>
    </div>
  );
}

export default function MessageView({ message, disabled, onRetry, onAsk }: Props) {
  const { role, text, items, error, retry } = message;
  return (
    <>
      <div className={`cz-row cz-row-${role}`}>
        <div className={`cz-bubble ${error ? "is-error" : ""}`} dir="auto">
          {role === "assistant" && !error ? <Markdown components={mdComponents}>{text}</Markdown> : text}
          {error && retry && (
            <button className="cz-link cz-retry" onClick={() => onRetry(retry)} disabled={disabled}>
              Try again
            </button>
          )}
        </div>
      </div>
      {items && items.length > 0 && (
        <div className="cz-cards" role="list">
          {items.map((it) => (
            <ItemCard key={`${it.item_id}-${it.name}`} item={it} disabled={disabled} onAsk={onAsk} />
          ))}
        </div>
      )}
    </>
  );
}
