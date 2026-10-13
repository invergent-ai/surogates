// The app's own native helper, per OS (native-helper.ts): where Windows's surogate-helper.exe is, as the installed
// app lays it out and as a development build makes it. macOS's is pinned by mac-helper.test.ts.

import { expect, it } from "vitest";

import { importedOn } from "./platforms.js";

it("is in the installed app's bin folder on Windows, and its build's own in a development build", async () => {
  const { helperAt } = await importedOn("win32", () => import("../src/native-helper.js"));
  expect(helperAt("C:\\Program Files\\Surogate\\resources\\app\\dist")).toBe("C:\\Program Files\\Surogate\\bin\\surogate-helper.exe");
  expect(helperAt("D:\\Apps\\Surogate\\resources\\app\\dist")).toBe("D:\\Apps\\Surogate\\bin\\surogate-helper.exe");
  expect(helperAt("C:\\Users\\ana\\surogates\\desktop\\dist")).toBe("C:\\Users\\ana\\surogates\\desktop\\native\\windows\\target\\release\\surogate-helper.exe");
  // Only the app's own code in an install: no other folder of one.
  expect(helperAt("C:\\Program Files\\Surogate\\resources\\other\\dist")).toBe("C:\\Program Files\\Surogate\\resources\\other\\native\\windows\\target\\release\\surogate-helper.exe");
});
