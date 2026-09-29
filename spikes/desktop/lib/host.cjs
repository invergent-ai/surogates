const { spawn } = require('node:child_process');

const port = process.parentPort;
const pendingAsks = new Map();
let SM;
let askSeq = 0;

const send = (msg) => port.postMessage(msg);

function ask({ host, port: p }) {
  const id = ++askSeq;
  send({ type: 'ask', id, host, port: p });
  return new Promise((resolve) => pendingAsks.set(id, resolve));
}

async function run({ id, command, cwd, env, timeoutMs = 180000, background = false }) {
  // On Linux srt bakes the proxy settings into argv and returns the caller's own
  // process.env as `env`; spawning with it would leak this process's environment
  // into the command and override the one we built. Use ours only.
  const { argv } = await SM.wrapWithSandboxArgv(command, undefined, undefined, undefined, cwd, { commandId: id });
  const child = spawn(argv[0], argv.slice(1), { cwd, env, stdio: ['ignore', 'pipe', 'pipe'] });
  if (background) {
    send({ type: 'started', id, pid: child.pid, argv });
    return;
  }
  let out = '';
  let err = '';
  child.stdout.on('data', (d) => { out += d; });
  child.stderr.on('data', (d) => { err += d; });
  const timer = setTimeout(() => child.kill('SIGKILL'), timeoutMs);
  child.on('close', (code, signal) => {
    clearTimeout(timer);
    send({
      type: 'result', id, code, signal, argv,
      stdout: out.slice(-20000),
      stderr: SM.annotateStderrWithSandboxFailures(command, err).slice(-20000),
    });
  });
}

port.on('message', async ({ data }) => {
  try {
    if (data.type === 'init') {
      ({ SandboxManager: SM } = await import('@anthropic-ai/sandbox-runtime'));
      await SM.initialize(data.policy, ask);
      // srt's proxy bridge sockets live in the host's /tmp, which the read policy
      // hides; re-allow exactly those sockets so the in-sandbox relay can reach them.
      const sockets = [SM.getLinuxHttpSocketPath(), SM.getLinuxSocksSocketPath()].filter(Boolean);
      if (sockets.length) {
        SM.updateConfig({ ...data.policy, filesystem: { ...data.policy.filesystem, allowRead: [...data.policy.filesystem.allowRead, ...sockets] } });
      }
      send({
        type: 'ready', pid: process.pid,
        deps: SM.checkDependencies ? SM.checkDependencies() : null,
        globWarnings: SM.getLinuxGlobPatternWarnings ? SM.getLinuxGlobPatternWarnings() : [],
      });
    } else if (data.type === 'answer') {
      pendingAsks.get(data.id)?.(data.allow);
      pendingAsks.delete(data.id);
    } else if (data.type === 'run') {
      await run(data);
    }
  } catch (e) {
    send({ type: 'error', id: data.id, message: String((e && e.stack) || e) });
  }
});
