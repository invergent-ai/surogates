import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";
import { duplexPair } from "node:stream";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CANCELLED, SANDBOX_STOPPED } from "../src/guest/command.js";
import { Control, type ControlPlaces, type ControlRoots } from "../src/guest/control.js";
import { Places } from "../src/guest/places.js";
import type { FromAgent, Share } from "../src/guest/protocol.js";
import { FOLDER_UNAVAILABLE } from "../src/hosts/messages.js";
import { readonlyFlag } from "../src/vm/linux.js";
import { type BootVm, type Place, VmManager, type VmOptions } from "../src/vm/manager.js";
import { virtiofsdArgs } from "../src/vm/qemu.js";

const KEY = "0123456789abcdef";
const R1: Share = { kind: "virtiofs", tag: "r1" };
const R2: Share = { kind: "virtiofs", tag: "r2" };
const signal = () => new AbortController().signal;

let dir: string;

beforeEach(() => {
  dir = realpathSync(mkdtempSync(join(tmpdir(), "places-")));
  for (const name of ["store", "Documents", "copy"]) mkdirSync(join(dir, name));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

const options = (): VmOptions => ({
  kernel: "/i/vmlinuz", rootfs: "/i/rootfs.img", agentDisk: "/a/agent.img", sessions: join(dir, "data", "sessions.img"),
  run: join(dir, "run"), console: join(dir, "logs", "console.log"), user: { uid: 1000, gid: 1000, name: "ana", home: "/home/ana" }, kvm: join(dir, "kvm"),
});
const place = (): Place => ({ key: KEY, history: join(dir, "store"), real: { path: join(dir, "Documents"), ...statSync(join(dir, "Documents")) } });

// A guest whose agent's Control is the real one, on *roots* and *places*, and whose backend says what it was asked.
const recording = (asked: unknown[], places: Partial<ControlPlaces> = {}, roots: Partial<ControlRoots> = {}): BootVm => async () => {
  const [host, guest] = duplexPair();
  const [net] = duplexPair();
  const [inbound] = duplexPair();
  let gone = (_said: string) => {};
  const exited = new Promise<string>((resolve) => {
    gone = resolve;
  });
  const kill = async () => {
    host.destroy();
    net.destroy();
    inbound.destroy();
    gone("");
  };
  const control = new Control(
    (message) => void guest.write(`${JSON.stringify(message)}\n`),
    { uid: () => 10_000, setup: async (root, folder, share) => void asked.push(["setup", root, folder, share]), teardown: async () => {}, perform: async () => ({ ok: true }), ...roots },
    { setClock: async () => {}, woke: () => {}, heard: () => {}, powerOff: kill },
    {
      mount: async (key, history, real) => void asked.push(["mount", key, history, real]),
      unmount: async (key) => void asked.push(["unmount", key]),
      ...places,
    },
  );
  createInterface({ input: guest }).on("line", (line) => control.receive(line));
  control.hello();
  let made = 0;
  return {
    control: host, net, inbound, exited, emulated: null, kill,
    share: async (folder, uid, _deadline, readonly) => {
      made += 1;
      asked.push(["share", folder, uid, readonly === true]);
      return { kind: "virtiofs", tag: `r${made}` };
    },
    unshare: async (share) => void asked.push(["unshare", share]),
  };
};

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
    // Named in another option's words, or as the start of another's name, it is not the option.
    expect(readonlyFlag("      --sandbox <SANDBOX>\n          Unlike --readonly, this\n      --readonly-cache <MODE>\n")).toBe(false);
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
    await expect(places.mount(KEY, { kind: "virtiofs", tag: "r3" }, R2)).rejects.toThrow("already mounted from other shares");
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

describe("the VM manager, asked for a folder's place", () => {
  it("shares the folder's history and the folder with the guest's root, the folder read-only, once for every thread on it", async () => {
    const asked: unknown[] = [];
    const manager = new VmManager(options(), recording(asked));
    expect(await manager.place(place(), signal())).toBeNull();
    expect(await manager.place(place(), signal())).toBeNull();
    expect(asked).toEqual([
      ["share", join(dir, "store"), 0, false], ["share", join(dir, "Documents"), 0, true], ["mount", KEY, R1, R2],
    ]);
    await manager.unplace(KEY);
    expect(asked.slice(3)).toEqual([["unmount", KEY], ["unshare", R2], ["unshare", R1]]);
    // A guest that holds no root and no place stops: the next place boots another, and is shared anew.
    expect(await manager.place(place(), signal())).toBeNull();
    expect(asked.slice(6)).toEqual([
      ["share", join(dir, "store"), 0, false], ["share", join(dir, "Documents"), 0, true], ["mount", KEY, R1, R2],
    ]);
    await manager.stop();
  });

  it("shares neither for a folder replaced since it was bound, nor a history that is a link to elsewhere", async () => {
    const asked: unknown[] = [];
    const manager = new VmManager(options(), recording(asked));
    const real = place().real;
    expect(await manager.place({ ...place(), real: { ...real, ino: real.ino + 1 } }, signal())).toEqual(FOLDER_UNAVAILABLE);
    symlinkSync(join(dir, "Documents"), join(dir, "linked"));
    const elsewhere = { error: { type: "unavailable", message: "This computer's sandbox could not add this folder's history: it is not where the app keeps it" } };
    expect(await manager.place({ ...place(), history: join(dir, "linked") }, signal())).toEqual(elsewhere);
    // Nor one reached through a link above it.
    symlinkSync(dir, join(dir, "via"));
    expect(await manager.place({ ...place(), history: join(dir, "via", "store") }, signal())).toEqual(elsewhere);
    // Nor one that is no folder, or is not there.
    writeFileSync(join(dir, "file"), "");
    expect(await manager.place({ ...place(), history: join(dir, "file") }, signal())).toEqual(elsewhere);
    expect(await manager.place({ ...place(), history: join(dir, "none") }, signal())).toEqual(elsewhere);
    // Nor a folder reached through a link, or one that has gone.
    expect(await manager.place({ ...place(), real: { ...real, path: join(dir, "linked") } }, signal())).toEqual(FOLDER_UNAVAILABLE);
    expect(await manager.place({ ...place(), real: { ...real, path: join(dir, "via", "Documents") } }, signal())).toEqual(FOLDER_UNAVAILABLE);
    expect(await manager.place({ ...place(), real: { ...real, path: join(dir, "none") } }, signal())).toEqual(FOLDER_UNAVAILABLE);
    expect(await manager.place({ ...place(), key: "../../etc" }, signal())).toEqual({
      error: { type: "unavailable", message: "This computer's sandbox could not add this folder's history: it has no key of a folder's" },
    });
    expect(asked).toEqual([]);
    await manager.stop();
  });

  it("takes back the history's share when the folder's could not be made, or the agent could not mount them, and asks again the next time", async () => {
    const asked: unknown[] = [];
    let fail = true;
    const manager = new VmManager(options(), recording(asked, {
      mount: async () => {
        if (fail) throw new Error("mount: wrong fs type");
      },
    }));
    expect(await manager.place(place(), signal())).toEqual({
      error: { type: "unavailable", message: "This computer's sandbox could not add this folder's history: mount: wrong fs type" },
    });
    expect(asked).toEqual([
      ["share", join(dir, "store"), 0, false], ["share", join(dir, "Documents"), 0, true], ["unshare", R2], ["unshare", R1],
    ]);
    fail = false;
    expect(await manager.place(place(), signal())).toBeNull();
    await manager.stop();
    // The folder's share alone not made: the history's, which was, is taken back.
    asked.length = 0;
    const boot = recording(asked);
    const refusing: BootVm = async (...args) => {
      const vm = await boot(...args);
      return { ...vm, share: (folder, uid, deadline, readonly) => (readonly ? Promise.reject(new Error("it holds 8 folders already")) : vm.share(folder, uid, deadline, readonly)) };
    };
    const other = new VmManager(options(), refusing);
    expect(await other.place(place(), signal())).toEqual({
      error: { type: "unavailable", message: "This computer's sandbox could not add this folder's history: it holds 8 folders already" },
    });
    expect(asked).toEqual([["share", join(dir, "store"), 0, false], ["unshare", R1]]);
    await other.stop();
  });

  it("takes a place again only once it has been let go, however soon it is asked for: as another, with shares of its own", async () => {
    const asked: unknown[] = [];
    // The agent's unmounts, each held until the test lets it go on.
    let letGo = () => {};
    let going: Promise<void>;
    const hold = () => {
      going = new Promise<void>((resolve) => {
        letGo = resolve;
      });
    };
    const unmounts = () => asked.filter((call) => (call as unknown[])[0] === "unmount");
    const manager = new VmManager(options(), recording(asked, {
      unmount: async (key) => {
        asked.push(["unmount", key]);
        await going;
      },
    }));
    expect(await manager.place(place(), signal())).toBeNull();
    hold();
    const gone = manager.unplace(KEY);
    // Asked for at once: not the place being let go, whose mounts and shares are on their way out.
    const again = manager.place(place(), signal());
    await vi.waitFor(() => expect(unmounts()).toHaveLength(1));
    // While that one's mounts go, nothing is shared for the one asked for.
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(asked.slice(3)).toEqual([["unmount", KEY]]);
    letGo();
    expect(await again).toBeNull();
    await gone;
    expect(asked.slice(3)).toEqual([
      ["unmount", KEY], ["unshare", R2], ["unshare", R1],
      ["share", join(dir, "store"), 0, false], ["share", join(dir, "Documents"), 0, true],
      ["mount", KEY, { kind: "virtiofs", tag: "r3" }, { kind: "virtiofs", tag: "r4" }],
    ]);
    // A cancel is answered at once, while it waits for a place on its way out too.
    hold();
    const leaving = manager.unplace(KEY);
    const cancel = new AbortController();
    const waiting = manager.place(place(), cancel.signal);
    await vi.waitFor(() => expect(unmounts()).toHaveLength(2));
    cancel.abort();
    expect(await waiting).toEqual(CANCELLED);
    letGo();
    await leaving;
    await manager.stop();
  });

  it("lets a place go that was asked for before its letting go, though it waited for one before that", async () => {
    const asked: unknown[] = [];
    let letGo = () => {};
    let going: Promise<void> | undefined;
    const manager = new VmManager(options(), recording(asked, {
      unmount: async (key) => {
        asked.push(["unmount", key]);
        await going;
      },
    }));
    expect(await manager.place(place(), signal())).toBeNull();
    going = new Promise<void>((resolve) => {
      letGo = resolve;
    });
    // Let go, asked for again, and let go again, before the first has gone.
    const first = manager.unplace(KEY);
    const again = manager.place(place(), signal());
    const last = manager.unplace(KEY);
    await vi.waitFor(() => expect(asked.slice(3)).toEqual([["unmount", KEY]]));
    going = undefined;
    letGo();
    expect(await again).toBeNull();
    await Promise.all([first, last]);
    // Each in the order it was asked: nothing of the place is left in the guest.
    const R3 = { kind: "virtiofs", tag: "r3" };
    const R4 = { kind: "virtiofs", tag: "r4" };
    expect(asked.slice(3)).toEqual([
      ["unmount", KEY], ["unshare", R2], ["unshare", R1],
      ["share", join(dir, "store"), 0, false], ["share", join(dir, "Documents"), 0, true], ["mount", KEY, R3, R4],
      ["unmount", KEY], ["unshare", R4], ["unshare", R3],
    ]);
    await manager.stop();
  });

  it("lets a place go that was asked for while the guest booted, once it is there", async () => {
    const asked: unknown[] = [];
    const boot = recording(asked);
    const manager = new VmManager(options(), async (...args) => {
      await new Promise((resolve) => setTimeout(resolve, 30));
      return boot(...args);
    });
    const placed = manager.place(place(), signal());
    const gone = manager.unplace(KEY);
    expect(await placed).toBeNull();
    await gone;
    expect(asked).toEqual([
      ["share", join(dir, "store"), 0, false], ["share", join(dir, "Documents"), 0, true], ["mount", KEY, R1, R2],
      ["unmount", KEY], ["unshare", R2], ["unshare", R1],
    ]);
    await manager.stop();
  });

  it("lets a place go once, however often it is asked to, and adds it anew only after the last has ended", async () => {
    const asked: unknown[] = [];
    let letGo = () => {};
    let going: Promise<void> | undefined;
    const manager = new VmManager(options(), recording(asked, {
      unmount: async (key) => {
        asked.push(["unmount", key]);
        await going;
      },
    }));
    expect(await manager.place(place(), signal())).toBeNull();
    going = new Promise<void>((resolve) => {
      letGo = resolve;
    });
    const first = manager.unplace(KEY);
    const second = manager.unplace(KEY);
    const again = manager.place(place(), signal());
    await vi.waitFor(() => expect(asked.slice(3)).toEqual([["unmount", KEY]]));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(asked.slice(3)).toEqual([["unmount", KEY]]);
    going = undefined;
    letGo();
    await Promise.all([first, second]);
    expect(await again).toBeNull();
    expect(asked.slice(3)).toEqual([
      ["unmount", KEY], ["unshare", R2], ["unshare", R1],
      ["share", join(dir, "store"), 0, false], ["share", join(dir, "Documents"), 0, true],
      ["mount", KEY, { kind: "virtiofs", tag: "r3" }, { kind: "virtiofs", tag: "r4" }],
    ]);
    await manager.stop();
  });

  it("answers a cancel at once while the guest adds the place", async () => {
    const asked: unknown[] = [];
    let mounted = () => {};
    const manager = new VmManager(options(), recording(asked, {
      mount: (key, history, real) => {
        asked.push(["mount", key, history, real]);
        return new Promise<void>((resolve) => {
          mounted = resolve;
        });
      },
    }));
    const cancel = new AbortController();
    const placing = manager.place(place(), cancel.signal);
    await vi.waitFor(() => expect(asked).toHaveLength(3));
    cancel.abort();
    expect(await placing).toEqual(CANCELLED);
    // The place it was adding is the next one's, once the agent has mounted it.
    const next = manager.place(place(), signal());
    mounted();
    expect(await next).toBeNull();
    expect(asked).toHaveLength(3);
    await manager.stop();
  });

  it("answers a key it holds for one folder as no other folder's place, and no other history's", async () => {
    const asked: unknown[] = [];
    const manager = new VmManager(options(), recording(asked));
    expect(await manager.place(place(), signal())).toBeNull();
    const held = { error: { type: "unavailable", message: "This computer's sandbox could not add this folder's history: its key is another folder's place in the sandbox" } };
    // Another folder under the key, another folder at the folder's path, and the folder with another history.
    const copy = { path: join(dir, "copy"), ...statSync(join(dir, "copy")) };
    expect(await manager.place({ ...place(), real: copy }, signal())).toEqual(held);
    expect(await manager.place({ ...place(), real: { ...place().real, ino: copy.ino } }, signal())).toEqual(held);
    expect(await manager.place({ ...place(), real: { ...place().real, path: join(dir, "copy") } }, signal())).toEqual(held);
    mkdirSync(join(dir, "other"));
    expect(await manager.place({ ...place(), history: join(dir, "other") }, signal())).toEqual(held);
    expect(asked).toHaveLength(3);
    // The place it holds is as it was, and once let go its key is the other's to take.
    expect(await manager.place(place(), signal())).toBeNull();
    await manager.unplace(KEY);
    expect(await manager.place({ ...place(), history: join(dir, "other") }, signal())).toBeNull();
    expect(asked.slice(6)).toEqual([["share", join(dir, "other"), 0, false], ["share", join(dir, "Documents"), 0, true], ["mount", KEY, R1, R2]]);
    await manager.stop();
  });

  it("shares no history kept in the folder, and no folder that holds its history or lies in it", async () => {
    const asked: unknown[] = [];
    const manager = new VmManager(options(), recording(asked));
    const apart = { error: { type: "unavailable", message: "This computer's sandbox could not add this folder's history: it is not kept apart from the folder" } };
    const folder = (path: string) => ({ path, ...statSync(path) });
    // The history in the folder: the agent's git would write the user's folder.
    mkdirSync(join(dir, "Documents", ".history"));
    expect(await manager.place({ ...place(), history: join(dir, "Documents", ".history") }, signal())).toEqual(apart);
    expect(await manager.place({ ...place(), history: join(dir, "Documents") }, signal())).toEqual(apart);
    // The folder in its history, or above it: the history's share would let the guest write a folder shared read-only.
    mkdirSync(join(dir, "store", "threads"));
    expect(await manager.place({ ...place(), real: folder(join(dir, "store", "threads")) }, signal())).toEqual(apart);
    expect(await manager.place({ ...place(), real: folder(dir) }, signal())).toEqual(apart);
    expect(asked).toEqual([]);
    // A folder whose name only begins as the history's does is apart from it.
    mkdirSync(join(dir, "store-2"));
    expect(await manager.place({ ...place(), real: folder(join(dir, "store-2")) }, signal())).toBeNull();
    await manager.stop();
  });

  it("sets a thread's root up on its copy, at its folder's path", async () => {
    const asked: unknown[] = [];
    const manager = new VmManager(options(), recording(asked));
    const copy = { path: join(dir, "copy"), ...statSync(join(dir, "copy")) };
    const outcome = await manager.perform({ id: "1", root: "root-1", folder: copy, at: join(dir, "Documents"), kind: "which", args: { name: "sh" } }, signal());
    expect(outcome).toEqual({ ok: true });
    // The share is the copy's; the path its commands see is the folder's.
    expect(asked).toEqual([["share", join(dir, "copy"), 10_000, false], ["setup", "root-1", join(dir, "Documents"), R1]]);
    await manager.stop();
  });

  it("keeps the guest that holds a place running once its last root has gone, and stops it with its last place", async () => {
    const asked: unknown[] = [];
    const boot = recording(asked);
    let boots = 0;
    const manager = new VmManager(options(), (...args) => {
      boots += 1;
      return boot(...args);
    });
    const copy = { path: join(dir, "copy"), ...statSync(join(dir, "copy")) };
    const work = () => manager.perform({ id: "1", root: "root-1", folder: copy, at: join(dir, "Documents"), kind: "which", args: { name: "sh" } }, signal());
    expect(await manager.place(place(), signal())).toBeNull();
    expect(await work()).toEqual({ ok: true });
    await manager.teardown("root-1");
    // Between a thread's snapshot and its next command the guest holds the place alone.
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(await manager.place(place(), signal())).toBeNull();
    expect(await work()).toEqual({ ok: true });
    expect(boots).toBe(1);
    expect(asked.filter((call) => (call as unknown[])[0] === "mount")).toHaveLength(1);
    await manager.teardown("root-1");
    await manager.unplace(KEY);
    expect(await work()).toEqual({ ok: true });
    expect(boots).toBe(2);
    await manager.stop();
  });

  it("stops a guest whose agent does not answer a place, asks nothing more of it, and boots another for the next", async () => {
    const asked: unknown[] = [];
    let stuck = true;
    const manager = new VmManager({ ...options(), setupMs: 100 }, recording(asked, {
      mount: (key, history, real) => {
        asked.push(["mount", key, history, real]);
        return stuck ? new Promise<void>(() => {}) : Promise.resolve();
      },
    }));
    expect(await manager.place(place(), signal())).toEqual(SANDBOX_STOPPED);
    expect(asked).toEqual([["share", join(dir, "store"), 0, false], ["share", join(dir, "Documents"), 0, true], ["mount", KEY, R1, R2]]);
    stuck = false;
    expect(await manager.place(place(), signal())).toBeNull();
    expect(asked.slice(3)).toEqual([["share", join(dir, "store"), 0, false], ["share", join(dir, "Documents"), 0, true], ["mount", KEY, R1, R2]]);
    await manager.stop();
  });

  it("writes nothing into the folder or its history, and removes nothing of either when the place goes", async () => {
    writeFileSync(join(dir, "Documents", "Report.docx"), "the real report\n");
    writeFileSync(join(dir, "store", "HEAD"), "ref: refs/heads/main\n");
    const manager = new VmManager(options(), recording([]));
    expect(await manager.place(place(), signal())).toBeNull();
    await manager.unplace(KEY);
    await manager.stop();
    expect(readdirSync(join(dir, "Documents"))).toEqual(["Report.docx"]);
    expect(readFileSync(join(dir, "Documents", "Report.docx"), "utf8")).toBe("the real report\n");
    expect(readdirSync(join(dir, "store"))).toEqual(["HEAD"]);
  });

  it("lets go of nothing for a place it does not hold, and answers a place asked of a manager that is stopping", async () => {
    const asked: unknown[] = [];
    const manager = new VmManager(options(), recording(asked));
    await manager.unplace(KEY);
    expect(await manager.place(place(), signal())).toBeNull();
    await manager.unplace("fedcba9876543210");
    expect(asked).toHaveLength(3);
    await manager.stop();
    expect(await manager.place(place(), signal())).toEqual({ error: { type: "unavailable", message: "This computer's sandbox is stopping" } });
  });
});
