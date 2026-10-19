import { useEffect, useMemo, useState } from "react";
import type { Api, Branch } from "./api";

type Props = {
  api: Api;
  current: Branch | null; // the current pickup branch, if any
  notice: string | null; // why the picker is showing (e.g. this branch has no pickup)
  lastUsed: string | null;
  switching: boolean;
  onPick: (b: Branch) => void;
  onCancel?: () => void;
};

/** Pickup branches: only the ones open right now that offer pickup. */
export default function BranchPicker({ api, current, notice, lastUsed, switching, onPick, onCancel }: Props) {
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
    for (const b of branches ?? []) {
      if (!b.open_now || !b.pickup) continue;
      if (q && !`${b.name} ${b.city ?? ""}`.toLowerCase().includes(q)) continue;
      const city = b.city || "Other";
      map.set(city, [...(map.get(city) ?? []), b]);
    }
    return [...map.entries()];
  }, [branches, query]);

  return (
    <div className="cz-picker">
      <div className="cz-picker-head">
        <div className="cz-picker-title">Pick a branch for pickup</div>
        {notice ? (
          <div className="cz-notice" role="status">
            {notice}
          </div>
        ) : (
          <div className="cz-muted">Branches open now that offer pickup. Prices and deals depend on the branch.</div>
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
        {branches && groups.length === 0 && (
          <div className="cz-empty">
            {query ? `No open pickup branch matches “${query}”.` : "No branch is open for pickup right now."}
          </div>
        )}
        {groups.map(([group, list]) => (
          <div key={group} className="cz-city">
            <div className="cz-city-name">{group}</div>
            {list.map((b) => (
              <button
                key={b.branch_id}
                className={`cz-branch-option ${current?.branch_id === b.branch_id ? "is-current" : ""}`}
                onClick={() => onPick(b)}
                disabled={switching}
              >
                {b.name}
                {current?.branch_id === b.branch_id && <span className="cz-muted"> · current</span>}
                {current?.branch_id !== b.branch_id && lastUsed === b.branch_id && (
                  <span className="cz-muted"> · last used</span>
                )}
              </button>
            ))}
          </div>
        ))}
      </div>
      {onCancel && (
        <button className="cz-picker-cancel" onClick={onCancel}>
          Back
        </button>
      )}
    </div>
  );
}
