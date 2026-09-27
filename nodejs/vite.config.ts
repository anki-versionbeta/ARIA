import fs from "node:fs";
import { fileURLToPath, URL } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import { tanstackRouter } from "@tanstack/router-plugin/vite";
import viteReact from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import svgr from "vite-plugin-svgr";

/**
 * TLS from files named in the environment rather than hardcoded paths, so a checkout
 * without the certificate still starts on plain HTTP for local work. Both must exist:
 * half-configured TLS should fall back rather than crash the dev server on import.
 */
function httpsFromEnv() {
  const key = process.env.TLS_KEY_FILE;
  const cert = process.env.TLS_CERT_FILE;
  if (!key || !cert || !fs.existsSync(key) || !fs.existsSync(cert)) return undefined;
  // cert.pem is leaf + issuing CA, which is what a client needs to build a path.
  return { key: fs.readFileSync(key), cert: fs.readFileSync(cert) };
}

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [
    tanstackRouter({
      target: "react",
      autoCodeSplitting: true,
      routeFileIgnorePattern: ".test.",
    }),
    viteReact(),
    tailwindcss(),
    svgr(),
  ],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    // Vite refuses a request whose Host header it does not recognise, which is what
    // protects a dev server from DNS rebinding. IP addresses are allowed implicitly, so
    // 10.224.134.56 worked and the Route 53 name did not -- it answered
    // "Blocked request. This host is not allowed." A leading dot allows the domain and its
    // subdomains, so any further internal record works without another code change, while
    // still refusing hosts outside abbvienet.com.
    allowedHosts: [".abbvienet.com"],
    https: httpsFromEnv(),
    // Mirrors the production ingress (/api/* -> api:8000), which keeps the
    // session cookie same-origin so SameSite behaves in dev as it does in prod.
    proxy: {
      "/api": { target: "http://localhost:8000" },
    },
  },
});
