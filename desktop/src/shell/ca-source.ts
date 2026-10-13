// What a start of the app trusts of the company's certificate authority, by this OS: where an
// administrator gives it, and Chromium's own trust of it (ca-source-linux.ts, ca-source-darwin.ts).
// Electron-free.

import { forThisOs, notBuilt } from "../platform.js";
import * as darwin from "./ca-source-darwin.js";
import * as linux from "./ca-source-linux.js";
import type { CompanyTrust } from "./company-ca.js";

/** What a start trusts of the company's CA, *packaged* or a development build's, for the user whose environment is *env* and whose home is *home*. */
export const companyTrust: (packaged: boolean, env: NodeJS.ProcessEnv, home: string) => CompanyTrust = forThisOs<(packaged: boolean, env: NodeJS.ProcessEnv, home: string) => CompanyTrust>({
  linux: linux.companyTrust,
  darwin: (packaged, env, home) => darwin.companyTrust(packaged, env, home),
}) ?? (() => notBuilt("Surogate Desktop's trust of a company's certificate authority"));
