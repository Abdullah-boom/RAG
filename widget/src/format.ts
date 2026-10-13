import type { Item } from "./api";

export const rs = (n: number) => `Rs ${n.toLocaleString("en-US")}`;

export function priceLabel(item: Item): string {
  if (item.price != null) return rs(item.price);
  if (item.sizes?.length) {
    const min = Math.min(...item.sizes.map((s) => s.price));
    return item.sizes.length > 1 ? `from ${rs(min)}` : rs(min);
  }
  if (item.price_from != null) return `from ${rs(item.price_from)}`;
  return "";
}
