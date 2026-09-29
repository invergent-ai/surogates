const { spawn } = require('node:child_process');
const net = require('node:net');

const reply = (m) => process.stdout.write(`${JSON.stringify(m)}\n`);

function handle(msg) {
  if (msg.forward) {
    const tunnel = new net.Socket({ fd: 3, readable: true, writable: true });
    const upstream = net.connect(msg.forward.port, '127.0.0.1', () => {
      tunnel.pipe(upstream);
      upstream.pipe(tunnel);
      reply({ id: msg.id, forwarding: true });
    });
    upstream.on('error', (e) => reply({ id: msg.id, error: String(e) }));
    return;
  }
  const child = spawn('bash', ['-c', msg.cmd], { stdio: ['ignore', 'pipe', 'pipe'] });
  if (msg.background) {
    reply({ id: msg.id, pid: child.pid });
    return;
  }
  let out = '';
  let err = '';
  child.stdout.on('data', (d) => { out += d; });
  child.stderr.on('data', (d) => { err += d; });
  child.on('close', (code) => reply({ id: msg.id, code, out, err }));
}

let buf = '';
process.stdin.on('data', (d) => {
  buf += d;
  let i;
  while ((i = buf.indexOf('\n')) >= 0) {
    handle(JSON.parse(buf.slice(0, i)));
    buf = buf.slice(i + 1);
  }
});
