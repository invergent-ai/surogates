const { utilityProcess } = require('electron');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { buildPolicy, toolchainDirs } = require('../lib/policy.cjs');
const { buildEnv, sessionTmp } = require('../lib/env.cjs');
const { writeResult } = require('../lib/results.cjs');

exports.run = async () => {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.homedir(), 'spike-q7-')));
  const outside = fs.mkdtempSync(path.join(os.homedir(), '.config/spike-q7-outside-'));
  const outsideFile = path.join(os.homedir(), 'Documents/q7-outside.txt');
  fs.mkdirSync(path.dirname(outsideFile), { recursive: true });
  fs.writeFileSync(outsideFile, 'original\n');
  const toolchainFile = path.join(toolchainDirs().find((d) => d.endsWith('.nvm')) || '/usr/bin', 'nvm.sh');
  const tmp = sessionTmp('q7');
  const policy = buildPolicy({ folder: root, tmp, allowedDomains: [], variant: process.env.SPIKE_VARIANT || 'root-deny', extraRead: [path.dirname(process.execPath)] });
  const host = utilityProcess.fork(path.join(__dirname, 'fs-host.cjs'), [], { serviceName: 'q7-fs', stdio: 'inherit' });
  const res = await new Promise((resolve) => {
    host.once('message', resolve);
    host.postMessage({ policy, root, env: buildEnv({ tmp }), outside, outsideFile, toolchainFile });
  });
  host.kill();
  console.log('q7 result:', writeResult('q7', res));
};
