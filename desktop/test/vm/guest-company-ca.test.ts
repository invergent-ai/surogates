// The company's CA in the guest, under QEMU and KVM, booted by the VM manager: the image built by
// images/guest/build.sh, the agent disk built from this package (npm run build first). A boot told
// the CA trusts it in its system store and through the tools' own variables; the next boot told none
// trusts it no more. Every site is a server of the chat's own, on its loopback: no network.
// Behind SUROGATE_VM_TESTS=1; SUROGATE_VM_IMAGE names another image folder.

import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { VmManager, type VmOptions } from "../../src/vm/manager.js";
import { certificate, versionOne } from "../certificates.js";
import { agentDisk, folderOf, IMAGE, KVM, needsKvm, ROOT, signal, USER } from "./guest-support.js";

beforeAll(needsKvm);

const VARIABLES = ["NODE_EXTRA_CA_CERTS", "PIP_CERT", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO"];
// A site of the chat's own on its loopback, with a certificate the company's CA signed: a simple index
// with no package in it, so a pip that reaches it says it found no version.
const SERVE = [
  "node -e 'const fs = require(\"fs\");",
  "require(\"https\").createServer({ cert: fs.readFileSync(\"site.pem\"), key: fs.readFileSync(\"site.key\") },",
  "(q, r) => r.writeHead(200, { \"content-type\": \"text/html\" }).end(\"<html><body></body></html>\")).listen(8443, \"127.0.0.1\")' &",
  "for n in $(seq 100); do (exec 3<>/dev/tcp/127.0.0.1/8443) 2>/dev/null && break; sleep 0.05; done",
].join(" ");
// What each tool says of that site: curl, Node, Python and pip (uv's), each with the root's environment as it is.
const ASK = [
  "echo curl $(curl -s -o /dev/null -w '%{http_code}' https://localhost:8443/)",
  "echo node $(node -e 'require(\"https\").get(\"https://localhost:8443/\", (r) => console.log(r.statusCode)).on(\"error\", (e) => console.log(e.code))')",
  "echo python $(python3 -c 'import urllib.request\ntry: print(urllib.request.urlopen(\"https://localhost:8443/\").status)\nexcept Exception as e: print(type(e.reason).__name__)')",
  "echo pip $(pip install --no-cache-dir --index-url https://localhost:8443/simple/ cowsay 2>&1 | grep -o -m1 -e 'no versions of cowsay' -e 'invalid peer certificate')",
].join("; ");

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("the company's CA in the guest", { timeout: 300_000 }, () => {
  let dir: string;
  let folder: string;
  let options: VmOptions;
  let company: string;
  let v1root: string;
  const run = async (manager: VmManager, command: string) => {
    const outcome = await manager.perform({ id: `run-${Math.random()}`, root: ROOT, folder: folderOf(folder), kind: "run", args: { command, workdir: null, timeout: 90 } }, signal());
    return (outcome as { ok: { output: string } }).ok.output;
  };

  beforeAll(() => {
    dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-company-ca-")));
    folder = join(dir, "folder");
    mkdirSync(folder);
    const certs = join(dir, "certs");
    mkdirSync(certs);
    company = readFileSync(certificate(certs, "company"), "utf8");
    certificate(certs, "site", "company");
    certificate(certs, "it");
    certificate(certs, "itsite", "it");
    // A version-1 root beside it, which the app's Electron takes as a CA's and the guest's Node would not.
    v1root = readFileSync(versionOne(certs, "v1root", "v1site"), "utf8");
    for (const name of ["site.pem", "site.key", "itsite.pem", "v1site.pem"]) copyFileSync(join(certs, name), join(folder, name));
    options = {
      kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: agentDisk(dir), sessions: join(dir, "sessions.img"),
      run: mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-")), console: join(dir, "console.log"), user: USER, kvm: KVM,
    };
  });

  afterAll(() => {
    rmSync(options.run, { recursive: true, force: true });
    rmSync(dir, { recursive: true, force: true });
  });

  it("trusts the CA the app gives a boot, in the system's store and through each tool's own variable, and the next boot told none trusts it no more", async () => {
    const trusted = new VmManager({ ...options, ca: [company, v1root] });
    try {
      // OpenSSL's own defaults, the store's hashed folder alone, and its bundle alone; a CA the app was not given is trusted nowhere.
      expect(await run(trusted, "openssl verify site.pem; openssl verify -no-CAfile -no-CAstore -CApath /etc/ssl/certs site.pem; openssl verify -no-CApath -no-CAstore -CAfile /etc/ssl/certs/ca-certificates.crt site.pem; openssl verify v1site.pem; openssl verify itsite.pem 2>&1 | tail -1")).toBe(
        "site.pem: OK\nsite.pem: OK\nsite.pem: OK\nv1site.pem: OK\nerror itsite.pem: verification failed\n",
      );
      expect(await run(trusted, `printenv ${VARIABLES.join(" ")}`)).toBe(VARIABLES.map(() => "/etc/ssl/certs/ca-certificates.crt\n").join(""));
      // Certificates alone, which no command changes or runs.
      expect(await run(trusted, "awk '$5 == \"/etc/ssl/certs\" { print $6 }' /proc/self/mountinfo")).toMatch(/^ro,nosuid,nodev,noexec,\S+\n$/);
      expect(await run(trusted, `${SERVE}; ${ASK}; kill %1`)).toBe("curl 200\nnode 200\npython 200\npip no versions of cowsay\n");
    } finally {
      await trusted.stop();
    }
    const untrusted = new VmManager(options);
    try {
      expect(await run(untrusted, "openssl verify site.pem 2>&1 | tail -1; ls /etc/ssl/certs | grep -c surogate")).toBe("error site.pem: verification failed\n0\n");
      expect(await run(untrusted, `printenv ${VARIABLES.join(" ")}; echo $?`)).toBe("1\n");
      expect(await run(untrusted, `${SERVE}; ${ASK}; kill %1`)).toBe("curl 000\nnode UNABLE_TO_VERIFY_LEAF_SIGNATURE\npython SSLCertVerificationError\npip invalid peer certificate\n");
    } finally {
      await untrusted.stop();
    }
  });
});
