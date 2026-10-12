// The company's certificate authority (spec, Section 9). A company that inspects TLS signs every
// site it carries with it, an internal server's too. The app trusts it beside the public roots in
// both of its TLS stacks, and keeps every other check of a certificate, its name's among them:
// - Node's, which the device link's WebSocket takes: as the main process's default CAs;
// - Chromium's, which the window and net.fetch take: as this OS has Chromium trust it.
// Both before the app is ready: Chromium does not see a certificate added once it has checked one.
// Where an administrator gives it, and Chromium's trust of it, are this OS's (ca-source.ts); what
// is taken of it, and Node's trust, are the same on every OS.

import { X509Certificate } from "node:crypto";
// Not named imports: Node 22, which the tests run on, has neither of the two the app's Node 24 has.
import * as tls from "node:tls";

// More than any company's certificate authorities take: hundreds of certificates.
export const MAX_BYTES = 1024 * 1024;

export const certificatesIn = (text: string): string[] => text.match(/-----BEGIN CERTIFICATE-----[\s\S]+?-----END CERTIFICATE-----/g) ?? [];

/**
 * The certificates *text*, read from *named*, holds, each a CA's. Text that holds a private key is
 * not taken: every user of this computer may read what an administrator gave.
 */
export function certificatesOf(text: string, named: string): string[] {
  if (/-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----/.test(text)) throw new Error(`${named} holds a private key`);
  const certificates = certificatesIn(text);
  if (certificates.length === 0) throw new Error(`${named} holds no certificate`);
  for (const pem of certificates) {
    let certificate: X509Certificate;
    try {
      certificate = new X509Certificate(pem);
    } catch {
      throw new Error(`${named} holds a certificate Surogate cannot read`);
    }
    if (!certificate.ca) throw new Error(`${named} holds a certificate that is not a certificate authority's: ${certificate.subject}`);
  }
  return certificates;
}

/** Node's TLS clients in this process trust *certificates* beside Node's own roots. */
export function trustInNode(certificates: string[]): void {
  if (certificates.length > 0) tls.setDefaultCACertificates([...tls.getCACertificates("default"), ...certificates]);
}

/** What the user is told when the company's CA is not trusted as its file says, once the app is ready. */
export interface Untrusted {
  message: string;
  detail: string;
}

/**
 * What a start trusts of the company's CA: the certificates the app's own connections trust, which
 * its guest's commands trust too (vm/manager.ts), and what the user is told, or null.
 */
export interface CompanyTrust {
  certificates: string[];
  untrusted: Untrusted | null;
}
