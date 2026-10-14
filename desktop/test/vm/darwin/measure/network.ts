// macOS 15's Local Network privacy, for what the VM manager's host proxy dials for a command (the guest
// has no network device: its one way out is the net port and this proxy, src/vm/proxy.ts): a command
// fetches a page from a server on this Mac's own network address, the proxy asked and allowing it, as a
// chat's approval would. Also the inbound port: a server in a root, reached from the host as the browser
// reaches a chat's own server. Decides the binding's and the prompt's words, and the Info.plist's
// NSLocalNetworkUsageDescription.
//   npx vitest run -c test/vm/darwin/measure/vitest.config.ts network
// If macOS asks to let an app find devices on the local network, write down the app it names, and allow it.
// PASS or FAIL: the command reached the server through the proxy, and the host reached the root's server.

import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { networkInterfaces } from "node:os";
import type { Duplex } from "node:stream";

import { expect, it } from "vitest";

import { ALLOWING, lab, ready, record, ROOTS, scratch, signal } from "./harness.js";

it("reaches this Mac's own network from a command, through the proxy, and a root's server from the host", async () => {
  const address = Object.values(networkInterfaces()).flat().find((each) => each && each.family === "IPv4" && !each.internal)?.address;
  const server = createServer((_, response) => response.end("surogate-measure\n"));
  await new Promise<void>((resolve) => server.listen(0, "0.0.0.0", resolve));
  const port = (server.address() as AddressInfo).port;
  const booted = await lab("network", {}, ALLOWING);
  const root = ROOTS[0]!;
  await ready(booted.guest, root, scratch("network"));
  console.log("MEASURE network: if macOS asks to let an app find devices on the local network, note the app, and allow it");
  const fetched = address ? (await booted.run(root, `curl -s -m 20 http://${address}:${port}/ || echo "curl ended $?"`, 60)).output.trim() : "this Mac has no network address";
  await booted.guest.op(root, "start", { command: "python3 -m http.server 8765 --bind 127.0.0.1", workdir: null, task_id: "server", pty: false, notify_on_complete: false, watcher_interval: null }, signal());
  await new Promise((resolve) => setTimeout(resolve, 2_000));
  const reached = await booted.guest.reach(root, 8765);
  let inbound = typeof reached === "string" ? `not reached: ${reached}` : "";
  if (typeof reached !== "string") {
    const stream = reached as Duplex;
    inbound = await new Promise<string>((resolve) => {
      let said = "";
      stream.on("data", (chunk: Buffer) => (said += chunk.toString()));
      stream.on("end", () => resolve(said.split("\r\n")[0] ?? ""));
      stream.on("error", (error) => resolve(`failed: ${error.message}`));
      stream.write("GET / HTTP/1.0\r\nHost: localhost\r\n\r\n");
    });
  }
  await booted.stop();
  server.close();
  const failed = record("network", { "this Mac's address": address ?? "none", "the command's fetch": fetched, "the root's server, from the host": inbound }, {
    "the command reached this Mac's own network through the proxy": fetched === "surogate-measure",
    "the host reached the root's own server": /^HTTP\/1\.[01] 200/.test(inbound),
  });
  expect(failed).toEqual([]);
});
