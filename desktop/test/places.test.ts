import { existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { Control, type ControlPlaces } from "../src/guest/control.js";
import { Places } from "../src/guest/places.js";
import type { FromAgent, Share } from "../src/guest/protocol.js";
import { readonlyFlag } from "../src/vm/linux.js";
import { virtiofsdArgs } from "../src/vm/qemu.js";

const KEY = "0123456789abcdef";
const R1: Share = { kind: "virtiofs", tag: "r1" };
const R2: Share = { kind: "virtiofs", tag: "r2" };

let dir: string;

beforeEach(() => {
  dir = realpathSync(mkdtempSync(join(tmpdir(), "places-")));
  for (const name of ["store", "Documents", "copy"]) mkdirSync(join(dir, name));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

describe("a folder's virtiofsd for the agent's own git", () => {
  it("maps the host user to the guest's root, and refuses every write where the folder is shared read-only", () => {
    expect(virtiofsdArgs("/home/ana/Documents", "/run/vm/vfs-2.sock", 0, { uid: 1000, gid: 1001 }, true)).toEqual([
      "--shared-dir=/home/ana/Documents", "--socket-path=/run/vm/vfs-2.sock", "--sandbox=namespace", "--cache=never",
      "--uid-map=:0:1000:1:", "--gid-map=:0:1001:1:", "--readonly",
    ]);
    expect(virtiofsdArgs("/d/history/k", "/run/vm/vfs-1.sock", 0, { uid: 1000, gid: 1001 })).not.toContain("--readonly");
  });

  it("is given --readonly only by a virtiofsd that has it", () => {
    // Ubuntu 26.04's 1.13.2 lists it; 24.04's 1.10.0 does not.
    expect(readonlyFlag("      --sandbox <SANDBOX>\n      --readonly\n          Prevent the guest from making modifications\n")).toBe(true);
    expect(readonlyFlag("      --sandbox <SANDBOX>\n      --seccomp <SECCOMP>\n      --no-readonly-thing\n")).toBe(false);
  });
});

describe("the agent's places", () => {
  const recorded = () => {
    const ran: string[][] = [];
    const places = new Places({
      folder: join(dir, "places"),
      mount: async (args) => void ran.push(["mount", ...args]),
      unmount: async (args) => void ran.push(["umount", ...args]),
    });
    return { ran, places };
  };

  it("mounts a folder's history for writing and the folder itself read-only, neither with programs to run", async () => {
    const { ran, places } = recorded();
    await places.mount(KEY, R1, R2);
    expect(ran).toEqual([
      ["mount", "-t", "virtiofs", "-o", "nosuid,nodev,noexec", "r1", join(dir, "places", KEY, "history")],
      ["mount", "-t", "virtiofs", "-o", "ro,nosuid,nodev,noexec", "r2", join(dir, "places", KEY, "real")],
    ]);
    // The agent's alone.
    expect(statSync(join(dir, "places")).mode & 0o777).toBe(0o700);
    expect(places.paths(KEY)).toEqual({ store: join(dir, "places", KEY, "history"), folder: join(dir, "places", KEY, "real") });
    // Asked again, as by a second thread on the folder, it is the one place.
    await places.mount(KEY, R1, R2);
    expect(ran).toHaveLength(2);
  });

  it("refuses a key that is not a folder's, a share that is not the host's, and a place asked for with other shares", async () => {
    const { ran, places } = recorded();
    for (const key of ["", "..", "0123456789ABCDEF", `${KEY}/x`, "0123456789abcde"]) {
      await expect(places.mount(key, R1, R2)).rejects.toThrow("not a folder's key");
    }
    await expect(places.mount(KEY, { kind: "virtiofs", tag: "r1 -o rw" }, R2)).rejects.toThrow("not a share tag");
    await expect(places.mount(KEY, R1, { kind: "virtiofs", tag: "-o" })).rejects.toThrow("not a share tag");
    // Nothing was made for any of them.
    expect(existsSync(join(dir, "places"))).toBe(false);
    await places.mount(KEY, R1, R2);
    await expect(places.mount(KEY, R2, R1)).rejects.toThrow("already mounted from other shares");
    await expect(places.mount(KEY, R1, { kind: "virtiofs", tag: "r3" })).rejects.toThrow("already mounted from other shares");
    expect(() => places.paths("fedcba9876543210")).toThrow("This folder's history is not in the sandbox");
    expect(ran).toHaveLength(2);
  });

  it("lets both mounts go, the folder's first, and mounts them again when asked", async () => {
    const { ran, places } = recorded();
    await places.mount(KEY, R1, R2);
    await places.unmount(KEY);
    expect(ran.slice(2)).toEqual([
      ["umount", "-l", join(dir, "places", KEY, "real")], ["umount", "-l", join(dir, "places", KEY, "history")],
    ]);
    expect(() => places.paths(KEY)).toThrow("This folder's history is not in the sandbox");
    expect(existsSync(join(dir, "places", KEY))).toBe(false);
    await places.unmount(KEY);
    expect(ran).toHaveLength(4);
    await places.mount(KEY, R1, R2);
    expect(ran).toHaveLength(6);
  });

  it("removes no file when it lets a place go: a mount that would not go keeps everything under it", async () => {
    const at = (name: string) => join(dir, "places", KEY, name);
    const places = new Places({
      folder: join(dir, "places"),
      // What the mounts would show: a file of the user's folder, and one of its history.
      mount: async (args) => writeFileSync(join(args.at(-1)!, "Report.docx"), "the real report\n"),
      unmount: async () => Promise.reject(new Error("umount: target is busy")),
    });
    await places.mount(KEY, R1, R2);
    await places.unmount(KEY);
    expect(readFileSync(join(at("real"), "Report.docx"), "utf8")).toBe("the real report\n");
    expect(readFileSync(join(at("history"), "Report.docx"), "utf8")).toBe("the real report\n");
    expect(() => places.paths(KEY)).toThrow("This folder's history is not in the sandbox");
  });

  it("lets a place go before it takes it again: an unmount still under way never takes the new mounts with it", async () => {
    const ran: string[] = [];
    const at = (name: string) => join(dir, "places", KEY, name);
    const places = new Places({
      folder: join(dir, "places"),
      mount: async (args) => void ran.push(`mount ${args.at(-2)} ${args.at(-1)}`),
      // umount -l, which takes whatever is mounted at its path by the time it runs, and takes its time.
      unmount: async (args) => {
        ran.push(`umount ${args.at(-1)}`);
        await new Promise((resolve) => setTimeout(resolve, 30));
      },
    });
    await places.mount(KEY, R1, R2);
    // The host lets the place go and, at once, asks for it again with the shares it has made anew.
    const gone = places.unmount(KEY);
    const again = places.mount(KEY, { kind: "virtiofs", tag: "r3" }, { kind: "virtiofs", tag: "r4" });
    await Promise.all([gone, again]);
    expect(ran.slice(2)).toEqual([`umount ${at("real")}`, `umount ${at("history")}`, `mount r3 ${at("history")}`, `mount r4 ${at("real")}`]);
    expect(places.paths(KEY)).toEqual({ store: at("history"), folder: at("real") });
  });

  it("mounts nothing of a place whose history could not be mounted, and takes the history's mount back when the folder's fails", async () => {
    const ran: string[][] = [];
    const failing = (at: string) => new Places({
      folder: join(dir, "places"), mountMs: 50,
      mount: async (args) => {
        ran.push(["mount", ...args]);
        if (args.at(-1)?.endsWith(at)) throw new Error("mount: wrong fs type");
      },
      unmount: async (args) => void ran.push(["umount", ...args]),
    });
    const history = failing("history");
    await expect(history.mount(KEY, R1, R2)).rejects.toThrow("wrong fs type");
    expect(ran.some((call) => call.at(-1)?.endsWith("real"))).toBe(false);
    expect(() => history.paths(KEY)).toThrow("This folder's history is not in the sandbox");
    ran.length = 0;
    const real = failing("real");
    await expect(real.mount(KEY, R1, R2)).rejects.toThrow("wrong fs type");
    expect(ran.at(-1)).toEqual(["umount", "-l", join(dir, "places", KEY, "history")]);
    expect(() => real.paths(KEY)).toThrow("This folder's history is not in the sandbox");
  });

  it("tries a mount again until the guest has found the share's device, and no longer than its bound", async () => {
    let tries = 0;
    const places = new Places({
      folder: join(dir, "places"), mountMs: 2_000,
      mount: async () => {
        tries += 1;
        if (tries < 3) throw new Error("mount: wrong fs type");
      },
      unmount: async () => {},
    });
    await places.mount(KEY, R1, R2);
    // The history's, at its third try, and the folder's at its first.
    expect(tries).toBe(4);
    const never = new Places({ folder: join(dir, "places"), mountMs: 60, mount: async () => Promise.reject(new Error("mount: wrong fs type")), unmount: async () => {} });
    const begun = performance.now();
    await expect(never.mount(KEY, R1, R2)).rejects.toThrow("wrong fs type");
    expect(performance.now() - begun).toBeLessThan(1_500);
  });
});

describe("the agent's control port, asked for a place", () => {
  const control = (places?: Partial<ControlPlaces>) => {
    const sent: FromAgent[] = [];
    const asked: unknown[] = [];
    const agent = new Control(
      (message) => sent.push(message),
      { uid: () => 10_000, setup: async () => {}, teardown: async () => {}, perform: async () => ({ ok: true }) },
      undefined,
      places && {
        mount: async (key, history, real) => void asked.push(["mount", key, history, real]),
        unmount: async (key) => void asked.push(["unmount", key]),
        ...places,
      },
    );
    const tell = async (message: unknown) => {
      agent.receive(JSON.stringify(message));
      await new Promise((resolve) => setTimeout(resolve, 0));
    };
    return { sent, asked, tell };
  };

  it("mounts and lets go a place, and says why one could not be mounted", async () => {
    const { sent, asked, tell } = control({});
    await tell({ type: "place", id: 1, key: KEY, history: R1, real: R2 });
    await tell({ type: "unplace", id: 2, key: KEY });
    expect(asked).toEqual([["mount", KEY, R1, R2], ["unmount", KEY]]);
    expect(sent).toEqual([{ type: "done", id: 1 }, { type: "done", id: 2 }]);
    const failing = control({ mount: async () => Promise.reject(new Error("mount: wrong fs type")) });
    await failing.tell({ type: "place", id: 3, key: KEY, history: R1, real: R2 });
    expect(failing.sent).toEqual([{ type: "failed", id: 3, message: "mount: wrong fs type" }]);
  });

  it("refuses a place request whose fields are not a place's", async () => {
    const { sent, asked, tell } = control({});
    await tell({ type: "place", id: 1, key: 7, history: R1, real: R2 });
    await tell({ type: "place", id: 2, key: KEY, history: { kind: "9p", tag: "r1" }, real: R2 });
    await tell({ type: "place", id: 3, key: KEY, history: R1 });
    await tell({ type: "unplace", id: 4 });
    expect(asked).toEqual([]);
    expect(sent).toEqual([
      { type: "failed", id: 1, message: "The agent cannot take this place request" },
      { type: "failed", id: 2, message: "The agent cannot take this place request" },
      { type: "failed", id: 3, message: "The agent cannot take this place request" },
      { type: "failed", id: 4, message: "The agent cannot take this unplace request" },
    ]);
  });

  it("answers that it keeps no folder's history where it was given no places", async () => {
    const { sent, tell } = control();
    await tell({ type: "place", id: 1, key: KEY, history: R1, real: R2 });
    await tell({ type: "unplace", id: 2, key: KEY });
    expect(sent).toEqual([
      { type: "failed", id: 1, message: "The agent keeps no folder's history" },
      { type: "failed", id: 2, message: "The agent keeps no folder's history" },
    ]);
  });
});
