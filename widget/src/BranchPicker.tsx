import { useEffect, useMemo, useState } from "react";
import type { Api, Branch } from "./api";

type Props = { api: Api; current: Branch | null; onPick: (b: Branch) => void; onCancel?: () => void };

export default function BranchPicker({ api, current, onPick, onCancel }: Props) {
  const [branches, setBranches] = useState<Branch[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [query, setQuery] = useState("");

  const fetchBranches = () => {
    setFailed(false);
    api.branches().then(setBranches, () => setFailed(true));
  };
  useEffect(fetchBranches, [api]);

  const groups = useMemo(() => {
    const q = query.trim().toLowerCase();
    const map = new Map<string, Branch[]>();
    for (const b of branches ?? []) {
      if (q && !`${b.name} ${b.city ?? ""}`.toLowerCase().includes(q)) continue;
      const city = b.city || "Other";
      map.set(city, [...(map.get(city) ?? []), b]);
    }
    return [...map.entries()];
  }, [branches, query]);

  return (
    <div className="cz-picker">
      <div className="cz-picker-head">
        <div className="cz-picker-title">Pick your branch</div>
        <div className="cz-muted">Prices, deals and timings depend on the branch.</div>
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
      <div className="cz-picker-list">
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
        {groups.map(([city, list]) => (
          <div key={city} className="cz-city">
            <div className="cz-city-name">{city}</div>
            {list.map((b) => (
              <button
                key={b.branch_id}
                className={`cz-branch-option ${current?.branch_id === b.branch_id ? "is-current" : ""}`}
                onClick={() => onPick(b)}
              >
                {b.name}
                {current?.branch_id === b.branch_id && <span className="cz-muted"> · current</span>}
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
