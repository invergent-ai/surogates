// Throwaway diagnostic for the Q2 network failure; not one of the eight questions.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { forkHost } = require('../lib/hosts.cjs');
const { buildPolicy } = require('../lib/policy.cjs');
const { buildEnv, sessionTmp } = require('../lib/env.cjs');
const { writeResult } = require('../lib/results.cjs');

// q9-scope: review fix pass — IPC denial and read scope, with /run narrowed or not.
async function scope() {
  const uid = process.getuid();
  const runUser = `/run/user/${uid}`;
  const sock = path.join(runUser, 'spike-ipc.sock');
  const net = require('node:net');
  try { fs.unlinkSync(sock); } catch { /* none */ }
  const server = net.createServer((c) => { c.end('ipc-reached\n'); });
  await new Promise((r) => server.listen(sock, r));
  const other = path.join(os.homedir(), '.local/share/surogate/tmp/other-session');
  fs.mkdirSync(other, { recursive: true });
  fs.writeFileSync(path.join(other, 'secret.txt'), 'other session temp\n');
  const userData = path.join(os.homedir(), '.config/surogate-desktop-spike');
  const profile = fs.readdirSync(path.join(os.homedir(), '.config')).find((d) => d.startsWith('surogate-spike-profile-'));
  const folder = path.join(os.homedir(), 'spike-roots/A');
  const results = {};
  for (const runMode of ['run-all', 'run-narrow']) {
    const tmp = sessionTmp(`q9scope-${runMode}`);
    const policy = buildPolicy({ folder, tmp, allowedDomains: ['registry.npmjs.org', 'pypi.org', 'files.pythonhosted.org'], variant: 'root-deny' });
    if (runMode === 'run-narrow') {
      policy.filesystem.allowRead = policy.filesystem.allowRead.filter((p) => p !== '/run').concat(['/run/systemd/resolve']);
    }
    const host = forkHost(`q9-${runMode}`, { policy, tmp });
    await host.ready;
    const env = buildEnv({ tmp });
    const run = (cmd) => host.run(cmd, { cwd: fs.realpathSync(folder), env });
    const r = {};
    r.unixConnect = await run(`python3 -c "import socket; s=socket.socket(socket.AF_UNIX); s.connect('${sock}'); print(s.recv(64).decode().strip())"`);
    r.sessionBus = await run(`busctl --user --address=unix:path=${runUser}/bus list 2>&1 | head -3; echo "exit=\${PIPESTATUS[0]}"`);
    r.runUserListing = await run(`ls -A ${runUser} 2>&1 | head -20`);
    r.otherMount = await run('cat /mnt/spike-data/C/seed.txt');
    r.otherSessionTmp = await run(`cat '${path.join(other, 'secret.txt')}'`);
    r.appUserData = await run(`ls '${userData}' 2>&1 | head -3`);
    r.browserProfile = await run(profile ? `ls '${path.join(os.homedir(), '.config', profile)}' 2>&1 | head -3` : 'echo no-profile-dir');
    r.appDirWrite = await run(`touch '${path.dirname(process.execPath)}/pwn' 2>&1; echo "exit=$?"`);
    r.tools = await run('node --version; git --version; python3 -c "print(1)"');
    r.npm = await run('printf \'{"name":"probe","version":"1.0.0"}\' > package.json && npm install left-pad --no-audit --no-fund');
    r.pip = await run('rm -rf .venv && python3 -m venv .venv && .venv/bin/pip install -q six && echo pip-ok');
    r.allowedNet = await run("curl -sS -o /dev/null -w '%{http_code}' https://registry.npmjs.org/");
    results[runMode] = r;
    host.child.kill();
  }
  server.close();
  console.log('q9-scope result:', writeResult('q9-scope', { sock, results }));
}

exports.run = async ({ arg } = {}) => {
  if (arg === 'q9-scope') return scope();
  const folder = fs.mkdtempSync(path.join(os.homedir(), 'spike-q9-'));
  const tmp = sessionTmp('q9');
  const asks = [];
  const policy = buildPolicy({ folder, tmp, allowedDomains: ['registry.npmjs.org'], variant: 'home-deny' });
  if (process.env.SPIKE_KEEP_TMP) policy.filesystem.denyRead = policy.filesystem.denyRead.filter((p) => p !== '/tmp');
  const host = forkHost('q9', {
    policy, tmp,
    onAsk: (m) => { asks.push(m); return true; },
  });
  const ready = await host.ready;
  const env = buildEnv({ tmp });
  const r = {};
  r.env = await host.run('env | sort | grep -iE "proxy|^tmpdir|^path=" | sed -E "s/(:\\/\\/)[^@]*@/\\1<auth>@/"', { cwd: folder, env });
  r.curlAllowed = await host.run('curl -sv -o /dev/null https://registry.npmjs.org/ 2>&1 | tail -25', { cwd: folder, env });
  r.curlAsk = await host.run('curl -sv -o /dev/null https://example.com/ 2>&1 | tail -12', { cwd: folder, env });
  r.node = await host.run('command -v node; node --version; echo "TMPDIR=$TMPDIR"; echo t > "$TMPDIR/t" && echo tmp-ok', { cwd: folder, env });
  console.log('q9 result:', writeResult('q9', { ready, asks, r }));
};
