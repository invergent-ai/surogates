// Surogate Desktop's consent page, as the web client serves it (web/src/lib/oauth-consent.ts).
import assert from "node:assert/strict";
import { test } from "node:test";

import { consentRequest, decide, recentSignIn } from "../web/src/lib/oauth-consent.ts";

const ASKED = {
  response_type: "code", client_id: "surogate-desktop", redirect_uri: "http://127.0.0.1:43123/callback",
  state: "s".repeat(24), code_challenge: "c".repeat(43), code_challenge_method: "S256",
};
const search = (fields) => `?${new URLSearchParams(fields)}`;
const answering = (status, body) => {
  const sent = [];
  return { sent, post: async (asked) => { sent.push(asked); return new Response(JSON.stringify(body), { status }); } };
};

test("reads the desktop's request from the page's address, naming this computer as the desktop does", () => {
  assert.deepEqual(consentRequest(search({ ...ASKED, computer: "  Flavius's ThinkPad " })), { ...ASKED, computer: "Flavius's ThinkPad" });
  assert.equal(consentRequest(search({ ...ASKED })).computer, "this computer");
  assert.equal(consentRequest(search({ ...ASKED, computer: "x".repeat(300) })).computer.length, 100);
  // Cut by character, never inside one.
  assert.equal(consentRequest(search({ ...ASKED, computer: "\u{1F4BB}".repeat(150) })).computer, "\u{1F4BB}".repeat(100));
});

test("is no request when a field the desktop always sends is missing", () => {
  for (const field of Object.keys(ASKED)) {
    const { [field]: _missing, ...rest } = ASKED;
    assert.equal(consentRequest(search(rest)), null, field);
  }
});

test("sends the user's answer with the request as the desktop sent it, and follows the agent", async () => {
  const request = consentRequest(search({ ...ASKED, computer: "ThinkPad" }));
  const server = answering(200, { redirect_to: "http://127.0.0.1:43123/callback?code=c&state=s" });
  assert.deepEqual(await decide(request, "allow", server.post), { kind: "redirect", to: "http://127.0.0.1:43123/callback?code=c&state=s" });
  assert.deepEqual(server.sent, [{ ...ASKED, decision: "allow" }]);
});

test("asks for a sign-in again when the agent wants a recent one, or the session is gone", async () => {
  const request = consentRequest(search(ASKED));
  const stale = answering(403, { detail: { code: "recent_sign_in_required", message: "Sign in again" } });
  assert.deepEqual(await decide(request, "allow", stale.post), { kind: "sign-in-again" });
  assert.deepEqual(await decide(request, "allow", answering(401, { detail: "Invalid token" }).post), { kind: "sign-in-again" });
});

test("goes nowhere with a request the agent refuses, and says why", async () => {
  const request = consentRequest(search(ASKED));
  const refused = answering(400, { detail: "This sign-in request did not come from Surogate Desktop." });
  assert.deepEqual(await decide(request, "deny", refused.post), { kind: "refused", message: "This sign-in request did not come from Surogate Desktop." });
  assert.deepEqual((await decide(request, "allow", answering(422, { detail: [{ msg: "bad" }] }).post)).kind, "refused");
});

test("says the agent failed, not the request, when the agent answers with an error of its own", async () => {
  const request = consentRequest(search(ASKED));
  const failed = { post: async () => new Response("<html>Bad gateway</html>", { status: 502 }) };
  assert.deepEqual(await decide(request, "allow", failed.post), {
    kind: "refused", message: "The agent could not finish this sign-in. Try again from Surogate Desktop.",
  });
});

test("tells a sign-in recent enough to allow the desktop from one the agent would refuse", () => {
  const issued = Date.parse("2026-10-07T12:00:00Z") / 1000;
  const token = (claims) => `h.${Buffer.from(JSON.stringify(claims)).toString("base64url")}.s`;
  assert.equal(recentSignIn(token({ auth_time: issued - 60, iat: issued })), true);
  assert.equal(recentSignIn(token({ auth_time: issued - 3600, iat: issued })), false);
  // Signed in before sign-ins carried their time, or no token at all: not recent.
  assert.equal(recentSignIn(token({ iat: issued })), false);
  assert.equal(recentSignIn(token({ auth_time: issued - 60 })), false);
  assert.equal(recentSignIn("not a token"), false);
  assert.equal(recentSignIn(null), false);
});

test("measures the sign-in on the agent's clock, so a browser clock 15 minutes off changes nothing", (t) => {
  const issued = Date.parse("2026-10-07T12:00:00Z") / 1000;
  const token = (claims) => `h.${Buffer.from(JSON.stringify(claims)).toString("base64url")}.s`;
  for (const offset of [15 * 60_000, -15 * 60_000]) {
    t.mock.method(Date, "now", () => issued * 1000 + offset);
    assert.equal(recentSignIn(token({ auth_time: issued - 60, iat: issued })), true, `${offset}`);
    assert.equal(recentSignIn(token({ auth_time: issued - 3600, iat: issued })), false, `${offset}`);
    t.mock.restoreAll();
  }
});
