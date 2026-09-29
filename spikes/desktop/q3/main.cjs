const { utilityProcess } = require('electron');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { forkHost } = require('../lib/hosts.cjs');
const { buildPolicy } = require('../lib/policy.cjs');
const { buildEnv, sessionTmp } = require('../lib/env.cjs');
const { writeResult } = require('../lib/results.cjs');

const statusOf = (url) => new Promise((resolve) => {
  http.get(url, (r) => { r.resume(); resolve(r.statusCode); }).on('error', (e) => resolve(String(e.code || e)));
});
const variant = process.env.SPIKE_VARIANT || 'root-deny';
const appDir = path.dirname(process.execPath);

exports.run = async () => {
  const folder = fs.mkdtempSync(path.join(os.homedir(), 'spike-q3-'));
  const tmp = sessionTmp('q3');
  const env = buildEnv({ tmp });
  const policy = buildPolicy({ folder, tmp, allowedDomains: [], variant, extraRead: [appDir] });
  const r = {};

  // Separate commands, each in its own sandbox.
  const host = forkHost('q3', { policy, tmp });
  r.ready = await host.ready;
  r.background = await host.run('python3 -m http.server 18765 --bind 127.0.0.1', { cwd: folder, env, background: true });
  await new Promise((res) => setTimeout(res, 1500));
  r.nextCommand = await host.run("curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:18765/", { cwd: folder, env });
  r.fromOutside = await statusOf('http://127.0.0.1:18765/');
  host.child.kill();

  // One long-lived sandbox for the session, plus forwarding over an inherited descriptor.
  const runnerHost = utilityProcess.fork(path.join(__dirname, 'runner-host.cjs'), [], { serviceName: 'q3-runner', stdio: 'inherit' });
  r.runner = await new Promise((resolve) => {
    runnerHost.once('message', resolve);
    runnerHost.postMessage({ policy, cwd: fs.realpathSync(folder), env });
  });
  runnerHost.kill();
  console.log('q3 result:', writeResult('q3', r));
};
