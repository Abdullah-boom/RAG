"""Terminal test drive for the Phase 1 chatbot.

  python chat.py                         # pick a branch interactively, guest user
  python chat.py --branch gulberg --user <customerId> --debug

--user simulates the identity a verified JWT would give the server. The model never sees or sets it.
Commands inside the chat: /branch, /reset, /quit
"""
import argparse
import sys
import warnings

warnings.filterwarnings("ignore")

import uuid  # noqa: E402

from app.agent.graph import chat  # noqa: E402
from app.data import repositories as repo  # noqa: E402


def pick_branch(hint: str = "") -> dict:
    branches = repo.list_branches()
    if hint:
        match = [b for b in branches if hint.lower() in (b["name"] + " " + b["city"]).lower() or b["branch_id"] == hint]
        if match:
            return match[0]
        print(f"No active branch matches '{hint}'.")
    for i, b in enumerate(branches, 1):
        print(f"  {i:2}. {b['name']} ({b['city']})")
    while True:
        choice = input("Pick a branch number: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(branches):
            return branches[int(choice) - 1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--branch", default="", help="branch name/city substring or id")
    ap.add_argument("--user", default=None, help="customerId to act as (simulated login)")
    ap.add_argument("--debug", action="store_true", help="show tools called and latency")
    args = ap.parse_args()

    branch = pick_branch(args.branch)
    thread_id = str(uuid.uuid4())
    print(f"\nCheezious Assistant | branch: {branch['name']} | "
          f"{'logged in as ' + args.user[:8] + '...' if args.user else 'guest'}\n(/branch, /reset, /quit)\n")
    while True:
        try:
            msg = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not msg:
            continue
        if msg == "/quit":
            break
        if msg == "/reset":
            thread_id = str(uuid.uuid4())
            print("(history cleared)")
            continue
        if msg == "/branch":
            branch = pick_branch()
            thread_id = str(uuid.uuid4())
            print(f"(branch: {branch['name']})")
            continue
        r = chat(msg, thread_id=thread_id, branch_id=branch["branch_id"], user_id=args.user)
        print(f"bot> {r['answer']}")
        for it in r["items"]:
            price = (f"Rs {it['price']}" if it.get("price") is not None
                     else ", ".join(f"{s['size']} Rs {s['price']}" for s in it.get("sizes") or []))
            print(f"     [card] {it['name']} | {price} | {'available' if it['available'] else 'unavailable'}")
        if args.debug:
            print(f"     (tools: {r['debug']['tools']}, {r['debug']['latency_s']}s"
                  f"{', guard retry' if r['debug']['guard_retry'] else ''})")
    sys.exit(0)


if __name__ == "__main__":
    main()
