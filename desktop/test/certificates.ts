// Certificates of a test's own, made by this computer's openssl: a company's certificate authority,
// and the certificates it signs, as a company's network signs every site it inspects.

import { spawnSync } from "node:child_process";
import { join } from "node:path";

/**
 * Make <name>.pem and its key <name>.key in *dir*: a certificate authority's, or, signed by the
 * authority <ca>.pem, a site's for *names* (this computer's localhost and 127.0.0.1 by default). Its path.
 */
export function certificate(dir: string, name: string, ca?: string, names = "DNS:localhost,IP:127.0.0.1"): string {
  const signed = ca
    ? ["-CA", join(dir, `${ca}.pem`), "-CAkey", join(dir, `${ca}.key`), "-subj", `/CN=${name}`, "-addext", `subjectAltName=${names}`, "-addext", "basicConstraints=CA:FALSE"]
    : ["-subj", `/CN=${name}`, "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign"];
  const made = spawnSync("openssl", ["req", "-x509", ...signed, "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
    "-keyout", join(dir, `${name}.key`), "-out", join(dir, `${name}.pem`), "-days", "2"], { encoding: "utf8" });
  if (made.status !== 0) throw new Error(`openssl could not make ${name}: ${made.stderr}`);
  return join(dir, `${name}.pem`);
}

/** The certificate at *pem* as openssl writes it again, which is how the install script keeps it. */
export const rewritten = (pem: string): string => spawnSync("openssl", ["x509", "-in", pem], { encoding: "utf8" }).stdout;
