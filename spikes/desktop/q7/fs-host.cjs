const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const port = process.parentPort;

function lineClient(child) {
  const replies = new Map();
  let buf = '';
  child.stdout.on('data', (d) => {
    buf += d;
    let i;
    while ((i = buf.indexOf('\n')) >= 0) { const m = JSON.parse(buf.slice(0, i)); buf = buf.slice(i + 1); replies.get(m.id)?.(m); replies.delete(m.id); }
  });
  let seq = 0;
  return (msg) => new Promise((resolve) => { const id = ++seq; replies.set(id, resolve); child.stdin.write(`${JSON.stringify({ id, ...msg })}\n`); });
}

function baselineWrite(root, rel, data) {
  const target = path.join(root, rel);
  const dir = fs.realpathSync(path.dirname(target));
  if (!dir.startsWith(root)) throw new Error('outside');
  fs.writeFileSync(target, data);   // time-of-check / time-of-use gap on purpose
}

port.on('message', async ({ data }) => {
  const { SandboxManager: SM } = await import('@anthropic-ai/sandbox-runtime');
  process.env.CLAUDE_CODE_TMPDIR = data.env.TMPDIR;   // Q2 finding
  // srt derives its mandatory denies (.git/config, .bashrc, …) from the process's cwd.
  if (process.env.SPIKE_CHDIR) process.chdir(data.root);
  await SM.initialize(data.policy);
  const wrap = async (cmd) => SM.wrapWithSandboxArgv(cmd, undefined, undefined, undefined, data.root);
  const spawnWrapped = async (cmd, stdio) => { const { argv } = await wrap(cmd); return spawn(argv[0], argv.slice(1), { cwd: data.root, env: data.env, stdio }); };   // Q2 finding: app env only
  const runWrapped = async (cmd) => new Promise(async (resolve) => {
    const c = await spawnWrapped(cmd, ['ignore', 'pipe', 'pipe']);
    let out = ''; c.stdout.on('data', (d) => { out += d; }); c.stderr.on('data', (d) => { out += d; });
    c.on('close', (code) => resolve({ code, out: out.slice(-2000) }));
  });
  const res = {};
  const outside = data.outside;
  const helper = await spawnWrapped(`ELECTRON_RUN_AS_NODE=1 '${process.execPath}' '${path.join(__dirname, 'fsop.cjs')}'`, ['pipe', 'pipe', 'pipe']);
  const op = lineClient(helper);

  // Race: a sandboxed command flips sub/ between a directory and a symlink to outside.
  const attackerCmd = `while :; do rm -rf sub; mkdir sub; rm -rf sub; ln -s '${outside}' sub; done`;
  for (const [name, write] of [
    ['A', (i) => op({ op: 'write', path: `sub/t-A-${i}.txt`, data: 'x' })],
    ['B', async (i) => { try { baselineWrite(data.root, `sub/t-B-${i}.txt`, 'x'); } catch { /* lost the race cleanly */ } }],
  ]) {
    const attacker = await spawnWrapped(attackerCmd, ['ignore', 'ignore', 'ignore']);
    for (let i = 0; i < 5000; i++) await write(i);
    attacker.kill('SIGKILL');
    await new Promise((r) => setTimeout(r, 500));
    res[`landedOutside_${name}`] = fs.readdirSync(outside).filter((f) => f.startsWith(`t-${name}-`)).length;
  }

  // Hard links (Review Focus 1).
  res.sandboxedHardlinkToToolchain = await runWrapped(`ln '${data.toolchainFile}' ./hl-toolchain; echo "exit=$?"`);
  fs.linkSync(data.outsideFile, path.join(data.root, 'hl-user'));   // made outside the sandbox, as a user could
  res.helperWriteThroughUserHardlink = await op({ op: 'write', path: 'hl-user', data: 'pwned-by-helper' });
  res.commandWriteThroughUserHardlink = await runWrapped("echo pwned-by-command >> hl-user; echo \"exit=$?\"");
  res.outsideFileAfter = fs.readFileSync(data.outsideFile, 'utf8');

  // Special files and magic links.
  await runWrapped('mkfifo fifo; ln -sf /dev/zero z; ln -sfn /proc/self/root pr');
  const started = Date.now();
  res.readFifo = { ...(await op({ op: 'read', path: 'fifo' })), ms: Date.now() - started };
  res.readDevZeroLink = await op({ op: 'read', path: 'z' });
  res.readThroughMagicLink = await op({ op: 'read', path: `pr${data.outsideFile}` });

  // Protected paths inside the folder.
  fs.mkdirSync(path.join(data.root, '.git'), { recursive: true });
  fs.writeFileSync(path.join(data.root, '.git/config'), '[core]\n');
  res.helperWriteGitConfig = await op({ op: 'write', path: '.git/config', data: 'x' });
  res.commandWriteGitConfig = await runWrapped('echo x >> .git/config; echo "exit=$?"');

  helper.kill();
  port.postMessage(res);
});
