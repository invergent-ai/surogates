import { expect, it } from "vitest";

import * as protocol from "../src/guest/protocol.js";
import * as restarts from "../src/hosts/restarts.js";

it("no longer carries the guest-bind machinery", () => {
  expect("guestBinds" in restarts).toBe(false);
  expect("MAX_PROTECTED" in protocol).toBe(false);
});
