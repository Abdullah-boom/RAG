import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev`  -> demo page at http://localhost:5173 (index.html) talking to the API on :8000
// `npm run build` -> dist/cheezious-chat.js: ONE self-contained script (React + CSS inlined) for any website
export default defineConfig({
  plugins: [react()],
  define: { "process.env.NODE_ENV": JSON.stringify("production") },
  build: {
    lib: { entry: "src/main.tsx", name: "CheeziousChat", formats: ["iife"], fileName: () => "cheezious-chat.js" },
    cssCodeSplit: false,
    sourcemap: false,
  },
});
