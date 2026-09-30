const { BrowserWindow, shell } = require('electron');
const crypto = require('node:crypto');
const http = require('node:http');
const { writeResult } = require('../lib/results.cjs');

async function autoLogin(url) {
  const { chromium } = require('playwright-core');
  const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome', headless: true });
  const page = await browser.newPage({ ignoreHTTPSErrors: true });
  await page.goto(url);
  await page.fill('#login-email', process.env.SPIKE_LOGIN_USER);
  await page.fill('#login-password', process.env.SPIKE_LOGIN_PASSWORD);
  await page.press('#login-password', 'Enter');
  await page.waitForURL(/127\.0\.0\.1:\d+\/callback/, { timeout: 60000 });
  autoLogin.landed = new URL(page.url()).pathname;
  await browser.close();
}

exports.run = async ({ argv }) => {
  const agent = process.env.SPIKE_AGENT_URL;
  const provider = argv.includes('q6-github') ? 'github' : argv.includes('q6-local') ? 'local' : 'google';
  const win = new BrowserWindow({ width: 1100, height: 800, webPreferences: { sandbox: true, contextIsolation: true, partition: 'persist:agent-spike' } });
  await win.loadURL(agent);
  const verifier = crypto.randomBytes(32).toString('base64url');
  const challenge = crypto.createHash('sha256').update(verifier).digest('base64url');
  const state = crypto.randomBytes(24).toString('base64url');
  const { code, redirectUri } = await new Promise((resolve, reject) => {
    const srv = http.createServer((req, res) => {
      const u = new URL(req.url, 'http://127.0.0.1');
      if (u.pathname !== '/callback' || u.searchParams.get('state') !== state) { res.writeHead(400); res.end('bad callback'); return; }
      res.end('Signed in, you can return to Surogate');
      const p = srv.address().port;
      srv.close();
      resolve({ code: u.searchParams.get('code'), redirectUri: `http://127.0.0.1:${p}/callback` });
    });
    srv.listen(0, '127.0.0.1', () => {
      const p = srv.address().port;
      const loginUrl = `${agent}/login?desktop_state=${state}&code_challenge=${challenge}&port=${p}`;
      // SPIKE_AUTOLOGIN: a headless Chrome driven by Playwright stands in for the
      // person at the system browser and signs in with a local account.
      if (process.env.SPIKE_AUTOLOGIN) autoLogin(loginUrl).catch(reject);
      else shell.openExternal(loginUrl);
    });
    setTimeout(() => { srv.close(); reject(new Error('sign-in timed out')); }, 5 * 60000);
  });
  const resp = await fetch(`${agent}/api/v1/auth/desktop/token`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ code, code_verifier: verifier, state, redirect_uri: redirectUri }),
  });
  const tokens = await resp.json();
  if (!resp.ok) throw new Error(`token exchange failed: ${JSON.stringify(tokens)}`);
  if (new URL(win.webContents.getURL()).origin !== new URL(agent).origin) throw new Error('window left the agent origin');
  // Spike shortcut: the design hands tokens to a preload completion handler instead.
  await win.webContents.executeJavaScript(
    `localStorage.setItem('surogates_auth_token', ${JSON.stringify(tokens.access_token)});`
    + `localStorage.setItem('surogates_auth_refresh_token', ${JSON.stringify(tokens.refresh_token)});`,
  );
  win.reload();
  await new Promise((r) => win.webContents.once('did-finish-load', r));
  const meStatus = await win.webContents.executeJavaScript(
    "fetch('/api/v1/auth/me', { headers: { Authorization: 'Bearer ' + localStorage.getItem('surogates_auth_token') } }).then((r) => r.status)",
  );
  console.log('q6 result:', writeResult(`q6-${provider}`, { provider, exchangeStatus: resp.status, meStatus, browserLandedOn: autoLogin.landed ?? null, windowOrigin: new URL(win.webContents.getURL()).origin }));
};
