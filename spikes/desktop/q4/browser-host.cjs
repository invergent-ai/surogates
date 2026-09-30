const { execFileSync } = require('node:child_process');
const dns = require('node:dns/promises');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require('playwright-core');
const { fixture } = require('./fixture.cjs');
const { isPrivate, guardProxy } = require('./guard-proxy.cjs');
const { writeResult } = require('../lib/results.cjs');

const ORIGIN = 'http://spike.example';   // plain http: no mixed-content blocking of http targets
const LAN = Object.values(os.networkInterfaces()).flat().find((a) => a && a.family === 'IPv4' && !a.internal)?.address;
const CANDIDATES = [['chrome', '/usr/bin/google-chrome'], ['chromium', '/usr/bin/chromium'], ['chromium-snap', '/snap/bin/chromium'], ['chromium-browser', '/usr/bin/chromium-browser'],
  ['edge', '/usr/bin/microsoft-edge'], ['brave', '/usr/bin/brave-browser'], ['vivaldi', '/usr/bin/vivaldi']];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function processes(match) {
  return fs.readdirSync('/proc').filter((p) => /^\d+$/.test(p)).map((pid) => {
    try {
      const raw = fs.readFileSync(`/proc/${pid}/cmdline`, 'utf8').split('\0').filter(Boolean);
      // Chrome rewrites its process title into one space-joined string.
      const argv = raw.length === 1 ? raw[0].split(' ') : raw;
      const seccomp = /Seccomp:\s+(\d)/.exec(fs.readFileSync(`/proc/${pid}/status`, 'utf8'))?.[1];
      return { pid: Number(pid), argv, seccomp };
    } catch { return null; }
  }).filter((p) => p && p.argv.some((a) => a.includes(match)));
}

function canary() {
  const hits = [];
  const web = http.createServer((req, res) => { hits.push(req.url); res.end('canary'); });
  const lan = http.createServer((req, res) => { hits.push(`lan:${req.url}`); res.end('canary'); });
  const ws = net.createServer((s) => { hits.push('ws-connect'); s.destroy(); });
  return {
    hits,
    start: () => Promise.all([new Promise((r) => web.listen(18081, '127.0.0.1', r)), new Promise((r) => ws.listen(18082, '127.0.0.1', r)), new Promise((r) => (LAN ? lan.listen(18083, LAN, r) : r()))]),
    stop: () => { web.close(); web.closeAllConnections(); ws.close(); lan.close(); lan.closeAllConnections(); },
  };
}

// Session-scoped guard on the whole context; popups inherit their opener's session.
async function installGuard(context, sessionOf, grants, log) {
  const decide = async (page, rawUrl) => {
    const url = new URL(rawUrl);
    const session = page ? sessionOf(page) : null;
    if (!['http:', 'https:', 'ws:', 'wss:'].includes(url.protocol)) return { allow: false, why: 'scheme', session };
    let addrs = [];
    try { addrs = (await dns.lookup(url.hostname, { all: true })).map((a) => a.address); } catch { /* unresolvable */ }
    const priv = url.hostname === 'localhost' || addrs.some(isPrivate);
    const granted = session && grants.get(session)?.has(`${url.protocol === 'ws:' ? 'http:' : url.protocol}//${url.host}`);
    return { allow: !priv || Boolean(granted), why: priv ? 'private' : 'public', session, addrs };
  };
  await context.route('**/*', async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    let page = null;
    try { page = req.frame().page(); } catch { /* worker or service-worker request: no frame */ }
    if (url.origin === ORIGIN) {
      const f = fixture(url.pathname, page ? sessionOf(page) : 'none', LAN);
      return route.fulfill({ status: f.status, contentType: f.contentType, body: f.body });
    }
    const d = await decide(page, req.url());
    log.push({ kind: 'http', url: req.url(), noFrame: !page, ...d });
    return d.allow ? route.continue() : route.abort('blockedbyclient');
  });
  await context.routeWebSocket(/.*/, async (ws) => {
    const d = await decide(null, ws.url());
    log.push({ kind: 'ws', url: ws.url(), ...d });
    if (d.allow) ws.connectToServer(); else ws.close();
  });
}

// Each phase records its own error, so one failure does not hide the rest.
async function phase(out, name, fn) {
  try { await fn(); } catch (e) { (out.errors ??= {})[name] = String((e && e.stack) || e).slice(0, 1500); }
}

async function probeBrowser(name, exe, folder) {
  const out = { name, exe };
  const real = fs.realpathSync(exe);
  // Ubuntu's /usr/bin/chromium resolves to /usr/bin/snap, not to a path under /snap/.
  if (real.startsWith('/snap/') || path.basename(real) === 'snap' || real.includes('/flatpak/')) {
    return { ...out, skipped: `unsupported package: ${real}` };
  }
  const profile = fs.mkdtempSync(path.join(os.homedir(), `.config/surogate-spike-profile-${name}-`));
  const downloads = fs.mkdtempSync(path.join(os.tmpdir(), 'spike-dl-'));
  const cn = canary();
  await cn.start();
  try {
    await phase(out, 'main', async () => {
      const context = await chromium.launchPersistentContext(profile, {
        executablePath: exe, headless: false, chromiumSandbox: true,
        acceptDownloads: true, downloadsPath: downloads, serviceWorkers: 'block',
        args: process.env.SPIKE_CHROME_ARGS ? process.env.SPIKE_CHROME_ARGS.split(' ') : [],
      });
      try {
        // context.browser() is null for persistent contexts; the spike may run --version.
        out.version = execFileSync(exe, ['--version'], { encoding: 'utf8', timeout: 10000 }).trim();
        const procs = processes(profile);
        const main = procs.find((p) => !p.argv.some((a) => a.startsWith('--type=')));
        out.flags = {
          pipe: main?.argv.includes('--remote-debugging-pipe') ?? false,
          port: main?.argv.some((a) => a.startsWith('--remote-debugging-port')) ?? false,
          noSandbox: procs.some((p) => p.argv.includes('--no-sandbox')),
          rendererSeccomp: procs.filter((p) => p.argv.includes('--type=renderer')).map((p) => p.seccomp),
        };
        await phase(out, 'secondLaunch', async () => {
          try {
            await chromium.launchPersistentContext(profile, { executablePath: exe, headless: false, chromiumSandbox: true, timeout: 15000 });
            out.secondLaunch = 'unexpectedly succeeded';
          } catch (e) { out.secondLaunch = String(e.message).split('\n')[0]; }
        });

        const sessions = new Map();
        const grants = new Map([['A', new Set(['http://127.0.0.1:18081'])]]);
        const log = [];
        let pageB;
        await phase(out, 'guard', async () => {
          await installGuard(context, (p) => sessions.get(p) ?? null, grants, log);
          const pageA = await context.newPage(); sessions.set(pageA, 'A');
          pageB = await context.newPage(); sessions.set(pageB, 'B');
          await Promise.all([pageA.goto(`${ORIGIN}/`, { waitUntil: 'domcontentloaded' }), pageB.goto(`${ORIGIN}/`, { waitUntil: 'domcontentloaded' })]);
          await sleep(6000);
          out.canaryHits = [...cn.hits];
          out.bLeaks = cn.hits.filter((h) => h.includes('t=B'));
          out.guardLogSample = log.slice(0, 60);
        });
        if (!pageB) return;
        await phase(out, 'popup', async () => {
          const popupWait = context.waitForEvent('page', { timeout: 5000 }).catch(() => null);
          await pageB.evaluate(() => window.open('file:///etc/hostname'));
          const popup = await popupWait;
          out.popupFile = popup ? `popup url: ${popup.url()}` : 'no popup';
        });
        await phase(out, 'download', async () => {
          const dl = await Promise.all([pageB.waitForEvent('download', { timeout: 10000 }), pageB.click('#dl')]).then(([d]) => d).catch((e) => e);
          if (dl && dl.suggestedFilename) {
            const raw = dl.suggestedFilename();
            out.download = { raw };
            const safe = path.basename(raw).replace(/[\u0000-\u001f]/g, '') || 'download';
            const target = path.join(folder, safe);
            await dl.saveAs(target);
            out.download = { raw, saved: target, inFolder: fs.realpathSync(target).startsWith(fs.realpathSync(folder)) };
          } else out.download = `no download: ${String(dl)}`;
        });
        await phase(out, 'fileChooser', async () => {
          let chooser = false;
          pageB.on('filechooser', () => { chooser = true; });
          await pageB.click('#file').catch(() => {});
          await sleep(1000);
          out.fileChooserIntercepted = chooser;
        });
        await phase(out, 'pageSchemes', async () => {
          out.pageSchemes = {};
          for (const u of ['chrome://version', 'view-source:https://example.com', 'file:///etc/hostname']) {
            await pageB.goto(`${ORIGIN}/`, { waitUntil: 'domcontentloaded' });
            await pageB.evaluate((x) => { location.href = x; }, u).catch(() => {});
            await sleep(2500);
            const popupWait = context.waitForEvent('page', { timeout: 3000 }).catch(() => null);
            await pageB.evaluate((x) => { window.open(x); }, u).catch(() => {});
            const popup = await popupWait;
            out.pageSchemes[u] = { locationAfter: pageB.url(), popup: popup ? popup.url() : null };
            if (popup) await popup.close().catch(() => {});
          }
        });
        await phase(out, 'schemes', async () => {
          out.schemes = {};
          for (const u of ['file:///etc/hostname', 'chrome://version', 'view-source:https://example.com']) {
            try { await pageB.goto(u, { timeout: 5000 }); out.schemes[u] = `navigated: ${pageB.url()}`; } catch (e) { out.schemes[u] = `blocked: ${String(e.message).split('\n')[0]}`; }
          }
          await pageB.goto(`${ORIGIN}/`, { waitUntil: 'domcontentloaded' });
        });
      } finally {
        await context.close();
      }
    });

    await phase(out, 'serviceWorkers', async () => {
      const swProfile = fs.mkdtempSync(path.join(os.homedir(), `.config/surogate-spike-sw-${name}-`));
      const swCtx = await chromium.launchPersistentContext(swProfile, { executablePath: exe, headless: false, chromiumSandbox: true, serviceWorkers: 'allow' });
      try {
        const swSessions = new Map();
        await installGuard(swCtx, (p) => swSessions.get(p) ?? null, new Map(), []);
        const swPage = await swCtx.newPage(); swSessions.set(swPage, 'S');
        const before = cn.hits.length;
        await swPage.goto(`${ORIGIN}/`, { waitUntil: 'domcontentloaded' });
        await sleep(6000);
        out.serviceWorkersAllowedHits = cn.hits.slice(before);
      } finally { await swCtx.close(); }
    });

    await phase(out, 'proxy', async () => {
      const proxyLog = [];
      const proxy = guardProxy(proxyLog);
      await new Promise((r) => proxy.listen(18090, '127.0.0.1', r));
      try {
        const pxProfile = fs.mkdtempSync(path.join(os.homedir(), `.config/surogate-spike-px-${name}-`));
        if (process.env.SPIKE_WEBRTC_PREF) {
          // The agent profile is the app's own, so the WebRTC policy can be set as a profile preference.
          fs.mkdirSync(path.join(pxProfile, 'Default'), { recursive: true });
          fs.writeFileSync(path.join(pxProfile, 'Default', 'Preferences'), JSON.stringify({ webrtc: { ip_handling_policy: 'disable_non_proxied_udp' } }));
          out.webrtcPref = true;
        }
        const webrtcFlag = process.env.SPIKE_WEBRTC_FLAG ? ['--force-webrtc-ip-handling-policy=disable_non_proxied_udp'] : [];
        out.webrtcFlag = webrtcFlag.length > 0;
        const pxCtx = await chromium.launchPersistentContext(pxProfile, {
          executablePath: exe, headless: false, chromiumSandbox: true, serviceWorkers: 'block',
          args: ['--proxy-server=http://127.0.0.1:18090', '--proxy-bypass-list=<-loopback>', ...webrtcFlag],
        });
        try {
          await pxCtx.route(`${ORIGIN}/**`, (route) => {
            const f = fixture(new URL(route.request().url()).pathname, 'P', LAN);
            return route.fulfill({ status: f.status, contentType: f.contentType, body: f.body });
          });
          const pxPage = await pxCtx.newPage();
          const beforePx = cn.hits.length;
          await pxPage.goto(`${ORIGIN}/`, { waitUntil: 'domcontentloaded' });
          await sleep(6000);
          out.proxyHits = cn.hits.slice(beforePx);
          out.proxyIce = await pxPage.evaluate(() => window.__ice);
          out.proxyBlocked = proxyLog;
        } finally { await pxCtx.close(); }
      } finally { proxy.close(); proxy.closeAllConnections?.(); }
    });
  } finally {
    cn.stop();
  }
  return out;
}

process.parentPort.on('message', async ({ data }) => {
  const results = [];
  for (const [name, exe] of CANDIDATES) {
    if (!fs.existsSync(exe)) { results.push({ name, exe, missing: true }); continue; }
    try { results.push(await probeBrowser(name, exe, data.folder)); } catch (e) { results.push({ name, exe, error: String((e && e.stack) || e) }); }
    writeResult('q4-partial', { results });   // survives a host crash
  }
  process.parentPort.postMessage(results);
});
