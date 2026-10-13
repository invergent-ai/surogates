/** The page each editor runs in: loads the document server's editor and bridges its connector. */
export const HOST_HTML = `<!doctype html>
<html><head><meta charset="utf-8"><title>runtime-office</title>
<style>html,body,#wrap{margin:0;height:100%;width:100%}</style></head>
<body><div id="wrap"><div id="placeholder"></div></div>
<script>
window.events = [];
window.dead = null;
const conn = {
  guid: "asc.{" + crypto.randomUUID() + "}",
  queue: [],
  frame() { return document.querySelector("iframe[name=frameEditor]") || document.querySelector("iframe"); },
  post(data) {
    this.frame().contentWindow.postMessage(JSON.stringify({ type: "onExternalPluginMessage", subType: "connector", data }), "*");
  },
  send(data, timeoutMs) {
    return new Promise((resolve) => {
      const sentAt = Date.now();
      const entry = {};
      entry.timer = setTimeout(() => {
        window.dead = "the editor stopped answering";
        resolve({ sentAt, value: { __timeout: true } });
      }, timeoutMs);
      entry.resolve = (value) => { clearTimeout(entry.timer); resolve({ sentAt, value }); };
      this.queue.push(entry);
      this.post(data);
    });
  },
};
window.addEventListener("message", (event) => {
  let message = event.data;
  if (typeof message === "string") { try { message = JSON.parse(message); } catch { return; } }
  if (!message || message.type !== "onExternalPluginMessageCallback" || !message.data || message.data.guid !== conn.guid) return;
  const data = message.data;
  if (data.type === "onCommandCallback" || data.type === "onMethodReturn") {
    const entry = conn.queue.shift();
    if (entry) entry.resolve(data.type === "onCommandCallback" ? data.commandReturnData : data.methodReturnData);
  }
});
window.startEditor = (config, documentServerUrl) => new Promise((resolve, reject) => {
  const script = document.createElement("script");
  script.src = documentServerUrl.replace(/[/]$/, "") + "/web-apps/apps/api/documents/api.js";
  script.onerror = () => reject(new Error("the editor did not load from " + documentServerUrl));
  script.onload = () => {
    const record = (name) => (event) => window.events.push({ t: Date.now(), name, data: event && event.data });
    config.events = {
      onDocumentReady: () => {
        record("onDocumentReady")();
        conn.post({ type: "register", guid: conn.guid });
        setTimeout(() => resolve(true), 500);
      },
      onDocumentStateChange: record("onDocumentStateChange"),
      onError: (event) => {
        window.dead = "editor error " + JSON.stringify(event && event.data);
        reject(new Error(window.dead));
      },
      onRequestClose: () => { window.dead = "the editor closed"; },
    };
    window.docEditor = new DocsAPI.DocEditor("placeholder", config);
  };
  document.head.appendChild(script);
});
window.runCommand = (code, timeoutMs) => conn.send({ type: "command", guid: conn.guid, data: code, recalculate: true }, timeoutMs);
window.runMethod = (name, args, timeoutMs) => conn.send({ type: "method", guid: conn.guid, methodName: name, data: args }, timeoutMs);
window.waitFlushed = (since, timeoutMs) => new Promise((resolve) => {
  const started = Date.now();
  (function poll() {
    const changes = window.events.filter((e) => e.name === "onDocumentStateChange" && e.t >= since);
    const last = changes[changes.length - 1];
    if (last && last.data === false) return resolve(true);
    if (Date.now() - started > timeoutMs) return resolve(false);
    setTimeout(poll, 20);
  })();
});
</script></body></html>`;
