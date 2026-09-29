const dns = require('node:dns/promises');
const http = require('node:http');
const net = require('node:net');

function isPrivate(ip) {
  if (net.isIPv4(ip)) {
    const [a, b] = ip.split('.').map(Number);
    return a === 10 || a === 127 || a === 0 || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31)
      || (a === 192 && b === 168) || (a === 100 && b >= 64 && b <= 127);
  }
  const v = ip.toLowerCase();
  if (v.startsWith('::ffff:')) return isPrivate(v.slice(7));
  return v === '::1' || v === '::' || v.startsWith('fc') || v.startsWith('fd') || v.startsWith('fe80');
}

function guardProxy(log) {
  const server = http.createServer(async (req, res) => {
    req.on('error', () => {});
    res.on('error', () => {});
    const u = new URL(req.url);
    let address;
    try { ({ address } = await dns.lookup(u.hostname)); } catch { res.writeHead(502); res.end(); return; }
    if (isPrivate(address)) { log.push({ proxyBlocked: req.url, address }); res.writeHead(403); res.end(); return; }
    const up = http.request({ host: address, port: u.port || 80, path: u.pathname + u.search, method: req.method, headers: { ...req.headers, host: u.host } },
      (r) => { res.writeHead(r.statusCode, r.headers); r.pipe(res); });
    up.on('error', () => { res.writeHead(502); res.end(); });
    req.pipe(up);
  });
  server.on('connect', async (req, sock, head) => {
    sock.on('error', () => {});
    const [host, portStr] = req.url.split(':');
    let address;
    try { ({ address } = await dns.lookup(host)); } catch { sock.end('HTTP/1.1 502 Bad Gateway\r\n\r\n'); return; }
    if (isPrivate(address)) { log.push({ proxyBlocked: req.url, address }); sock.end('HTTP/1.1 403 Forbidden\r\n\r\n'); return; }
    const up = net.connect(Number(portStr), address, () => {
      sock.write('HTTP/1.1 200 Connection Established\r\n\r\n');
      up.write(head);
      up.pipe(sock);
      sock.pipe(up);
    });
    up.on('error', () => sock.destroy());
    sock.on('close', () => up.destroy());
  });
  return server;
}

module.exports = { isPrivate, guardProxy };
