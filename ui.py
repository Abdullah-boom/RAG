"""Streamlit test UI for the Phase 1 chatbot.  Run: make ui   (or .venv/bin/streamlit run ui.py)

The sidebar "login" simulates what a verified JWT gives the server: the customer id goes into the
graph's runtime config, never into the prompt, so the model cannot change it.
"""
import uuid
import warnings

warnings.filterwarnings("ignore")

import streamlit as st  # noqa: E402

from app.agent.graph import chat, graph  # noqa: E402
from app.data import repositories as repo  # noqa: E402
from app.retrieval import index  # noqa: E402

st.set_page_config(page_title="Cheezious Assistant", page_icon="🍕", layout="centered")


@st.cache_resource(show_spinner="Loading menu index…")
def warm_up():
    index().search("pizza", "menu", k=1)  # loads the embedding model once per server
    return True


@st.cache_data(ttl=300)
def branches():
    return repo.list_branches()


def new_chat():
    st.session_state.thread_id = str(uuid.uuid4())
    st.session_state.turns = []
    st.session_state.cart = None


def render_cart():
    st.subheader("🛒 Cart")
    cart = st.session_state.get("cart")
    if not cart or not cart["lines"]:
        st.caption("Empty. Try: *2 regular chicken tikka aur 1 bazooka add karo*")
        return
    for line in cart["lines"]:
        label = f"{line['qty']} × {line['name']}" + (f" ({line['size']})" if line.get("size") else "")
        if line["available"]:
            st.markdown(f"{label}  \n<span style='opacity:.7'>Rs {line['line_total']}</span>", unsafe_allow_html=True)
        else:
            st.markdown(f"~~{label}~~ · not available now")
    st.markdown(f"**Subtotal: Rs {cart['subtotal']}**")
    st.caption("Tax & delivery added in the app at checkout. This cart is not an order.")
    if st.button("Clear cart", use_container_width=True):
        graph().update_state({"configurable": {"thread_id": st.session_state.thread_id}}, {"cart": []})
        st.session_state.cart = None
        st.rerun()


def price_line(item: dict) -> str:
    if item.get("price") is not None:
        return f"Rs {item['price']}"
    return " · ".join(f"{s['size']} Rs {s['price']}" for s in item.get("sizes") or [])


def render_turn(turn: dict, show_debug: bool):
    with st.chat_message("user"):
        st.write(turn["question"])
    with st.chat_message("assistant", avatar="🍕"):
        r = turn["response"]
        st.markdown(r["answer"])
        if r["items"]:
            cols = st.columns(min(len(r["items"]), 3))
            for col, item in zip(cols * 2, r["items"]):
                with col.container(border=True):
                    st.markdown(f"**{item['name']}**")
                    if item.get("category"):
                        st.caption(item["category"])
                    st.write(price_line(item))
                    st.caption("✅ available" if item.get("available") else "❌ unavailable")
        if show_debug:
            d = r["debug"]
            st.caption(f"tools: {', '.join(d['tools']) or 'none'} · {d['latency_s']}s"
                       + (" · guard retry" if d["guard_retry"] else "") + (" · offline" if d["offline"] else ""))


if "thread_id" not in st.session_state:
    new_chat()

# ---------- sidebar ----------
with st.sidebar:
    st.header("🍕 Cheezious Assistant")
    st.caption("Phase 1 test drive · read-only")
    blist = branches()
    labels = [f"{b['name']} ({b['city']})" for b in blist]
    default = next((i for i, b in enumerate(blist) if "gulberg" in b["name"].lower()), 0)
    choice = st.selectbox("Branch", range(len(blist)), index=default, format_func=lambda i: labels[i])
    branch = blist[choice]
    status = repo.get_branch_status(branch["branch_id"]) or {}
    st.caption(("🟢 open now" if status.get("open_now") else "🔴 closed now")
               + (f" · {status['closed_reason']}" if status.get("closed_reason") else ""))

    st.divider()
    user_id = st.text_input("Customer ID (simulated login)", placeholder="leave empty = guest",
                            help="In the real app this comes from the verified login token.").strip() or None
    st.caption(f"Logged in as `{user_id[:8]}…`" if user_id else "Guest: order questions need a login")

    st.divider()
    render_cart()

    st.divider()
    show_debug = st.toggle("Show tools & latency", value=True)
    if st.button("New chat", use_container_width=True):
        new_chat()
        st.rerun()

# Branch or login changes start a fresh conversation and cart (prices differ per branch).
ctx_key = (branch["branch_id"], user_id)
if st.session_state.get("ctx_key") != ctx_key:
    st.session_state.ctx_key = ctx_key
    new_chat()

warm_up()

# ---------- conversation ----------
if not st.session_state.turns:
    st.info("Ask about the menu, prices, deals, branch timings or your orders, in English, "
            "Roman Urdu or اردو.  e.g. *kuch teekha 800 se kam* · *How much is the chicken tikka pizza?*")

for turn in st.session_state.turns:
    render_turn(turn, show_debug)

suggestions = st.session_state.turns[-1]["response"]["suggestions"] if st.session_state.turns else \
    ["Show deals", "Is my branch open?", "Where is my order?"]
clicked = None
cols = st.columns(len(suggestions)) if suggestions else []
for col, s in zip(cols, suggestions):
    if col.button(s, use_container_width=True, key=f"sug-{len(st.session_state.turns)}-{s}"):
        clicked = s

question = st.chat_input("Type your question…") or clicked
if question:
    with st.spinner("Checking live menu…"):
        response = chat(question, thread_id=st.session_state.thread_id,
                        branch_id=branch["branch_id"], user_id=user_id)
    st.session_state.turns.append({"question": question, "response": response})
    if response.get("cart") is not None:
        st.session_state.cart = response["cart"]
    st.rerun()
