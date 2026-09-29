const { utilityProcess } = require('electron');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { buildPolicy } = require('../lib/policy.cjs');
const { buildEnv, sessionTmp } = require('../lib/env.cjs');
const { writeResult, DIR } = require('../lib/results.cjs');

const JOURNAL = path.join(DIR, 'q8-journal.jsonl');

function survivors() {
  const byMark = {};
  for (const pid of fs.readdirSync('/proc').filter((p) => /^\d+$/.test(p))) {
    try {
      const mark = fs.readFileSync(`/proc/${pid}/environ`, 'utf8').split('\0').find((e) => e.startsWith('SPIKE_MARK=q8-'));
      if (mark) byMark[mark.slice(11)] = (byMark[mark.slice(11)] || 0) + 1;
    } catch { /* process gone or not ours */ }
  }
  return byMark;
}

exports.run = async ({ arg, argv }) => {
  fs.mkdirSync(DIR, { recursive: true });
  if (arg === 'q8') {
    fs.writeFileSync(path.join(DIR, 'q8-main.pid'), String(process.pid));
    const folder = fs.mkdtempSync(path.join(os.homedir(), 'spike-q8-'));
    const tmp = sessionTmp('q8');
    const host = utilityProcess.fork(path.join(__dirname, 'tree-host.cjs'), [], { serviceName: 'q8-tree', stdio: 'inherit' });
    const info = await new Promise((resolve) => {
      host.once('message', resolve);
      host.postMessage({ policy: buildPolicy({ folder, tmp, allowedDomains: [], variant: process.env.SPIKE_VARIANT || 'root-deny' }), cwd: folder, env: buildEnv({ tmp }) });
    });
    fs.writeFileSync(path.join(DIR, 'q8-start-info.json'), JSON.stringify(info, null, 2));
    await new Promise(() => {});   // stay alive until the scenario kills us
  }
  // q8-recover: what a replacement host does before accepting work.
  const victim = argv.includes('main') ? 'main' : 'host';
  const before = survivors();
  const entries = fs.readFileSync(JOURNAL, 'utf8').trim().split('\n').map((l) => JSON.parse(l));
  const done = new Set(entries.filter((e) => e.state === 'result').map((e) => e.op));
  const interrupted = entries.filter((e) => e.state === 'started' && !done.has(e.op)).map((e) => e.op);
  const stops = {};
  for (const e of entries.filter((x) => x.unit)) {
    try { execFileSync('systemctl', ['--user', 'stop', `${e.unit}.scope`], { stdio: 'pipe' }); stops[e.unit] = 'stopped'; } catch (err) { stops[e.unit] = String(err.stderr || err); }
  }
  await new Promise((r) => setTimeout(r, 1500));
  let userManager;
  try { userManager = execFileSync('systemctl', ['--user', 'is-system-running'], { encoding: 'utf8' }).trim(); } catch (err) { userManager = String(err.stdout || err).trim(); }
  const startInfo = JSON.parse(fs.readFileSync(path.join(DIR, 'q8-start-info.json'), 'utf8'));
  console.log('q8 result:', writeResult(`q8-${victim}`, { victim, startInfo, survivorsBeforeRecover: before, interrupted, stops, survivorsAfterRecover: survivors(), userManager }));
};
