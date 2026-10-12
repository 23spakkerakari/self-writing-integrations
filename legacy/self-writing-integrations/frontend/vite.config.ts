import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The dev server proxies /api to the FastAPI control plane so the browser never deals with CORS.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  // Forward slashes on purpose: a backslash Windows path here makes Vite treat every "@/" import
  // as outside the project root and serve it from a broken /@fs/ URL.
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)).split("\\").join("/") } },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.API_URL ?? "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
