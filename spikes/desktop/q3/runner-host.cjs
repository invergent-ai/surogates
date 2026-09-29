const { spawn } = require('node:child_process');
const http = require('node:http');
const net = require('node:net');
const path = require('node:path');

const port = process.parentPort;
const statusOf = (url) => new Promise((resolve) => {
  http.get(url, (r) => { r.resume(); resolve(r.statusCode); }).on('error', (e) => resolve(String(e.code || e)));
});
const alive = (pid) => { try { process.kill(pid, 0); return true; } catch { return false; } };

port.on('message', async ({ data }) => {
  const { SandboxManager: SM } = await import('@anthropic-ai/sandbox-runtime');
  process.env.CLAUDE_CODE_TMPDIR = data.env.TMPDIR;   // Q2 finding: srt's TMPDIR comes from here
  await SM.initialize(data.policy);
  const runnerCmd = `ELECTRON_RUN_AS_NODE=1 '${process.execPath}' '${path.join(__dirname, 'runner.cjs')}'`;
  const { argv } = await SM.wrapWithSandboxArgv(runnerCmd, undefined, undefined, undefined, data.cwd);
  const child = spawn(argv[0], argv.slice(1), { cwd: data.cwd, env: data.env, stdio: ['pipe', 'pipe', 'pipe', 'pipe'] });   // Q2 finding: app env only
  const replies = [];
  let buf = '';
  let stderr = '';
  child.stderr.on('data', (d) => { stderr += d; });
  child.stdout.on('data', (d) => {
    buf += d;
    let i;
    while ((i = buf.indexOf('\n')) >= 0) { replies.push(JSON.parse(buf.slice(0, i))); buf = buf.slice(i + 1); }
  });
  const call = (msg) => new Promise((resolve) => {
    child.stdin.write(`${JSON.stringify(msg)}\n`);
    const started = Date.now();
    const t = setInterval(() => {
      const m = replies.find((r) => r.id === msg.id);
      if (m || Date.now() - started > 15000) { clearInterval(t); resolve(m || { id: msg.id, timeout: true }); }
    }, 50);
  });
  const res = {};
  res.start = await call({ id: 1, cmd: 'python3 -m http.server 18767 --bind 127.0.0.1', background: true });
  await new Promise((r) => setTimeout(r, 1500));
  res.curlSameSandbox = await call({ id: 2, cmd: "curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:18767/" });
  const tunnel = child.stdio[3];
  const server = net.createServer((sock) => { sock.pipe(tunnel); tunnel.pipe(sock); });
  await new Promise((r) => server.listen(18768, '127.0.0.1', r));
  res.forwardSetup = await call({ id: 3, forward: { port: 18767 } });
  res.forwardedFromOutside = await statusOf('http://127.0.0.1:18768/');
  res.runnerPid = child.pid;
  res.serverPidInsideRunner = res.start.pid;
  child.kill('SIGKILL');
  server.close();
  await new Promise((r) => setTimeout(r, 1500));
  res.runnerAliveAfterKill = alive(child.pid);
  res.anyHttpServerLeft = require('node:child_process')
    .execSync("pgrep -af 'http.server 18767' || true", { encoding: 'utf8' }).trim();
  res.stderr = stderr.slice(-4000);
  port.postMessage(res);
});
