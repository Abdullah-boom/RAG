import { useState } from "react";
import Markdown from "react-markdown";
import type { Message } from "./App";
import type { Item } from "./api";
import { priceLabel, rs } from "./format";

type OnAdd = (item: Item, size?: string) => void;
type Props = { message: Message; disabled: boolean; onRetry: (q: string) => void; onAdd: OnAdd };

// react-markdown renders no raw HTML, so model output can't inject markup into the host page.
const mdComponents = {
  a: (props: React.AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a href={props.href} target="_blank" rel="noopener noreferrer">
      {props.children}
    </a>
  ),
};

function ItemCard({ item, disabled, onAdd }: { item: Item; disabled: boolean; onAdd: OnAdd }) {
  const [imgOk, setImgOk] = useState(Boolean(item.image_url));
  const [picking, setPicking] = useState(false);
  const sizes = item.sizes ?? [];
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
        {(item.type || item.category) && (
          <div className="cz-card-cat">
            {/* what it is first ("Pizza"), then the menu range customers may recognise */}
            {[item.type && item.type[0].toUpperCase() + item.type.slice(1), item.category].filter(Boolean).join(" · ")}
          </div>
        )}
        <div className="cz-card-price">{priceLabel(item)}</div>
        {available && picking ? (
          <div className="cz-sizes" role="group" aria-label={`Choose a size for ${item.name}`}>
            {sizes.map((s) => (
              <button
                key={s.size}
                className="cz-size"
                disabled={disabled}
                onClick={() => {
                  setPicking(false);
                  onAdd(item, s.size);
                }}
              >
                {s.size} <span className="cz-muted">{rs(s.price)}</span>
              </button>
            ))}
          </div>
        ) : available ? (
          <button
            className="cz-card-add"
            disabled={disabled}
            onClick={() => (sizes.length > 1 ? setPicking(true) : onAdd(item, sizes[0]?.size))}
          >
            Add to cart
          </button>
        ) : (
          <div className="cz-card-na">Not available now</div>
        )}
      </div>
    </div>
  );
}

export default function MessageView({ message, disabled, onRetry, onAdd }: Props) {
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
            <ItemCard key={`${it.item_id}-${it.name}`} item={it} disabled={disabled} onAdd={onAdd} />
          ))}
        </div>
      )}
    </>
  );
}
