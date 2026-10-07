// The desktop's OAuth client against a real agent, for the cross-check in
// tests/integration/test_desktop_sign_in.py: it prints, one JSON line each, the address
// the system browser would be opened at, then what the sign-in, a refresh and the sign-out gave.

import { OAuthError, refreshTokens, revokeTokens, signInWithBrowser } from "../shell/oauth.js";

const origin = process.argv[process.argv.indexOf("--origin") + 1] ?? "";
const say = (event: Record<string, unknown>) => console.log(JSON.stringify(event));
const fetchOut = (url: string, init: RequestInit) => fetch(url, init);

const tokens = await signInWithBrowser({ origin, computer: "Cross-check", fetch: fetchOut, open: async (url) => say({ event: "open", url }) });
say({ event: "signed-in", authTime: tokens.authTime, accessToken: tokens.accessToken });
const renewed = await refreshTokens(origin, tokens.refreshToken, fetchOut);
say({ event: "refreshed", rotated: renewed.refreshToken !== tokens.refreshToken, authTime: renewed.authTime });
await revokeTokens(origin, renewed.refreshToken, fetchOut);
const after = await refreshTokens(origin, renewed.refreshToken, fetchOut).then(() => "refreshed", (error: unknown) =>
  error instanceof OAuthError ? error.code : String(error));
say({ event: "signed-out", refresh: after });
