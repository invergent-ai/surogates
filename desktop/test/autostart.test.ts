import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { autostartEntry, autostartFile, LAUNCHER, loginRefusal, MISREAD, REFUSED, setStartAtLogin, startsAtLogin } from "../src/shell/autostart.js";

let config: string;

beforeEach(() => {
  config = mkdtempSync(join(tmpdir(), "autostart-"));
});

afterEach(() => {
  rmSync(config, { recursive: true, force: true });
});

describe("start at login", () => {
  it("starts the installed app hidden, from its launcher", () => {
    expect(autostartEntry([LAUNCHER])).toBe(
      "[Desktop Entry]\nType=Application\nName=Surogate\nExec=/usr/local/bin/surogate --hidden\nTerminal=false\n",
    );
  });

  it("quotes each argument as the Desktop Entry specification reads it", () => {
    // A space, a quote and a percent sign in a development build's path.
    const exec = autostartEntry(["/home/f/my work/electron", '/home/f/a"b/100%/main.js']).split("\n")[3];
    expect(exec).toBe(String.raw`Exec="/home/f/my work/electron" "/home/f/a\\"b/100%%/main.js" --hidden`);
    expect(() => autostartEntry(["/home/f/a\nb"])).toThrow("A command Surogate starts at login cannot hold a control character");
  });

  it("refuses a program whose path holds a percent sign, which GNOME looks for unread and never finds", () => {
    expect(loginRefusal(["/home/f/100%/electron", "/home/f/main.js"])).toBe(REFUSED);
    expect(() => autostartEntry(["/home/f/100%/electron", "/home/f/main.js"])).toThrow(REFUSED);
    expect(loginRefusal([LAUNCHER])).toBeNull();
  });

  it("refuses a command any of whose words holds a $, a ` or a backslash, which systemd's autostart reader misreads", () => {
    for (const word of ["/home/f/a$b/main.js", "/home/f/a`b/main.js", "/home/f/a\\b/main.js"]) {
      expect(loginRefusal(["/home/f/electron", word])).toBe(MISREAD);
      expect(() => autostartEntry(["/home/f/electron", word])).toThrow(MISREAD);
    }
    expect(loginRefusal(["/home/f/a$b/electron", "/home/f/main.js"])).toBe(MISREAD);
  });

  it("is on while its entry is in the user's autostart folder, which it makes when it must", () => {
    const file = autostartFile(config);
    expect(file).toBe(join(config, "autostart", "surogate.desktop"));
    expect(startsAtLogin(file)).toBe(false);
    setStartAtLogin(file, true, [LAUNCHER]);
    expect(startsAtLogin(file)).toBe(true);
    expect(readFileSync(file, "utf8")).toContain("Exec=/usr/local/bin/surogate --hidden\n");
    setStartAtLogin(file, false, [LAUNCHER]);
    expect(existsSync(file)).toBe(false);
    // Off when it is off already.
    setStartAtLogin(file, false, [LAUNCHER]);
    expect(startsAtLogin(file)).toBe(false);
  });
});
