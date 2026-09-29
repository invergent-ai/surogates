const os = require('node:os');
const fs = require('node:fs');
const path = require('node:path');
const { forkHost } = require('../lib/hosts.cjs');
const { buildPolicy } = require('../lib/policy.cjs');
const { buildEnv, sessionTmp } = require('../lib/env.cjs');
const { writeResult } = require('../lib/results.cjs');

exports.run = async () => {
  const folder = fs.mkdtempSync(path.join(os.homedir(), 'spike-q0-'));
  const tmp = sessionTmp('q0');
  const host = forkHost('q0', { policy: buildPolicy({ folder, tmp, allowedDomains: [], variant: 'home-deny' }) });
  const ready = await host.ready;
  const echo = await host.run('echo sandboxed-ok && pwd', { cwd: folder, env: buildEnv({ tmp }) });
  console.log('q0 result:', writeResult('q0', { ready, echo }));
};
