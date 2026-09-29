const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const { DIR } = require('../lib/results.cjs');

const port = process.parentPort;
const JOURNAL = path.join(DIR, 'q8-journal.jsonl');

function journal(entry) {
  const fd = fs.openSync(JOURNAL, 'a');
  fs.writeSync(fd, `${JSON.stringify(entry)}\n`);
  fs.fsyncSync(fd);
  fs.closeSync(fd);
}

port.on('message', async ({ data }) => {
  fs.writeFileSync(path.join(DIR, 'q8-host.pid'), String(process.pid));
  const { SandboxManager: SM } = await import('@anthropic-ai/sandbox-runtime');
  process.env.CLAUDE_CODE_TMPDIR = data.env.TMPDIR;   // Q2 finding
  process.chdir(data.cwd);                            // Q7 finding
  await SM.initialize(data.policy);
  const tree = "sleep 600 & setsid sleep 600 & (sleep 600 &); wait";
  const { argv } = await SM.wrapWithSandboxArgv(tree, undefined, undefined, undefined, data.cwd);
  const info = { dieWithParent: argv.join(' ').includes('--die-with-parent'), argvSample: argv.slice(0, 12) };
  for (const mode of ['plain', 'scope']) {
    const op = `q8-${mode}`;
    const unit = `surogate-spike-${mode}-${process.pid}`;
    journal({ op, state: 'started', mode, unit: mode === 'scope' ? unit : undefined });
    const cmd = mode === 'scope' ? ['systemd-run', '--user', '--scope', '--quiet', `--unit=${unit}`, '--', ...argv] : argv;
    spawn(cmd[0], cmd.slice(1), { cwd: data.cwd, env: { ...data.env, SPIKE_MARK: op }   /* Q2 finding: app env only */, stdio: 'ignore' });
  }
  port.postMessage({ type: 'running', ...info });
});
