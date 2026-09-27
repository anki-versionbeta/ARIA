/**
 * Sends port 80 to HTTPS.
 *
 * Vite binds a single port, so once it is on 443 nothing answers on 80 and the bare
 * hostname stops working -- which is the URL everyone types. There is no nginx, iptables
 * or socat on this host to do the redirect, so this is the smallest thing that can: no
 * dependencies, no request body ever read, and it never proxies. It cannot serve the wrong
 * application because it does not serve anything at all.
 *
 * The Host header is echoed back so both the Route 53 name and the raw IP redirect to
 * themselves; the certificate covers each.
 */
import http from "node:http";

const PORT = Number(process.env.REDIRECT_LISTEN_PORT ?? 80);
const TARGET = Number(process.env.REDIRECT_TARGET_PORT ?? 443);

http
  .createServer((request, response) => {
    // Strip any port the client sent, then re-add ours only when it is non-standard.
    const host = (request.headers.host ?? "").split(":")[0];
    if (!host) {
      response.writeHead(400, { "Content-Type": "text/plain" });
      response.end("Missing Host header\n");
      return;
    }
    const suffix = TARGET === 443 ? "" : `:${TARGET}`;
    response.writeHead(308, {
      // 308 rather than 301: it preserves the method, so a POST that arrives on 80 is not
      // silently turned into a GET. Nothing should POST over plain HTTP, but if it does,
      // failing loudly beats losing the body.
      Location: `https://${host}${suffix}${request.url ?? "/"}`,
      "Cache-Control": "no-store",
    });
    response.end();
  })
  .listen(PORT, "0.0.0.0", () => {
    console.log(`redirecting http://:${PORT} -> https://:${TARGET}`);
  });
