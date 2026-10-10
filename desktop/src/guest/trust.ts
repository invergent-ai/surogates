// The company's CA in the guest (spec, Section 9, --ca-cert, and Section 11, Network): the
// certificates the app's own connections trust at this start, which the host gives as data in
// hello's answer. For the boot, the guest's system trust store holds them beside the image's roots,
// laid out as update-ca-certificates leaves /etc/ssl/certs for a certificate of
// /usr/local/share/ca-certificates: a file of each, its hash link, and each at the bundle's end.
// The image is read-only, so the store is made on the guest's /run, and is each root's
// /etc/ssl/certs (enter-root); without a CA none is made, and each root's is the image's. It is
// made by one copy and one rehash: update-ca-certificates itself runs programs for each of the
// image's certificates, about 2 s of every boot. Nothing else trusts them: Java's keystore, which
// only its own tool rewrites, keeps the image's roots alone.

import { execFile } from "node:child_process";
import { X509Certificate } from "node:crypto";
import { appendFile, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { promisify } from "node:util";

// The guest's own trust store, the image's system store as Debian's ca-certificates keeps it, and the program that hashes it.
export const STORE = "/run/surogate/certs";
const SYSTEM = "/etc/ssl/certs";
const OPENSSL = "/usr/bin/openssl";
const BUNDLE = "ca-certificates.crt";
// Each company certificate's file, named as the app names its entries for the CA in a user's NSS database.
const fileOf = (certificate: X509Certificate) => `surogate-company-ca-${certificate.fingerprint256.replaceAll(":", "").slice(0, 16).toLowerCase()}.pem`;

const execute = promisify(execFile);

export interface TrustPlaces {
  system: string;
  store: string;
  openssl: string;
}

/**
 * The guest's trust store, at *places.store*, holding *certificates* beside *places.system*'s own:
 * true once it does, false where there is none to hold, and no store is made. Every certificate
 * must be a CA's, as the app's file must: one that is not, or not a certificate at all, and none is
 * taken, with why. A store that could not be finished goes, and is none. The guest's roots are set
 * up only once this has settled (Control), so none sees a store half made.
 */
export async function trust(certificates: readonly string[], places: TrustPlaces = { system: SYSTEM, store: STORE, openssl: OPENSSL }): Promise<boolean> {
  if (certificates.length === 0) return false;
  const parsed = certificates.map((pem) => {
    let certificate: X509Certificate;
    try {
      certificate = new X509Certificate(pem);
    } catch {
      throw new Error("the host gave a certificate it cannot read");
    }
    if (!certificate.ca) throw new Error(`the host gave a certificate that is not a certificate authority's: ${certificate.subject}`);
    return certificate;
  });
  const { store } = places;
  try {
    // The system's as it is, its links to the image's certificates among it, with its modes.
    await execute("/usr/bin/cp", ["-a", "--", places.system, store]);
    // Each as openssl writes it, so nothing of the host's text but the certificate is kept.
    for (const certificate of parsed) {
      await writeFile(join(store, fileOf(certificate)), certificate.toString(), { mode: 0o644 });
      await appendFile(join(store, BUNDLE), certificate.toString());
    }
    // Every certificate's hash link, those of the company's among them, as update-ca-certificates makes them.
    await execute(places.openssl, ["rehash", store], { env: { PATH: "/usr/bin:/bin" } });
  } catch (error) {
    await rm(store, { recursive: true, force: true });
    throw error;
  }
  return true;
}
