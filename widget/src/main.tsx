import { createRoot } from "react-dom/client";
import App, { type WidgetConfig } from "./App";
import css from "./widget.css?inline";

// Embed:  <script src="https://API_HOST/widget/cheezious-chat.js" defer
//                 data-api-url="https://API_HOST"        (optional: defaults to the script's own origin)
//                 data-title="Cheezious Assistant"       (optional)
//                 data-color="#f59f00"                   (optional: accent colour)
//                 data-position="right"></script>        (optional: right | left)
// Page API: window.CheeziousChat.open() / .close()

// Captured at load time: document.currentScript is null once DOMContentLoaded fires.
const CURRENT_SCRIPT = document.currentScript as HTMLScriptElement | null;

function readConfig(): WidgetConfig {
  const script =
    CURRENT_SCRIPT ??
    document.querySelector<HTMLScriptElement>("script[data-api-url], script[src*='cheezious-chat']");
  const d = script?.dataset ?? {};
  const origin = script?.src ? new URL(script.src, location.href).origin : location.origin;
  return {
    apiUrl: (d.apiUrl || origin).replace(/\/$/, ""),
    title: d.title || "Cheezious Assistant",
    color: d.color || "#f59f00",
    position: d.position === "left" ? "left" : "right",
  };
}

function mount() {
  if (document.getElementById("cheezious-chat-root")) return; // script included twice
  const config = readConfig();
  const host = document.createElement("div");
  host.id = "cheezious-chat-root";
  document.body.appendChild(host);
  // Shadow DOM: the site's CSS can't restyle the widget, and the widget's CSS can't leak into the site.
  const shadow = host.attachShadow({ mode: "open" });
  const style = document.createElement("style");
  style.textContent = css;
  shadow.appendChild(style);
  const container = document.createElement("div");
  shadow.appendChild(container);
  createRoot(container).render(<App config={config} />);
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
else mount();
