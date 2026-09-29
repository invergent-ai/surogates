const { app, BrowserWindow } = require('electron');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { forkHost } = require('../lib/hosts.cjs');
const { buildPolicy } = require('../lib/policy.cjs');
const { buildEnv, sessionTmp } = require('../lib/env.cjs');
const { writeResult } = require('../lib/results.cjs');

const read = (p) => { try { return fs.readFileSync(p, 'utf8').trim(); } catch (e) { return `unreadable: ${e.code}`; } };
const link = (p) => { try { return fs.readlinkSync(p); } catch (e) { return `unreadable: ${e.code}`; } };
const seccomp = (pid) => /Seccomp:\s+(\d)/.exec(read(`/proc/${pid}/status`))?.[1] ?? null;

exports.run = async () => {
  const folder = fs.mkdtempSync(path.join(os.homedir(), 'spike-q1-'));
  const tmp = sessionTmp('q1');
  const win = new BrowserWindow({ show: false, webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } });
  await win.loadURL('about:blank');
  const host = forkHost('q1', { policy: buildPolicy({ folder, tmp, allowedDomains: [], variant: 'home-deny' }) });
  const ready = await host.ready;
  const cmd = await host.run(
    'id -u; cat /proc/self/attr/current; readlink /proc/self/ns/user; touch ok && echo wrote',
    { cwd: folder, env: buildEnv({ tmp }) },
  );
  const processes = app.getAppMetrics().map((m) => ({
    type: m.type, pid: m.pid,
    apparmor: read(`/proc/${m.pid}/attr/current`),
    userns: link(`/proc/${m.pid}/ns/user`),
    seccomp: seccomp(m.pid),
    cmdline: read(`/proc/${m.pid}/cmdline`).split('\0').filter((a) => a.includes('sandbox')),
  }));
  const file = writeResult('q1', {
    restrictUserns: read('/proc/sys/kernel/apparmor_restrict_unprivileged_userns'),
    mainUserns: link(`/proc/${process.pid}/ns/user`),
    processes, ready, cmd,
    wroteInFolder: fs.existsSync(path.join(folder, 'ok')),
  });
  console.log('q1 result:', file);
};
