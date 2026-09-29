import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const AGENT = process.env.SPIKE_AGENT_URL;
const TOKEN = process.env.ACCESS_TOKEN;
const PORT = 49152;
const REDIRECT = `http://127.0.0.1:${PORT}/callback`;

const pkce = () => {
  const verifier = crypto.randomBytes(32).toString('base64url');
  return { verifier, challenge: crypto.createHash('sha256').update(verifier).digest('base64url') };
};
const state = () => crypto.randomBytes(24).toString('base64url');
const post = async (p, body, auth) => {
  const r = await fetch(`${AGENT}/api/v1/auth/desktop/${p}`, {
    method: 'POST', headers: { 'content-type': 'application/json', ...(auth ? { authorization: `Bearer ${TOKEN}` } : {}) },
    body: JSON.stringify(body),
  });
  return { status: r.status, body: await r.json().catch(() => null) };
};
const mint = async (s, challenge, port = PORT) => post('code', { state: s, code_challenge: challenge, port }, true);
const exchange = (code, verifier, s, redirect = REDIRECT) => post('token', { code, code_verifier: verifier, state: s, redirect_uri: redirect });

const out = {};
{ const k = pkce(); const s = state(); const { body } = await mint(s, k.challenge);
  out.wrongVerifier = (await exchange(body.code, pkce().verifier, s)).status;
  out.correctAfterFailedAttempt = (await exchange(body.code, k.verifier, s)).status; }
{ const k = pkce(); const s = state(); const { body } = await mint(s, k.challenge);
  out.wrongRedirect = (await exchange(body.code, k.verifier, s, 'http://127.0.0.1:1/callback')).status; }
{ const k = pkce(); const s = state(); const { body } = await mint(s, k.challenge);
  out.wrongState = (await exchange(body.code, k.verifier, state())).status; }
{ const k = pkce(); const s = state(); const { body } = await mint(s, k.challenge);
  out.valid = (await exchange(body.code, k.verifier, s)).status;
  out.replay = (await exchange(body.code, k.verifier, s)).status; }
out.lowPort = (await mint(state(), pkce().challenge, 80)).status;
out.badChallenge = (await mint(state(), 'short', PORT)).status;
out.noAuthMint = (await post('code', { state: state(), code_challenge: pkce().challenge, port: PORT }, false)).status;
{ const k = pkce(); const s = state(); const { body } = await mint(s, k.challenge);
  await new Promise((r) => setTimeout(r, 61000));
  out.expired = (await exchange(body.code, k.verifier, s)).status; }

const dir = path.join(os.homedir(), 'surogate-spike-results');
fs.mkdirSync(dir, { recursive: true });
fs.writeFileSync(path.join(dir, `q6-negative-${os.hostname()}.json`), JSON.stringify(out, null, 2));
console.log(out);
