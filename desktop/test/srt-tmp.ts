// srt binds /tmp/claude into a sandbox only when it exists. It is made once for
// the whole run, if it is absent, so that no host starts while it comes or goes.
import { existsSync, mkdirSync, rmSync } from "node:fs";

export default function setup(): () => void {
  if (existsSync("/tmp/claude")) return () => {};
  mkdirSync("/tmp/claude");
  return () => rmSync("/tmp/claude", { recursive: true, force: true });
}
