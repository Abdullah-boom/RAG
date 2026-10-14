import { useEffect, useMemo, useState } from "react";
import type { Api, Branch } from "./api";

type Props = {
  api: Api;
  current: Branch | null;
  notice: string | null; // why the picker is showing (location off, no branch delivers here, ...)
  nearby: string[]; // branches whose delivery area contains the customer, listed first
  lastUsed: string | null;
  switching: boolean;
  onPick: (b: Branch) => void;
  onCancel?: () => void;
};

export default function BranchPicker({ api, current, notice, nearby, lastUsed, switching, onPick, onCancel }: Props) {
  const [branches, setBranches] = useState<Branch[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [query, setQuery] = useState("");

  const fetchBranches = () => {
    setFailed(false);
    api.branches().then(setBranches, () => setFailed(true));
  };
  // Refetch whenever the picker opens or its notice changes: open/closed changes during the day.
  useEffect(fetchBranches, [api, notice]);

  const groups = useMemo(() => {
    const q = query.trim().toLowerCase();
    const map = new Map<string, Branch[]>();
    const near = (branches ?? []).filter((b) => nearby.includes(b.branch_id));
    if (near.length && !q) map.set("Delivers to you", near);
    for (const b of branches ?? []) {
      if (q && !`${b.name} ${b.city ?? ""}`.toLowerCase().includes(q)) continue;
      if (!q && nearby.includes(b.branch_id)) continue;
      const city = b.city || "Other";
      map.set(city, [...(map.get(city) ?? []), b]);
    }
    // open branches first inside each group
    return [...map.entries()].map(([g, list]) => [g, [...list].sort((a, b) => Number(b.open_now) - Number(a.open_now))] as const);
  }, [branches, query, nearby]);

  return (
    <div className="cz-picker">
      <div className="cz-picker-head">
        <div className="cz-picker-title">Pick your branch</div>
        {notice ? (
          <div className="cz-notice" role="status">
            {notice}
          </div>
        ) : (
          <div className="cz-muted">Prices, deals and timings depend on the branch.</div>
        )}
        <input
          className="cz-search"
          type="search"
          placeholder="Search city or area"
          aria-label="Search branches"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          autoFocus
        />
      </div>
      <div className="cz-picker-list" aria-busy={switching}>
        {failed && (
          <div className="cz-empty">
            Couldn't load branches.{" "}
            <button className="cz-link" onClick={fetchBranches}>
              Try again
            </button>
          </div>
        )}
        {!failed && !branches && <div className="cz-empty">Loading branches…</div>}
        {branches && groups.length === 0 && <div className="cz-empty">No branch matches “{query}”.</div>}
        {groups.map(([group, list]) => (
          <div key={group} className="cz-city">
            <div className="cz-city-name">{group}</div>
            {list.map((b) => (
              <button
                key={b.branch_id}
                className={`cz-branch-option ${current?.branch_id === b.branch_id ? "is-current" : ""}`}
                onClick={() => onPick(b)}
                disabled={!b.open_now || switching}
                title={b.open_now ? undefined : "Closed now"}
              >
                {b.name}
                {current?.branch_id === b.branch_id && <span className="cz-muted"> · current</span>}
                {current?.branch_id !== b.branch_id && lastUsed === b.branch_id && (
                  <span className="cz-muted"> · last used</span>
                )}
                {!b.open_now && <span className="cz-muted"> · closed now</span>}
              </button>
            ))}
          </div>
        ))}
      </div>
      {onCancel && (
        <button className="cz-picker-cancel" onClick={onCancel}>
          Back to chat
        </button>
      )}
    </div>
  );
}
