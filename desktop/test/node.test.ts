// The app's own node (spec, Section 11), as the build leaves it at bin/node, and the script that fetches it.

import { execFileSync, spawn } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, it } from "vitest";

const PACKAGE = fileURLToPath(new URL("..", import.meta.url));

it("is the pinned Node for linux-x64, of the major this package's engines start at, with nothing beside it", () => {
  const pinned = /^VERSION=(v[\d.]+)$/m.exec(readFileSync(join(PACKAGE, "scripts", "node.sh"), "utf8"))?.[1];
  const { engines } = JSON.parse(readFileSync(join(PACKAGE, "package.json"), "utf8")) as { engines: { node: string } };
  const floor = /^>=(\d+)\./.exec(engines.node)?.[1];
  expect(execFileSync(join(PACKAGE, "bin", "node"), ["-p", "`${process.version} ${process.platform}-${process.arch}`"], { encoding: "utf8" }))
    .toBe(`${pinned} linux-x64\n`);
  expect(pinned?.split(".")[0]).toBe(`v${floor}`);
  // No npm, no headers, no corepack: the app ships the one program.
  expect(readdirSync(join(PACKAGE, "bin"))).toEqual(["node"]);
});

it("fetches it with none of the user's curl configuration: a curlrc's headers never go out", async () => {
  // The script in a package of its own, with no bin/node, so it fetches. Its connection goes to a
  // proxy of the test's, which hears the request and refuses it, so nothing is downloaded.
  const dir = mkdtempSync(join(tmpdir(), "node-sh-"));
  try {
    mkdirSync(join(dir, "scripts"));
    copyFileSync(join(PACKAGE, "scripts", "node.sh"), join(dir, "scripts", "node.sh"));
    mkdirSync(join(dir, "home"));
    writeFileSync(join(dir, "home", ".curlrc"), 'proxy-header = "X-From-Curlrc: proxy"\nheader = "Authorization: Bearer from-the-users-curlrc"\n');
    let heard = "";
    const proxy = createServer((socket) => socket.once("data", (head) => {
      heard = head.toString("latin1");
      socket.end("HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n");
    }));
    await new Promise<void>((resolve) => proxy.listen(0, "127.0.0.1", resolve));
    const { port } = proxy.address() as AddressInfo;
    const code = await new Promise<number | null>((resolve) => {
      spawn("bash", [join(dir, "scripts", "node.sh")], {
        env: { PATH: process.env.PATH ?? "/usr/bin:/bin", HOME: join(dir, "home"), https_proxy: `http://127.0.0.1:${port}` },
        stdio: "ignore",
      }).on("exit", resolve);
    });
    proxy.close();
    expect(heard.split("\r\n")[0]).toBe("CONNECT nodejs.org:443 HTTP/1.1");
    expect(heard).not.toContain("X-From-Curlrc");
    expect([code === 0, existsSync(join(dir, "bin"))]).toEqual([false, false]);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

it("refuses on Windows, fetching nothing, and names the Windows app's build, which makes the node it runs on", async () => {
  // The script in a package of its own, with no bin/node, on a PATH of every program of this computer's but Git for
  // Windows' own uname, and a curl that says it was run.
  const dir = mkdtempSync(join(tmpdir(), "node-sh-win32-"));
  try {
    mkdirSync(join(dir, "scripts"));
    copyFileSync(join(PACKAGE, "scripts", "node.sh"), join(dir, "scripts", "node.sh"));
    const bin = join(dir, "path");
    mkdirSync(bin);
    for (const name of readdirSync("/usr/bin")) if (name !== "uname" && name !== "curl") symlinkSync(join("/usr/bin", name), join(bin, name));
    writeFileSync(join(bin, "uname"), "#!/bin/sh\necho MINGW64_NT-10.0-26100\n", { mode: 0o755 });
    writeFileSync(join(bin, "curl"), `#!/bin/sh\ntouch "${join(dir, "fetched")}"\nexit 1\n`, { mode: 0o755 });
    let said = "";
    const code = await new Promise<number | null>((resolve) => {
      const child = spawn(join(bin, "bash"), [join(dir, "scripts", "node.sh")], { env: { PATH: bin, HOME: dir }, stdio: ["ignore", "ignore", "pipe"] });
      child.stderr.on("data", (chunk: Buffer) => (said += chunk.toString()));
      child.on("exit", resolve);
    });
    expect(said).toBe("npm run build is the Linux app's build, with Linux's node: on Windows, build the app with npm run build:win32\n");
    expect([code, existsSync(join(dir, "fetched")), existsSync(join(dir, "bin"))]).toEqual([1, false, false]);
    // A development build for Windows makes the node its hosts run on, bin/node.exe, itself: no npm script fetches one.
    // The Windows app's layout ships the pinned one (scripts/package-win.sh, scripts/node.sh win32-x64).
    const { scripts } = JSON.parse(readFileSync(join(PACKAGE, "package.json"), "utf8")) as { scripts: Record<string, string> };
    expect(scripts.build!.split(" && ")[0]).toBe("scripts/node.sh");
    expect(scripts["build:win32"]!.split(" && ")).not.toContain("scripts/node.sh");
    for (const [name, script] of Object.entries(scripts)) if (name !== "build:win32") expect(script, name).not.toContain("node.exe");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
