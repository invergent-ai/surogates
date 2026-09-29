const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { forkHost } = require('../lib/hosts.cjs');
const { buildPolicy } = require('../lib/policy.cjs');
const { buildEnv, sessionTmp } = require('../lib/env.cjs');
const { writeResult } = require('../lib/results.cjs');

const base = path.join(os.homedir(), 'spike-roots');
const roots = { A: path.join(base, 'A'), B: path.join(base, 'B My Files ü'), C: path.join(base, 'C-link') };
const DOMAINS = ['registry.npmjs.org', 'pypi.org', 'files.pythonhosted.org'];
const CURL = (url) => `curl -sS -o /dev/null -w '%{http_code}' ${url}`;

exports.run = async () => {
  const asks = [];
  const results = {};
  for (const variant of ['root-deny', 'home-deny']) {
    const tmps = {};
    const hosts = {};
    for (const k of Object.keys(roots)) {
      tmps[k] = sessionTmp(`q2-${k}-${variant}`);
      hosts[k] = forkHost(`q2-${k}-${variant}`, {
        policy: buildPolicy({ folder: roots[k], tmp: tmps[k], allowedDomains: DOMAINS, variant }),
        tmp: tmps[k],
        onAsk: (m) => { asks.push({ variant, root: k, host: m.host, port: m.port }); return k === 'A'; },
      });
    }
    const readies = Object.fromEntries(await Promise.all(Object.entries(hosts).map(async ([k, h]) => [k, await h.ready])));
    const run = (k, cmd) => hosts[k].run(cmd, { cwd: fs.realpathSync(roots[k]), env: buildEnv({ tmp: tmps[k] }) });
    const other = (k) => (k === 'A' ? roots.B : roots.A);
    const checks = {};
    await Promise.all(Object.keys(roots).map(async (k) => {
      const c = {};
      c.ownWrite = await run(k, 'echo hi > own.txt && cat own.txt');
      c.privateDoc = await run(k, 'cat ~/Documents/private.txt');
      c.sshSecret = await run(k, 'cat ~/.ssh/spike_secret');
      c.otherRead = await run(k, `cat "${other(k)}/seed.txt"`);
      c.otherWrite = await run(k, `echo x > "${other(k)}/pwn.txt"`);
      c.tmpMarker = await run(k, `echo ${k} > /tmp/spike-shared-${k}; ls /tmp | head -20`);
      c.tmpdirWrite = await run(k, 'echo t > "$TMPDIR/t" && cat "$TMPDIR/t"');
      c.tools = await run(k, 'node --version; git --version; python3 -c "print(1)"');
      c.allowedNet = await run(k, CURL('https://registry.npmjs.org/'));
      c.askNet = await run(k, CURL('https://example.com/'));
      c.askNetAgain = await run(k, CURL('https://example.com/'));
      // npm init -y would derive an invalid package name from 'B My Files ü'.
      c.npm = await run(k, 'printf \'{"name":"probe","version":"1.0.0"}\' > package.json && npm install left-pad --no-audit --no-fund && ls "$npm_config_cache"');
      c.pip = await run(k, 'python3 -m venv .venv && .venv/bin/pip install -q six && ls "$PIP_CACHE_DIR"');
      c.dockerSock = await run(k, 'if [ -S /var/run/docker.sock ]; then curl -sS --unix-socket /var/run/docker.sock http://x/version; else echo no-docker-socket; fi');
      c.sessionBus = await run(k, 'busctl --user list 2>&1 | head -3');
      checks[k] = c;
    }));
    checks.crossTmp = {
      AreadsB: await run('A', 'cat /tmp/spike-shared-B'),
      BreadsA: await run('B', 'cat /tmp/spike-shared-A'),
    };
    results[variant] = { readies, checks };
    for (const h of Object.values(hosts)) h.child.kill();
  }
  console.log('q2 result:', writeResult('q2', { roots, asks, results }));
};
