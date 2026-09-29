const { utilityProcess } = require('electron');
const path = require('node:path');

function forkHost(name, { policy, onAsk }) {
  const child = utilityProcess.fork(path.join(__dirname, 'host.cjs'), [], { serviceName: name, stdio: 'inherit' });
  const waiters = new Map();
  let seq = 0;
  let readyResolve;
  const ready = new Promise((r) => { readyResolve = r; });
  child.on('message', (msg) => {
    if (msg.type === 'ready' || (msg.type === 'error' && !msg.id)) readyResolve(msg);
    else if (msg.type === 'ask') {
      Promise.resolve(onAsk ? onAsk(msg) : false)
        .then((allow) => child.postMessage({ type: 'answer', id: msg.id, allow }));
    } else if (waiters.has(msg.id)) {
      waiters.get(msg.id)(msg);
      waiters.delete(msg.id);
    }
  });
  child.postMessage({ type: 'init', policy });
  return {
    child,
    ready,
    run(command, opts = {}) {
      const id = `${name}-${++seq}`;
      child.postMessage({ type: 'run', id, command, ...opts });
      return new Promise((r) => waiters.set(id, r));
    },
  };
}

module.exports = { forkHost };
