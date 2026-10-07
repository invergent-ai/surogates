import { expect, it } from "vitest";

import * as protocol from "../src/guest/protocol.js";

it("no longer carries the guest-bind machinery", () => {
  expect("MAX_PROTECTED" in protocol).toBe(false);
});
