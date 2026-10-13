// Per-visitor conveniences only (chosen branch, current session id). Every access is guarded:
// storage can be blocked (private mode, strict cookie settings) and the widget must still work.

const PREFIX = "cheezious-chat:";

export function load(key: string): string | null {
  try {
    return localStorage.getItem(PREFIX + key);
  } catch {
    return null;
  }
}

export function save(key: string, value: string | null): void {
  try {
    if (value === null) localStorage.removeItem(PREFIX + key);
    else localStorage.setItem(PREFIX + key, value);
  } catch {
    /* storage unavailable: the chat still works, it just won't survive a reload */
  }
}
