// Throwaway diagnostic for the Q2 network failure; not one of the eight questions.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { forkHost } = require('../lib/hosts.cjs');
const { buildPolicy } = require('../lib/policy.cjs');
const { buildEnv, sessionTmp } = require('../lib/env.cjs');
const { writeResult } = require('../lib/results.cjs');

exports.run = async () => {
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
