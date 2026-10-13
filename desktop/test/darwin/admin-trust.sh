#!/usr/bin/env bash
# The company's certificate authority as an administrator gives it on a Mac, read through surogate-helper
# and taken by the app's rules (src/shell/ca-source-darwin.ts): three test roots put in the admin trust
# domain as `sudo security add-trusted-cert -d` puts one, the first trusted for SSL, the second for S/MIME
# alone, the third denied; then a site signed by the first, which Node reaches with what the app takes,
# and a browser too, by hand. Each root is taken out again at the end, whatever happened. Asks for an
# administrator's password once (sudo); on the macOS runner, sudo asks for none. With --user, the same three
# go in the user's own trust domain and login keychain, as a configuration profile the user installs puts a
# root there: macOS asks for the user's password in a window of its own, for each.
#
#   test/darwin/admin-trust.sh           the check; a pass ends "The admin trust domain is read as it should be"
#   test/darwin/admin-trust.sh --user    the user's trust domain; a pass ends "The user's trust domain is read as it should be"
#   test/darwin/admin-trust.sh --keep    either, then waits with the site up, for a browser's check by hand
set -euo pipefail

keep=""
user=""
for arg in "$@"; do
  case "$arg" in
    --keep) keep=--keep ;;
    --user) user=1 ;;
    *) echo "usage: test/darwin/admin-trust.sh [--user] [--keep]" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")/../.."
helper=native/helper/.build/surogate-helper
[ -x "$helper" ] || { echo "Build surogate-helper first: native/helper/build.sh" >&2; exit 1; }
[ -f dist/shell/ca-source-darwin.js ] || { echo "Build the app first: npm run build:darwin" >&2; exit 1; }
openssl=$(command -v openssl)

work=$(mktemp -d)
# No config of this Mac's: LibreSSL's own would add its v3_ca extensions beside those given here.
printf '[req]\ndistinguished_name = dn\n[dn]\n' > "$work/bare.cnf"
names=()
login="$HOME/Library/Keychains/login.keychain-db"
cleanup() {
  for name in ${names[@]+"${names[@]}"}; do
    if [ -n "$user" ]; then
      security remove-trusted-cert "$work/$name.pem" 2>/dev/null || true
      security delete-certificate -c "Surogate test $name $$" "$login" 2>/dev/null || true
    else
      sudo security remove-trusted-cert -d "$work/$name.pem" 2>/dev/null || true
      sudo security delete-certificate -c "Surogate test $name $$" /Library/Keychains/System.keychain 2>/dev/null || true
    fi
  done
  rm -rf "$work"
}
trap cleanup EXIT

root() {
  "$openssl" req -x509 -config "$work/bare.cnf" -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 2 -subj "/CN=Surogate test $1 $$" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -keyout "$work/$1.key" -out "$work/$1.pem" 2>/dev/null
  names+=("$1")
}
root ssl
root smime
root denied
"$openssl" req -new -config "$work/bare.cnf" -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=localhost" -keyout "$work/site.key" -out "$work/site.csr" 2>/dev/null
printf 'subjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=CA:FALSE\n' > "$work/site.ext"
"$openssl" x509 -req -in "$work/site.csr" -CA "$work/ssl.pem" -CAkey "$work/ssl.key" -CAcreateserial -days 2 -extfile "$work/site.ext" -out "$work/site.pem" 2>/dev/null

if [ -n "$user" ]; then
  domain="The user's trust domain"
  security add-trusted-cert -r trustRoot -p ssl -k "$login" "$work/ssl.pem"
  security add-trusted-cert -r trustRoot -p smime -k "$login" "$work/smime.pem"
  security add-trusted-cert -r deny -k "$login" "$work/denied.pem"
else
  domain="The admin trust domain"
  sudo security add-trusted-cert -d -r trustRoot -p ssl -k /Library/Keychains/System.keychain "$work/ssl.pem"
  sudo security add-trusted-cert -d -r trustRoot -p smime -k /Library/Keychains/System.keychain "$work/smime.pem"
  sudo security add-trusted-cert -d -r deny -k /Library/Keychains/System.keychain "$work/denied.pem"
fi

"$helper" trust > "$work/listed.pem"
node --input-type=module - "$work" "$keep" "$domain" <<'EOF'
import { X509Certificate } from "node:crypto";
import { readFileSync } from "node:fs";
import { createServer, get } from "node:https";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const [work, keep, domain] = process.argv.slice(2);
const { listedCertificates } = await import(pathToFileURL(join(process.cwd(), "dist", "shell", "ca-source-darwin.js")).href);
const fingerprint = (pem) => new X509Certificate(pem).fingerprint256;
const listed = listedCertificates(readFileSync(join(work, "listed.pem"), "utf8"));
const has = (name) => listed.some((pem) => fingerprint(pem) === fingerprint(readFileSync(join(work, `${name}.pem`), "utf8")));
const said = { ssl: has("ssl"), smime: has("smime"), denied: has("denied") };
console.log("Listed by the helper and taken:", JSON.stringify(said));
if (!said.ssl || said.smime || said.denied) throw new Error(`${domain} was not read as it should be: the SSL root alone is to be taken`);
const server = createServer({ key: readFileSync(join(work, "site.key")), cert: readFileSync(join(work, "site.pem")) }, (req, res) => res.end("trusted"));
await new Promise((done) => server.listen(0, "127.0.0.1", done));
const { port } = server.address();
const body = await new Promise((done, failed) => get({ host: "127.0.0.1", port, ca: listed, servername: "localhost" }, (res) => {
  let text = "";
  res.on("data", (part) => (text += part)).on("end", () => done(text));
}).on("error", failed));
console.log(`Node reached https://localhost:${port} with the roots the app takes: ${body}`);
if (keep === "--keep") {
  console.log(`Open https://localhost:${port} in Chrome, Edge or Safari: it opens with no warning. Press Ctrl+C to end, and the test roots go.`);
  await new Promise(() => {});
}
server.close();
console.log(`${domain} is read as it should be`);
EOF
