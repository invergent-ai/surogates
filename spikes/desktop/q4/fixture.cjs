const C = 'http://127.0.0.1:18081';

function page(token) {
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

function fixture(pathname, token) {
  if (pathname === '/file.txt') return { status: 200, contentType: 'text/plain', body: 'download body' };
  if (pathname === '/sw.js') {
    return { status: 200, contentType: 'text/javascript', body: `self.addEventListener('install', () => fetch('${C}/sw?t=${token}').catch(() => {}));` };
  }
  return { status: 200, contentType: 'text/html', body: page(token) };
}

module.exports = { fixture };
