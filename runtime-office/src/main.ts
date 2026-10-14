import { Office } from "./office.js";
import { PagePool } from "./pages.js";
import { createServer } from "./server.js";

const env = process.env;
const token = env.RUNTIME_OFFICE_TOKEN ?? "";
if (token.length < 32) {
  console.error("RUNTIME_OFFICE_TOKEN must be at least 32 characters");
  process.exit(1);
}
const port = Number(env.PORT ?? 8080);
const pool = new PagePool({
  hostUrl: `http://127.0.0.1:${port}/host.html`,
  maxPages: Number(env.RUNTIME_OFFICE_MAX_PAGES ?? 6),
  idleMs: Number(env.RUNTIME_OFFICE_IDLE_MS ?? 600_000),
  openTimeoutMs: 60_000,
  commandTimeoutMs: 20_000,
  // DEV's document server has an mkcert certificate Chromium does not trust.
  ignoreTls: env.RUNTIME_OFFICE_IGNORE_TLS === "1",
  executablePath: env.CHROMIUM_PATH || undefined,
});
const server = createServer(new Office(pool), { token });
server.listen(port, "0.0.0.0", () => console.log(`runtime-office listening on ${port}`));
const sweeper = setInterval(() => {
  pool.sweep().catch((error: unknown) => console.error("closing idle pages failed", error));
}, 30_000);
for (const signal of ["SIGTERM", "SIGINT"] as const) {
  process.on(signal, () => {
    clearInterval(sweeper);
    server.close();
    void pool.shutdown().finally(() => process.exit(0));
  });
}
