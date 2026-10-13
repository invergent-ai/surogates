// The company's certificate authority on macOS: the certificates trusted as roots for SSL for this user, as
// Safari trusts them: those an administrator trusted for every user of this Mac, as an MDM profile or `sudo
// security add-trusted-cert -d` trusts one, in macOS's admin trust domain, and those of a configuration profile
// the user installed, in the user's own; none either domain denies. surogate-helper lists them (native/helper,
// Trust.swift). Chromium, the window's and net.fetch's, and the user's own Chrome trust them already, through
// macOS's own verifier: the app has no step of its own for Chromium. Node's TLS, the device link's and the
// updater's, trusts them as on Linux (company-ca.ts), and the guest is given them.

import { helperSync } from "../mac-helper.js";
import { certificatesIn, certificatesOf, type CompanyTrust, MAX_BYTES, trustInNode } from "./company-ca.js";

const NAMED = "This Mac's trust settings";
const NOT_TRUSTED = "Surogate could not trust your company's certificate authority";

/**
 * The CA certificates *listed*, the helper's answer, holds, by the rules a company's CA file is read
 * by (company-ca.ts): certificates only, no private key, a megabyte at most. Each is judged alone: one
 * that is not a certificate authority's, as a server's own certificate an administrator trusted, is left
 * to macOS, which trusts it as it was told, and is not taken.
 */
export function listedCertificates(listed: string): string[] {
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
 * What a start trusts of the company's CA, *packaged*: the roots for SSL of the user's and the admin trust domains, as
 * *list* reads them, in Node's TLS; what the user is told where they could not be read, or null. A
 * development build takes none, as it never takes an installed app's CA on Linux. A list that cannot be
 * read gives Node nothing: no start trusts what it could not read whole.
 */
export function companyTrust(packaged: boolean, _env: NodeJS.ProcessEnv, _home: string, list: () => string = () => helperSync(["trust"])): CompanyTrust {
  if (!packaged) return { certificates: [], untrusted: null };
  let certificates: string[];
  try {
    certificates = listedCertificates(list());
  } catch (error) {
    const said = error instanceof Error ? error.message : String(error);
    return { certificates: [], untrusted: { message: NOT_TRUSTED, detail: `${said}. Ask your administrator to check the certificate authority this Mac trusts.` } };
  }
  trustInNode(certificates);
  return { certificates, untrusted: null };
}
