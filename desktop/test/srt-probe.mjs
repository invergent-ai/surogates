// Run in the file helper's sandbox by srt-darwin.test.ts: what a program there can reach of this computer. One JSON
// line out: each try's name, and "ok" or the code it failed with.
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { connect, createServer } from "node:net";
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const at = JSON.parse(process.argv[2]);
const said = {};
const tried = (name, call) => {
  try {
    call();
    said[name] = "ok";
  } catch (error) {
    said[name] = error.code ?? String(error);
  }
};
// A connection: "ok" once it is up, or how it failed, within 3 s.
const reach = (name, options) => new Promise((resolve) => {
  const socket = connect(options);
  const done = (what) => {
    said[name] = what;
    socket.destroy();
    clearTimeout(timer);
    resolve();
  };
  const timer = setTimeout(() => done("timeout"), 3_000);
  socket.once("connect", () => done("ok"));
  socket.once("error", (error) => done(error.code ?? String(error)));
});
const listen = (name, path) => new Promise((resolve) => {
  const server = createServer();
  server.once("listening", () => {
    said[name] = "ok";
    server.close();
    resolve();
  });
  server.once("error", (error) => {
    said[name] = error.code ?? String(error);
    resolve();
  });
  server.listen(path);
});

tried("read the folder", () => readFileSync(join(at.folder, "inside.txt")));
tried("list the folder", () => readdirSync(at.folder));
tried("write the folder", () => writeFileSync(join(at.folder, "made.txt"), "made"));
tried("write the working folder", () => writeFileSync(join(at.tmp, "made.txt"), "made"));
tried("read a key in the home folder", () => readFileSync(join(at.home, ".ssh", "id_ed25519")));
tried("read the key through the data volume", () => readFileSync(join("/System/Volumes/Data", at.home, ".ssh", "id_ed25519")));
tried("list the home folder", () => readdirSync(at.home));
tried("write the home folder", () => writeFileSync(join(at.home, "made.txt"), "made"));
tried("read /etc/master.passwd", () => readFileSync("/etc/master.passwd"));
tried("read another folder", () => readFileSync(join(at.other, "secret.txt")));
tried("list another folder", () => readdirSync(at.other));
tried("write another folder", () => writeFileSync(join(at.other, "made.txt"), "made"));
tried("list /Users", () => readdirSync("/Users"));
tried("load fsat", () => {
  const fsat = createRequire(import.meta.url)(at.fsat);
  const fd = fsat.openat(fsat.constants.AT_FDCWD, join(at.folder, "inside.txt"), fsat.constants.O_RDONLY, 0);
  if (!fsat.getpath(fd).endsWith("inside.txt")) throw new Error("F_GETPATH said another path");
});
tried("load a copy of fsat in the folder", () => createRequire(import.meta.url)(join(at.folder, "copy.node")));
tried("run rg", () => {
  const ran = spawnSync(at.rg, ["--count", "inside", at.folder], { encoding: "utf8" });
  if (ran.error) throw ran.error;
  if (ran.status !== 0) throw new Error(`rg exited ${ran.status}: ${ran.stderr}`);
});
await reach("connect to a unix socket outside", { path: at.sock });
await listen("listen on a unix socket in the folder", join(at.folder, "made.sock"));
await reach("connect to this computer's TCP port", { host: "127.0.0.1", port: at.port });
await reach("connect to the network", { host: "1.1.1.1", port: 443 });
process.stdout.write(`${JSON.stringify(said)}\n`);
