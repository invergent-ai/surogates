// Run by this package's Electron as its main, in an Ubuntu container that the install script set up
// with --ca-cert: the app's own trust of the company's CA (dist/shell/company-ca.js), as its main
// does it at start, on that release's certutil and NSS. Laid out as a release is, the Electron
// knows itself installed, and reads /etc/surogate/ca.pem as an installed app does. Then each TLS
// stack against servers of this process: one the CA signed for localhost, one it signed for
// another name, and one the user's own IT signed. It prints what each answered, and exits.
// PROBE_SITES names the servers' folder. With PROBE_BROWSER, nothing of the app's is run first: a
// Chromium of the user's, as their own browser is, which trusts what their database holds.
import { readFileSync } from "node:fs";
import { createServer, get } from "node:https";

import { app, net } from "electron";

import { companyCaFile, trustCompanyCa } from "../dist/shell/company-ca.js";

const companyCa = process.env.PROBE_BROWSER ? undefined : companyCaFile(app.isPackaged, process.env);
const untrusted = companyCa ? trustCompanyCa(companyCa, app.isPackaged, app.getPath("home"), process.env.XDG_DATA_HOME) : null;

const serve = (name) => new Promise((resolve) => {
  const tls = { cert: readFileSync(`${process.env.PROBE_SITES}/${name}.pem`), key: readFileSync(`${process.env.PROBE_SITES}/${name}.key`) };
  const server = createServer(tls, (_request, response) => response.end("ok"));
  server.listen(0, "127.0.0.1", () => resolve(server.address().port));
});
const node = (url) => new Promise((resolve) => {
  get(url, (response) => {
    response.resume();
    resolve(response.statusCode);
  }).on("error", (error) => resolve(error.code));
});
// Asked again where Chromium gave the request up because the computer's network changed, as it
// does when a container beside this one starts or ends: that says nothing of a certificate.
const chromium = async (url) => {
  for (let tries = 0; ; tries += 1) {
    const answer = await net.fetch(url).then((response) => response.status, (error) => error.message);
    if (answer !== "net::ERR_NETWORK_CHANGED" || tries === 20) return answer;
  }
};

app.whenReady().then(async () => {
  const answers = { installed: app.isPackaged, said: untrusted?.detail ?? null };
  for (const name of ["site", "misnamed", "itsite"]) {
    const url = `https://localhost:${await serve(name)}/`;
    answers[name] = { chromium: await chromium(url), node: await node(url) };
  }
  console.log(JSON.stringify(answers));
  app.exit(0);
});
