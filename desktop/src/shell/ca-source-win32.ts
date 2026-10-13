// The company's certificate authority on Windows: the roots an administrator or a policy gave this computer, as a
// company's Group Policy or Intune gives them, which surogate-helper.exe lists (native/windows/helper, trust.rs): the
// computer's own Root store, Group Policy's for the computer and for this user, and the enterprise's, each by its own
// physical store; never Microsoft's root program's, which any user's program can have Windows fill, nor the user's
// own Root store, which any program of the user's may add to; and of those, only each root Windows would trust for
// TLS, which none of the Untrusted Certificates stores holds. Chromium,
// the window's and net.fetch's, and the person's own Edge and Chrome trust them already, through Windows's own
// verifier: the app has no step of its own for Chromium. Node's TLS, the device link's and the updater's, trusts them
// as on Linux (company-ca.ts), and the guest is given them.

import { helperSync } from "../native-helper.js";
import { certificatesIn, certificatesOf, type CompanyTrust, MAX_BYTES, trustInNode } from "./company-ca.js";

const NAMED = "This computer's root certificate stores";
const NOT_TRUSTED = "Surogate could not trust your company's certificate authority";

/**
 * The CA certificates *listed*, the helper's answer, holds, by the rules a company's CA file is read by
 * (company-ca.ts): certificates only, no private key, a megabyte at most. Each is judged alone: one that is not a
 * certificate authority's, as a server's own certificate an administrator trusted, is left to Windows, which trusts it
 * as it was told, and is not taken.
 */
export function storeCertificates(listed: string): string[] {
  if (Buffer.byteLength(listed) > MAX_BYTES) throw new Error(`${NAMED} hold more than a megabyte of certificates`);
  if (/-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----/.test(listed)) throw new Error(`${NAMED} hold a private key`);
  return certificatesIn(listed).filter((pem) => {
    try {
      return certificatesOf(pem, NAMED).length === 1;
    } catch {
      return false;
    }
  });
}

/**
 * What a start trusts of the company's CA, *packaged*: the stores' roots, as *list* reads them, in Node's TLS; what
 * the user is told where they could not be read, or null. A development build takes none, as on Linux and macOS. A
 * list that cannot be read gives Node nothing: no start trusts what it could not read whole.
 */
export function companyTrust(packaged: boolean, _env: NodeJS.ProcessEnv, _home: string, list: () => string = () => helperSync(["trust"])): CompanyTrust {
  if (!packaged) return { certificates: [], untrusted: null };
  let certificates: string[];
  try {
    certificates = storeCertificates(list());
  } catch (error) {
    const said = error instanceof Error ? error.message : String(error);
    return { certificates: [], untrusted: { message: NOT_TRUSTED, detail: `${said}. Ask your administrator to check the root certificates this computer trusts.` } };
  }
  trustInNode(certificates);
  return { certificates, untrusted: null };
}
