import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// @testing-library/react auto-cleanup only registers when vitest globals are
// enabled; this config does not use globals, so unmount after each test
// explicitly to keep the DOM isolated between tests.
afterEach(cleanup);
