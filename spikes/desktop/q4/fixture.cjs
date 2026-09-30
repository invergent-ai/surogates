const C = 'http://127.0.0.1:18081';

function page(token, lan) {
  return `<!doctype html><title>fixture</title><body>
<a id="dl" href="data:text/plain,download%20body" download="../../escape.txt">download</a>
<input id="file" type="file">
<iframe src="${C}/iframe?t=${token}"></iframe>
<img src="${C}/img?t=${token}">
<link rel="prefetch" href="${C}/prefetch?t=${token}">
<script>
const T = ${JSON.stringify(token)}, C = ${JSON.stringify(C)};
const q = (p) => C + p + '?t=' + T;
fetch(q('/fetch')).catch(() => {});
fetch('http://localhost:18081/localhost?t=' + T).catch(() => {});
fetch('http://127.0.0.1.nip.io:18081/nip?t=' + T).catch(() => {});
if (${JSON.stringify(Boolean(lan))}) fetch('http://${lan}:18083/lan?t=' + T).catch(() => {});
window.__ice = null;
try {
  const pc = new RTCPeerConnection({ iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] });
  const cands = [];
  pc.onicecandidate = (e) => { if (e.candidate) cands.push(e.candidate.candidate); };
  pc.createDataChannel('x');
  pc.createOffer().then((o) => pc.setLocalDescription(o));
  setTimeout(() => { window.__ice = cands; }, 4000);
} catch (e) { window.__ice = ['error ' + e]; }
fetch('https://httpbin.org/redirect-to?url=' + encodeURIComponent(q('/redirect'))).catch(() => {});
const x = new XMLHttpRequest(); x.open('GET', q('/xhr')); x.send();
navigator.sendBeacon(q('/beacon'), 'x');
try { new EventSource(q('/sse')); } catch (e) {}
try { new WebSocket('ws://127.0.0.1:18082/ws?t=' + T); } catch (e) {}
new Worker(URL.createObjectURL(new Blob(["fetch('" + q('/worker') + "').catch(() => {})"])));
try { new SharedWorker(URL.createObjectURL(new Blob(["fetch('" + q('/shared') + "').catch(() => {})"]))); } catch (e) {}
if (navigator.serviceWorker) navigator.serviceWorker.register('/sw.js?t=' + T).catch(() => {});
</script>`;
}

function fixture(pathname, token, lan) {
  if (pathname === '/file.txt') return { status: 200, contentType: 'text/plain', body: 'download body' };
  if (pathname === '/sw.js') {
    return { status: 200, contentType: 'text/javascript', body: `self.addEventListener('install', () => fetch('${C}/sw?t=${token}').catch(() => {}));` };
  }
  return { status: 200, contentType: 'text/html', body: page(token, lan) };
}

module.exports = { fixture };
