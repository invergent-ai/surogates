// surogate-vsock (images/guest/surogate-vsock.c), the guest's streams to the host on Hyper-V's sockets,
// built here and run: against the kernel's vsock loopback where this computer has it, and always against
// a stand-in compiled for the test, which takes each AF_VSOCK stream for a Unix socket's, at <dir>/<cid>-<port>.

import { execFileSync, spawn, spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, readFileSync, readlinkSync, rmSync, statSync, writeFileSync } from "node:fs";
import { connect, createServer, type Server, type Socket } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

const SOURCE = fileURLToPath(new URL("../../images/guest/surogate-vsock.c", import.meta.url));
const USAGE = "usage: surogate-vsock <cid>:<port>:<fd>... -- <program> <arg>... | surogate-vsock <cid>:<port>=<path>...\n";
// The stand-in: AF_VSOCK's sockets made as Unix ones, and each connect to <cid>:<port> one to $SHIM/<cid>-<port>.
const SHIM = `#define _GNU_SOURCE
#include <dlfcn.h>
#include <linux/vm_sockets.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <sys/un.h>
int socket(int domain, int type, int protocol) {
  int (*real)(int, int, int) = (int (*)(int, int, int))dlsym(RTLD_NEXT, "socket");
  return domain == AF_VSOCK ? real(AF_UNIX, type, 0) : real(domain, type, protocol);
}
int connect(int fd, const struct sockaddr *address, socklen_t length) {
  int (*real)(int, const struct sockaddr *, socklen_t) = (int (*)(int, const struct sockaddr *, socklen_t))dlsym(RTLD_NEXT, "connect");
  if (address->sa_family != AF_VSOCK) return real(fd, address, length);
  const struct sockaddr_vm *vm = (const struct sockaddr_vm *)address;
  struct sockaddr_un un = { .sun_family = AF_UNIX };
  snprintf(un.sun_path, sizeof un.sun_path, "%s/%u-%u", getenv("SHIM"), vm->svm_cid, vm->svm_port);
  return real(fd, (struct sockaddr *)&un, sizeof un);
}
`;

const cc = ["cc", "gcc", "clang"].find((c) => spawnSync(c, ["--version"]).status === 0);
let build: string;
let program: string;
let shim: string;
beforeAll(() => {
  // Without a compiler every test here fails, as the drift test's do: skipped, they would show nothing.
  if (!cc) throw new Error("surogate-vsock's tests need a C compiler, and none of cc, gcc or clang runs on this PATH");
  build = mkdtempSync(join(tmpdir(), "surogate-vsock-"));
  program = join(build, "surogate-vsock");
  execFileSync(cc, ["-O2", "-Wall", "-Wextra", "-Werror", "-o", program, SOURCE]);
  writeFileSync(join(build, "shim.c"), SHIM);
  shim = join(build, "shim.so");
  execFileSync(cc, ["-shared", "-fPIC", "-o", shim, join(build, "shim.c"), "-ldl"]);
});
afterAll(() => {
  if (build) rmSync(build, { recursive: true, force: true });
});

let dir: string;
const servers: Server[] = [];
afterEach(async () => {
  await Promise.all(servers.splice(0).map((server) => new Promise((resolve) => server.close(resolve))));
  if (dir) rmSync(dir, { recursive: true, force: true });
});
const fresh = () => (dir = mkdtempSync(join(tmpdir(), "vsock-")));
const env = () => ({ PATH: "/usr/bin:/bin", LD_PRELOAD: shim, SHIM: dir });
// The host's end of <cid>:<port>, through the stand-in: each connection it takes, given to *took*.
const host = (cid: number, port: number, took: (socket: Socket) => void = () => {}) => new Promise<void>((resolve) => {
  const server = createServer(took);
  servers.push(server);
  server.listen(join(dir, `${cid}-${port}`), resolve);
});
const run = (...args: string[]) => spawnSync(program, args, { encoding: "utf8", env: env(), timeout: 10_000 });
const heardOn = (cid: number, port: number) => new Promise<string>((resolve) => {
  void host(cid, port, (socket) => {
    let said = "";
    socket.on("data", (chunk) => (said += chunk.toString())).on("end", () => resolve(said));
  });
});

describe("its descriptor form, which a share's mount takes", () => {
  it("connects a stream to each port and puts it at its descriptor, the program's only ones but its three", async () => {
    fresh();
    await host(2, 5000);
    await host(2, 5001);
    const sleeping = spawn(program, ["2:5000:3", "2:5001:7", "--", "/bin/sleep", "10"], { env: env(), stdio: "ignore" });
    try {
      let fds: string[] = [];
      for (const end = Date.now() + 5_000; Date.now() < end; await new Promise((resolve) => setTimeout(resolve, 20))) {
        try {
          if (readFileSync(`/proc/${sleeping.pid}/cmdline`, "utf8").startsWith("/bin/sleep\0")) {
            fds = readdirSync(`/proc/${sleeping.pid}/fd`).sort();
            break;
          }
        } catch {
          // Not started yet.
        }
      }
      expect(fds).toEqual(["0", "1", "2", "3", "7"]);
      for (const fd of ["3", "7"]) expect(readlinkSync(`/proc/${sleeping.pid}/fd/${fd}`)).toMatch(/^socket:\[\d+\]$/);
    } finally {
      sleeping.kill("SIGKILL");
    }
  });

  it("gives the program the streams themselves: what it writes there reaches each port", async () => {
    fresh();
    const three = heardOn(2, 564);
    const four = heardOn(2, 565);
    expect(run("2:564:4", "2:565:3", "--", "/bin/sh", "-c", "printf four >&4; printf three >&3")).toMatchObject({ status: 0, stderr: "" });
    expect([await three, await four]).toEqual(["four", "three"]);
  });

  it("runs nothing, and says which, when a port refuses its connection", async () => {
    fresh();
    await host(2, 5000);
    const ran = run("2:5000:3", "2:5001:4", "--", "/bin/sh", "-c", `touch ${join(dir, "ran")}`);
    expect(ran).toMatchObject({ status: 1, stderr: "surogate-vsock: cannot connect to 2:5001: No such file or directory\n" });
    expect(existsSync(join(dir, "ran"))).toBe(false);
  });
});

describe("its relay, which the agent's ports take", () => {
  it("relays a port's stream to the one connection made to its socket, mode 0600, which then goes; and exits once both ways have ended", async () => {
    fresh();
    let hostEnd: Socket | undefined;
    await host(2, 5000, (socket) => {
      hostEnd = socket;
      socket.on("data", (chunk) => socket.write(`heard ${chunk.toString()}`));
    });
    const path = join(dir, "ai.surogate.control");
    const relay = spawn(program, [`2:5000=${path}`], { env: env(), stdio: ["ignore", "ignore", "pipe"] });
    const exited = new Promise<number | null>((resolve) => relay.once("exit", resolve));
    try {
      for (const end = Date.now() + 5_000; !existsSync(path) && Date.now() < end;) await new Promise((resolve) => setTimeout(resolve, 10));
      expect(statSync(path).isSocket()).toBe(true);
      expect(statSync(path).mode & 0o777).toBe(0o600);
      const agent = await new Promise<Socket>((resolve, reject) => {
        const socket = connect(path, () => resolve(socket));
        socket.once("error", reject);
      });
      const heard = new Promise<string>((resolve) => agent.once("data", (chunk: Buffer) => resolve(chunk.toString())));
      agent.write("hello\n");
      expect(await heard).toBe("heard hello\n");
      // The socket has gone, and takes no second connection.
      expect(existsSync(path)).toBe(false);
      // The host's end goes: the agent's port ends; then the agent's, and the relay is done.
      const ended = new Promise<void>((resolve) => agent.once("end", resolve));
      agent.resume();
      hostEnd!.end();
      await ended;
      agent.end();
      expect(await exited).toBe(0);
    } finally {
      relay.kill("SIGKILL");
    }
  });

  it("relays each of several ports apart, and listens at none when one refuses", async () => {
    fresh();
    for (const port of [5000, 5001]) await host(2, port, (socket) => socket.on("data", (chunk) => socket.end(`${port} ${chunk.toString()}`)));
    const relay = spawn(program, [`2:5000=${join(dir, "control")}`, `2:5001=${join(dir, "net")}`], { env: env(), stdio: "ignore" });
    try {
      const through = async (name: string) => {
        for (const end = Date.now() + 5_000; !existsSync(join(dir, name)) && Date.now() < end;) await new Promise((resolve) => setTimeout(resolve, 10));
        return new Promise<string>((resolve) => {
          let said = "";
          const socket = connect(join(dir, name), () => socket.write(name));
          socket.on("data", (chunk) => (said += chunk.toString())).on("end", () => resolve(said));
        });
      };
      expect(await Promise.all([through("net"), through("control")])).toEqual(["5001 net", "5000 control"]);
    } finally {
      relay.kill("SIGKILL");
    }
    expect(run(`2:5000=${join(dir, "a")}`, `2:5009=${join(dir, "b")}`)).toMatchObject({ status: 1, stderr: "surogate-vsock: cannot connect to 2:5009: No such file or directory\n" });
    expect(readdirSync(dir).sort()).toEqual(["2-5000", "2-5001"]);
  });
});

describe("its arguments", () => {
  it("refuses any it does not take, and runs nothing", () => {
    fresh();
    for (const args of [
      [], ["--", "/bin/true"], ["2:5000:3"], ["2:5000:3", "--"], ["2:5000:3", "--", "true"], ["2:5000:2", "--", "/bin/true"],
      ["2:5000:3", "2:5001:3", "--", "/bin/true"], ["2:5000", "--", "/bin/true"], ["x:5000:3", "--", "/bin/true"], ["2:-1:3", "--", "/bin/true"],
      ["2:05000:3", "--", "/bin/true"], ["2:4294967296:3", "--", "/bin/true"], ["2:5000:1024", "--", "/bin/true"],
      ["2:5000:3", "2:5001=/run/x", "--", "/bin/true"], ["2:5001=/run/x", "--", "/bin/true"], ["2:5001=/run/x", "2:5000:3"],
    ]) {
      expect(run(...args), args.join(" ")).toMatchObject({ status: 2, stderr: USAGE });
    }
  });
});

// The kernel's own vsock, CID 1 to itself, where this computer's has its loopback transport.
const LOOPBACK = spawnSync("python3", ["-c", [
  "import socket",
  "s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM); s.bind((socket.VMADDR_CID_ANY, socket.VMADDR_PORT_ANY)); s.listen()",
  "c = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM); c.connect((1, s.getsockname()[1]))",
].join("\n")]).status === 0;

describe.skipIf(!LOOPBACK)("on the kernel's vsock loopback", () => {
  it("connects to the port, puts the stream at its descriptor, and says so when nothing listens", () => {
    const listened = spawnSync("python3", ["-c", [
      "import socket, subprocess, sys",
      "s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM); s.bind((socket.VMADDR_CID_ANY, socket.VMADDR_PORT_ANY)); s.listen()",
      "p = subprocess.Popen([sys.argv[1], '1:%d:3' % s.getsockname()[1], '--', '/bin/sh', '-c', 'printf over-vsock >&3'])",
      "c, peer = s.accept(); print(c.recv(64).decode()); sys.exit(p.wait())",
    ].join("\n"), program], { encoding: "utf8", timeout: 10_000 });
    expect(listened).toMatchObject({ status: 0, stdout: "over-vsock\n" });
    // A port with no listener: the loopback resets the connection.
    expect(spawnSync(program, ["1:1:3", "--", "/bin/true"], { encoding: "utf8" })).toMatchObject({ status: 1, stderr: expect.stringMatching(/^surogate-vsock: cannot connect to 1:1: /) });
  });
});
