// The file helper's sandbox on Windows (src/hosts/srt-win32.ts): none, its walk the containment, and Windows's own
// folders, which no chat's folder may be or lie in.

import { describe, expect, it } from "vitest";

import { importedOn } from "./platforms.js";

const ENV = {
  SystemRoot: "C:\\Windows", ProgramFiles: "C:\\Program Files", "ProgramFiles(x86)": "C:\\Program Files (x86)", ProgramW6432: "C:\\Program Files",
  ProgramData: "C:\\ProgramData", LOCALAPPDATA: "C:\\Users\\Ana\\AppData\\Local",
};

async function windows() {
  const [{ srtOs }, { WIN32_SRT }] = await importedOn("win32", () => Promise.all([import("../src/hosts/srt-os.js"), import("../src/hosts/srt-win32.js")]));
  return { srtOs, WIN32_SRT };
}

describe("the file helper's sandbox on Windows", () => {
  it("wraps nothing: the helper's line is its own, it reads nothing of the system's through a sandbox, and leaves nothing of srt's", async () => {
    const { srtOs, WIN32_SRT } = await windows();
    expect(srtOs).toBe(WIN32_SRT);
    expect([srtOs.wraps, srtOs.system, srtOs.line("node helper.js"), srtOs.folderBytes, srtOs.socketsFit("C:\\x")]).toEqual([false, [], "node helper.js", null, true]);
    expect(srtOs.tempFolder("C:\\Users\\Ana\\AppData\\Local\\Surogate", "1-2")).toBe("C:\\Users\\Ana\\AppData\\Local\\Surogate\\hosts\\1-2");
    expect(srtOs.toolsMissing(["ripgrep"])).toBe("Surogate's tools are missing. Install Surogate again. It lacks ripgrep");
    expect(srtOs.fileToolsMissing(undefined, "Z:\\nowhere")).toEqual(["ripgrep"]);
    // Linux's and macOS's wrap theirs.
    const [{ srtOs: linux }, { srtOs: darwin }] = [await importedOn("linux", () => import("../src/hosts/srt-os.js")), await importedOn("darwin", () => import("../src/hosts/srt-os.js"))];
    expect([linux.wraps, darwin.wraps]).toEqual([true, true]);
  });

  it("looks for the app's own rg.exe beside the node the host runs on, first, then on the app's PATH, by Windows's separator", async () => {
    const { srtOs } = await windows();
    const node = "C:\\Program Files\\Surogate\\resources\\app\\bin\\node.exe";
    expect(srtOs.toolsPath("C:\\Windows\\System32;C:\\Tools", node)).toBe("C:\\Program Files\\Surogate\\resources\\app\\bin;C:\\Windows\\System32;C:\\Tools");
    expect(srtOs.toolsPath("", node)).toBe("C:\\Program Files\\Surogate\\resources\\app\\bin");
  });

  it("runs its hosts on the app's own node.exe, in the app's bin folder, where the app looks for the tools too", async () => {
    const [{ NODE: windowsNode }, { NODE: linuxNode }] = [await importedOn("win32", () => import("../src/hosts/tool-hosts.js")), await importedOn("linux", () => import("../src/hosts/tool-hosts.js"))];
    expect(windowsNode).toBe(`${linuxNode.slice(0, -"node".length)}node.exe`);
    expect(linuxNode).toMatch(/[\\/]bin[\\/]node$/);
  });

  it("keeps of the app's PATH each folder on a drive, by Windows's separator, and nothing in its stead where none is left", async () => {
    const { pathOutside } = await importedOn("win32", () => import("../src/hosts/policy.js"));
    expect(pathOutside("relative;\\\\server\\share;/usr/bin;;C:", [])).toBe("");
    expect(pathOutside(undefined, [])).toBe("");
  });

  it("refuses each of Windows's own folders, and what lies in them, in any case, and no other", async () => {
    const { srtOs } = await windows();
    const saved = Object.fromEntries(Object.keys(ENV).map((name) => [name, process.env[name]]));
    Object.assign(process.env, ENV);
    try {
      for (const path of [
        "C:\\Windows", "c:\\windows\\System32", "C:\\Program Files\\Surogate", "C:\\Program Files (x86)\\x", "C:\\ProgramData",
        "C:\\ProgramData\\Microsoft", "C:\\Users\\Ana\\AppData\\Local\\Surogate", "C:\\Users\\Ana\\AppData\\Local\\Surogate\\vm\\x",
      ]) expect(srtOs.reserved(path), path).toBe(true);
      for (const path of ["C:\\Windows2", "C:\\Users\\Ana\\Documents", "C:\\Users\\Ana\\AppData\\Local", "C:\\Program", "D:\\Windows", "C:\\"]) {
        expect(srtOs.reserved(path), path).toBe(false);
      }
    } finally {
      for (const [name, value] of Object.entries(saved)) {
        if (value === undefined) delete process.env[name];
        else process.env[name] = value;
      }
    }
  });
});
