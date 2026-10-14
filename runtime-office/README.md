# runtime-office

The agent's editor sessions: a Node service with one headless Chromium that opens a Word document in a real
Euro-Office editor (one browser context per document per chat) and applies the agent's reads and edits through the
editor's connector. The worker's `office_read` and `office_edit` tools call it. The spec is
`docs/superpowers/specs/2026-10-13-office-editors-design.md`; the connector's behaviour is in
`docs/superpowers/notes/2026-10-13-office-connector-verify.md`.

| Variable | Default | |
|---|---|---|
| `RUNTIME_OFFICE_TOKEN` | required | Bearer token the worker sends (at least 32 characters); the runtime's `office.runtime_office_token`. |
| `PORT` | `8080` | |
| `RUNTIME_OFFICE_MAX_PAGES` | `6` | Documents open at once; each costs 0.5–0.7 GB. |
| `RUNTIME_OFFICE_IDLE_MS` | `600000` | A page no call used for this long closes, and the agent leaves the co-author list. |
| `RUNTIME_OFFICE_IGNORE_TLS` | unset | `1` in DEV, where the document server's certificate is from mkcert. |
| `CHROMIUM_PATH` | Playwright's | A Chromium to use instead of the one Playwright installed. |

DEV: `npm ci && npm run build && RUNTIME_OFFICE_TOKEN=… RUNTIME_OFFICE_IGNORE_TLS=1 npm start`. Tests: `npm test`; the
`*.it.test.ts` files run against a document server when `OFFICE_IT_DS_URL`, `OFFICE_IT_JWT_SECRET` and `OFFICE_IT_HOST`
are set (see `test/support/office.ts`).
