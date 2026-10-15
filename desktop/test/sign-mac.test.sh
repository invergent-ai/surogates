#!/usr/bin/env bash
# scripts/sign-mac.sh on a Mac, against a small bundle of Surogate.app's shape that the test makes, with two identities
# of the test's own, each self-signed in a keychain of its own: what it signs and how, what its checks take and what
# each refuses, the update payload and the disk image, and that what is the release's alone (notarize, assess, import)
# takes nothing that is not Apple's. Run on a Mac with Xcode, from anywhere:
#   desktop/test/sign-mac.test.sh
set -euo pipefail

SIGN="$(cd "$(dirname "$0")/.." && pwd)/scripts/sign-mac.sh"
work="$(mktemp -d)"
work="$(cd "$work" && pwd -P)"
SIGNER="Surogate test signer"
OTHER="Surogate other signer"
cleanup() {
  for keychain in "$work/test.keychain-db" "$work/other.keychain-db"; do "$SIGN" forget "$keychain" 2>/dev/null || :; done
  rm -rf "$work"
}
trap cleanup EXIT
failed=0

ok() { echo "ok - $1"; }
not_ok() {
  echo "not ok - $1"
  failed=$((failed + 1))
}
# Whether "$@" after the description ends 0.
takes() {
  local what="$1" said
  shift
  if said="$("$@" 2>&1)"; then ok "$what"; else not_ok "$what: $said"; fi
}
# Whether "$@" after the description and the words ends other than 0, saying those words.
refuses() {
  local what="$1" words="$2" said
  shift 2
  if said="$("$@" 2>&1)"; then not_ok "$what: it was taken"; elif [[ "$said" == *"$words"* ]]; then ok "$what"; else not_ok "$what: it said $said"; fi
}

# A program, a library and a loadable bundle, each Apple silicon's.
program() { printf 'int main(void) { return 0; }\n' | xcrun clang -arch arm64 -mmacosx-version-min=14.0 -x c -o "$1" -; }
library() { printf 'int surogate_test(void) { return 1; }\n' | xcrun clang -arch arm64 -mmacosx-version-min=14.0 -dynamiclib -x c -o "$1" -; }
loadable() { printf 'int surogate_test(void) { return 1; }\n' | xcrun clang -arch arm64 -mmacosx-version-min=14.0 -bundle -x c -o "$1" -; }

# An Info.plist at $1 of a bundle of identifier $2, program $3 and package type $4.
info_plist() {
  printf '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0">\n<dict>\n\t<key>CFBundleExecutable</key>\n\t<string>%s</string>\n\t<key>CFBundleIdentifier</key>\n\t<string>%s</string>\n\t<key>CFBundlePackageType</key>\n\t<string>%s</string>\n</dict>\n</plist>\n' "$3" "$2" "$4" >"$1"
}

# A bundle of Surogate.app's shape at $1/Surogate.app: its program and the two beside it, a framework with a library of
# its own, a helper app, and the app's node, a tool and a loadable library in Resources.
fixture() {
  local app="$1/Surogate.app"
  mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources/app/bin" "$app/Contents/Resources/app/dist/native" \
    "$app/Contents/Frameworks/Tiny.framework/Versions/A/Resources" "$app/Contents/Frameworks/Tiny.framework/Versions/A/Libraries" \
    "$app/Contents/Frameworks/Surogate Helper.app/Contents/MacOS"
  info_plist "$app/Contents/Info.plist" ai.invergent.surogate Surogate APPL
  program "$app/Contents/MacOS/Surogate"
  program "$app/Contents/MacOS/surogate-vm"
  program "$app/Contents/MacOS/surogate-helper"
  program "$app/Contents/Resources/app/bin/node"
  program "$app/Contents/Resources/app/bin/rg"
  loadable "$app/Contents/Resources/app/dist/native/fsat.node"
  printf '{}\n' >"$app/Contents/Resources/app/package.json"
  local framework="$app/Contents/Frameworks/Tiny.framework"
  library "$framework/Versions/A/Tiny"
  library "$framework/Versions/A/Libraries/libtiny.dylib"
  info_plist "$framework/Versions/A/Resources/Info.plist" ai.invergent.surogate.test.tiny Tiny FMWK
  ln -s A "$framework/Versions/Current"
  ln -s Versions/Current/Tiny "$framework/Tiny"
  ln -s Versions/Current/Resources "$framework/Resources"
  ln -s Versions/Current/Libraries "$framework/Libraries"
  local helper="$app/Contents/Frameworks/Surogate Helper.app"
  program "$helper/Contents/MacOS/Surogate Helper"
  info_plist "$helper/Contents/Info.plist" ai.invergent.surogate.helper "Surogate Helper" APPL
}

# A copy of bundle $1 at $2/Surogate.app, its signatures kept.
copy() {
  mkdir -p "$2"
  ditto "$1" "$2/Surogate.app"
}

identity="$("$SIGN" trial "$work/test.keychain-db" "$SIGNER")"
other="$("$SIGN" trial "$work/other.keychain-db" "$OTHER")"
[[ "$identity" =~ ^[0-9A-F]{40}$ ]] && [[ "$other" =~ ^[0-9A-F]{40}$ ]] && ok "a trial makes an identity of its own and says its SHA-1" || not_ok "a trial says no SHA-1: $identity $other"
refuses "a trial takes no keychain that is there already" "is there already" "$SIGN" trial "$work/test.keychain-db" "$SIGNER"
refuses "a trial takes no keychain path but a whole one of a .keychain-db" "ends in .keychain-db" "$SIGN" trial "$work/test.keychain" "$SIGNER"

fixture "$work/signed"
signed="$work/signed/Surogate.app"
takes "signs every code object of the bundle with the test's identity" "$SIGN" sign "$signed" "$identity" "$work/test.keychain-db"
takes "checks it as signed by the test's identity alone, each with its entitlements, the hardened runtime and a secure timestamp" \
  "$SIGN" verify "$signed" trial "$SIGNER"
for file in Contents/MacOS/Surogate Contents/MacOS/surogate-vm "Contents/Frameworks/Tiny.framework/Versions/A/Libraries/libtiny.dylib" Contents/Resources/app/dist/native/fsat.node; do
  info="$(codesign -dvvv "$signed/$file" 2>&1)"
  if grep -Eq 'flags=0x[0-9a-f]+\(runtime\)' <<<"$info" && grep -q '^Timestamp=' <<<"$info"; then
    ok "signs $file with the hardened runtime and a secure timestamp"
  else
    not_ok "signs $file with the hardened runtime and a secure timestamp: $info"
  fi
done
[ "$(codesign -d --entitlements - --xml "$signed/Contents/MacOS/surogate-vm" 2>/dev/null | grep -o '<key>[^<]*</key>')" = "<key>com.apple.security.virtualization</key>" ] \
  && ok "gives surogate-vm the Virtualization framework's entitlement alone" || not_ok "gives surogate-vm other entitlements"
[ -z "$(codesign -d --entitlements - --xml "$signed/Contents/MacOS/surogate-helper" 2>/dev/null)" ] \
  && ok "gives surogate-helper no entitlement" || not_ok "gives surogate-helper an entitlement"
# surogate-helper reads its own Team ID to check an update's bundle by: it is the app's own signer's, Team ID and all.
signers() { codesign -dvvv "$1" 2>&1 | grep -E '^(Authority|TeamIdentifier|Signature)=' || :; }
[ -n "$(signers "$signed/Contents/MacOS/Surogate")" ] && [ "$(signers "$signed/Contents/MacOS/surogate-helper")" = "$(signers "$signed/Contents/MacOS/Surogate")" ] \
  && ok "signs surogate-helper by the app's own signer, its Team ID the app's" || not_ok "signs surogate-helper by another signer than the app's: $(signers "$signed/Contents/MacOS/surogate-helper")"
refuses "takes no other signer's bundle for the test's" "is not signed by $OTHER alone" "$SIGN" verify "$signed" trial "$OTHER"
refuses "takes a trial's bundle for no ad hoc one" "is not signed ad hoc" "$SIGN" verify "$signed" adhoc
refuses "takes a trial's bundle for no Developer ID's" "Developer ID Application certificate" "$SIGN" verify "$signed" developer-id ABCDE12345
refuses "is told a signer it knows" "usage:" "$SIGN" verify "$signed" someone
refuses "is told a Team ID of ten capitals and digits" "usage:" "$SIGN" verify "$signed" developer-id abcde

copy "$signed" "$work/other-signed"
codesign --force --sign "$other" --keychain "$work/other.keychain-db" --options runtime --timestamp "$work/other-signed/Surogate.app/Contents/Resources/app/bin/rg"
refuses "refuses a bundle one of whose programs another signed" "Contents/Resources/app/bin/rg is not signed by $SIGNER alone" "$SIGN" verify "$work/other-signed/Surogate.app" trial "$SIGNER"

copy "$signed" "$work/other-helper"
codesign --force --sign "$other" --keychain "$work/other.keychain-db" --options runtime --timestamp "$work/other-helper/Surogate.app/Contents/MacOS/surogate-helper"
refuses "refuses a bundle whose surogate-helper another signed, which would check updates by another Team ID" "Contents/MacOS/surogate-helper is not signed by $SIGNER alone" \
  "$SIGN" verify "$work/other-helper/Surogate.app" trial "$SIGNER"

copy "$signed" "$work/no-runtime"
codesign --force --sign "$identity" --keychain "$work/test.keychain-db" --timestamp "$work/no-runtime/Surogate.app/Contents/MacOS/surogate-helper"
refuses "refuses a program signed without the hardened runtime" "Contents/MacOS/surogate-helper is not signed with the hardened runtime" \
  "$SIGN" verify "$work/no-runtime/Surogate.app" trial "$SIGNER"

copy "$signed" "$work/no-timestamp"
codesign --force --sign "$identity" --keychain "$work/test.keychain-db" --options runtime --timestamp=none "$work/no-timestamp/Surogate.app/Contents/MacOS/surogate-helper"
refuses "refuses a program signed without a secure timestamp" "Contents/MacOS/surogate-helper's signature has no secure timestamp" \
  "$SIGN" verify "$work/no-timestamp/Surogate.app" trial "$SIGNER"

copy "$signed" "$work/entitled"
codesign --force --sign "$identity" --keychain "$work/test.keychain-db" --options runtime --timestamp \
  --entitlements "$(dirname "$SIGN")/../macos/entitlements/vm.plist" "$work/entitled/Surogate.app/Contents/Resources/app/bin/node"
refuses "refuses a program with other entitlements than its own" "Contents/Resources/app/bin/node has other entitlements" \
  "$SIGN" verify "$work/entitled/Surogate.app" trial "$SIGNER"

copy "$signed" "$work/unsigned"
program "$work/unsigned/Surogate.app/Contents/Resources/app/bin/surogate-spawn"
refuses "refuses a program the signing did not sign" "Contents/Resources/app/bin/surogate-spawn is not signed by $SIGNER alone" \
  "$SIGN" verify "$work/unsigned/Surogate.app" trial "$SIGNER"

copy "$signed" "$work/added"
printf 'more\n' >"$work/added/Surogate.app/Contents/Resources/app/added.txt"
refuses "refuses a bundle with a file added after its signing" "signature does not check out" "$SIGN" verify "$work/added/Surogate.app" trial "$SIGNER"

fixture "$work/renamed"
plutil -replace CFBundleIdentifier -string ai.invergent.other "$work/renamed/Surogate.app/Contents/Info.plist"
"$SIGN" sign "$work/renamed/Surogate.app" "$identity" "$work/test.keychain-db" >/dev/null
refuses "refuses an app of another identifier" "is not ai.invergent.surogate by that signer" "$SIGN" verify "$work/renamed/Surogate.app" trial "$SIGNER"

fixture "$work/adhoc"
takes "signs a development build ad hoc" "$SIGN" sign "$work/adhoc/Surogate.app" -
takes "checks it as signed ad hoc" "$SIGN" verify "$work/adhoc/Surogate.app" adhoc
refuses "takes an ad hoc bundle for no trial's" "is not signed by $SIGNER alone" "$SIGN" verify "$work/adhoc/Surogate.app" trial "$SIGNER"
info="$(codesign -dvvv "$work/adhoc/Surogate.app/Contents/MacOS/Surogate" 2>&1)"
grep -Eq 'flags=0x[0-9a-f]+\([a-z,-]*runtime' <<<"$info" && not_ok "signs a development build with the hardened runtime" \
  || ok "signs a development build without the hardened runtime, as Electron's own build is signed"
[ "$(signers "$work/adhoc/Surogate.app/Contents/MacOS/surogate-helper")" = "$(signers "$work/adhoc/Surogate.app/Contents/MacOS/Surogate")" ] \
  && ok "signs a development build's surogate-helper ad hoc, as its app" || not_ok "signs a development build's surogate-helper otherwise than its app"

# An extended attribute on a file of the bundle, as macOS leaves some: the payload carries none, in no AppleDouble file
# and in no header (libarchive's LIBARCHIVE.xattr.<name> and SCHILY.xattr.<name> keywords), as either would break the seal
# of the bundle the updater unpacks. Its name is no part of any file's content in the bundle.
PROBE=ai.invergent.surogate.packprobe
xattr -w "$PROBE" kept "$signed/Contents/Resources/app/package.json"
[ -n "$(xattr -p "$PROBE" "$signed/Contents/Resources/app/package.json" 2>/dev/null)" ] || not_ok "the test's extended attribute was set"
takes "packs the update payload" "$SIGN" pack "$signed" "$work/payload.tar.gz"
mkdir "$work/unpacked"
tar -xzf "$work/payload.tar.gz" -C "$work/unpacked"
[ -z "$(xattr -lr "$work/unpacked" 2>/dev/null)" ] && ok "packs no extended attribute: unpacked, no file of it has one" || not_ok "packs extended attributes: $(xattr -lr "$work/unpacked" | head -3)"
[ "$(gzip -dc "$work/payload.tar.gz" | LC_ALL=C grep -a -c "xattr\.$PROBE" || :)" = 0 ] \
  && ok "packs no header that names an extended attribute" || not_ok "packs a header that names an extended attribute"
xattr -d "$PROBE" "$signed/Contents/Resources/app/package.json"
[ "$(ls -A "$work/unpacked")" = Surogate.app ] && ok "packs Surogate.app as its one top folder" || not_ok "packs another top folder: $(ls -A "$work/unpacked")"
[ -z "$(tar -tzf "$work/payload.tar.gz" | grep -E '(^|/)\._' || :)" ] && ok "packs no AppleDouble file" || not_ok "packs AppleDouble files"
takes "packs the bundle as it was signed: unpacked, it checks out as the test's" "$SIGN" verify "$work/unpacked/Surogate.app" trial "$SIGNER"
mkdir -p "$work/elsewhere/Other.app"
refuses "packs nothing but Surogate.app" "the update payload's one folder is Surogate.app" "$SIGN" pack "$work/elsewhere/Other.app" "$work/other.tar.gz"

takes "makes the disk image, signed" "$SIGN" dmg "$signed" "$work/Surogate.dmg" "$identity" "$work/test.keychain-db"
takes "checks the disk image as the test's, holding the bundle and the link to /Applications" "$SIGN" dmg-check "$work/Surogate.dmg" "$signed" trial "$SIGNER"
refuses "takes the disk image as no other signer's" "is not signed by $OTHER alone" "$SIGN" dmg-check "$work/Surogate.dmg" "$signed" trial "$OTHER"
refuses "refuses a disk image of another bundle than the one it is told" "holds another Surogate.app" "$SIGN" dmg-check "$work/Surogate.dmg" "$work/renamed/Surogate.app" trial "$SIGNER"
mkdir -p "$work/bare"
ditto "$signed" "$work/bare/Surogate.app"
hdiutil create -quiet -volname Surogate -srcfolder "$work/bare" -fs HFS+ -format UDZO "$work/bare.dmg"
codesign --force --sign "$identity" --keychain "$work/test.keychain-db" --timestamp "$work/bare.dmg"
refuses "refuses a disk image with no link to /Applications" "holds other than Surogate.app and the link to /Applications" "$SIGN" dmg-check "$work/bare.dmg" "$signed" trial "$SIGNER"
mkdir -p "$work/dotted"
ditto "$signed" "$work/dotted/Surogate.app"
ln -s /Applications "$work/dotted/Applications"
printf 'hidden\n' >"$work/dotted/.hidden"
hdiutil create -quiet -volname Surogate -srcfolder "$work/dotted" -fs HFS+ -format UDZO "$work/dotted.dmg"
codesign --force --sign "$identity" --keychain "$work/test.keychain-db" --timestamp "$work/dotted.dmg"
refuses "refuses a disk image that holds a hidden file beside the app and the link" "holds other than Surogate.app and the link to /Applications: .hidden" \
  "$SIGN" dmg-check "$work/dotted.dmg" "$signed" trial "$SIGNER"
said="$("$SIGN" dmg-check "$work/Surogate.dmg" "$signed" trial "$SIGNER" 2>&1)" || :
grep -q "beside the volume's own: " <<<"$said" && ok "says what of the volume's own the disk image holds ($said)" || not_ok "says nothing of the volume's own entries: $said"

refuses "notarizes nothing without the notary service's key" "APPLE_NOTARY_KEY_P8" env -u APPLE_NOTARY_KEY_P8 "$SIGN" notarize "$signed"
refuses "takes no bundle but a notarized one for Gatekeeper's" "Gatekeeper" "$SIGN" assess "$signed" "$work/Surogate.dmg"
refuses "stages no bundle but a notarized one as its updater's update" "Gatekeeper" "$SIGN" staged "$signed" 1.2.3
for version in 1.2 0.0.0 01.2.3; do
  refuses "stages an update of no version but a release's: $version" "is no release's version" "$SIGN" staged "$signed" "$version"
done
refuses "imports no identity without the certificate" "MACOS_SIGNING_CERTIFICATE_P12" env -u MACOS_SIGNING_CERTIFICATE_P12 "$SIGN" import "$work/developer-id.keychain-db" ABCDE12345
refuses "imports for no Team ID but one of ten capitals and digits" "is no Team ID" "$SIGN" import "$work/developer-id.keychain-db" abcde
# A certificate that is a code signing identity, but no Developer ID: refused, as no valid identity of Apple's issuing.
/usr/bin/openssl req -new -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=Developer ID Application: Not Apple's (ABCDE12345)/OU=ABCDE12345" \
  -addext "extendedKeyUsage=critical,codeSigning" -keyout "$work/fake.key" -out "$work/fake.pem" 2>/dev/null \
  || /usr/bin/openssl req -new -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=Developer ID Application: Not Apple's (ABCDE12345)/OU=ABCDE12345" \
    -keyout "$work/fake.key" -out "$work/fake.pem" 2>/dev/null
/usr/bin/openssl pkcs12 -export -inkey "$work/fake.key" -in "$work/fake.pem" -passout pass:fake -out "$work/fake.p12"
refuses "imports no certificate that Apple did not issue, whatever it calls itself" "holds not one valid code signing identity" \
  env MACOS_SIGNING_CERTIFICATE_P12="$(base64 <"$work/fake.p12")" MACOS_SIGNING_CERTIFICATE_PASSWORD=fake "$SIGN" import "$work/fake.keychain-db" ABCDE12345
"$SIGN" forget "$work/fake.keychain-db" 2>/dev/null || :

"$SIGN" forget "$work/other.keychain-db"
if grep -q "$work/other.keychain-db" <<<"$(security list-keychains -d user)"; then not_ok "forgets a keychain: it is still searched"; else ok "forgets a keychain, which is searched no more"; fi
[ ! -e "$work/other.keychain-db" ] && ok "forgets a keychain, which is gone" || not_ok "forgets a keychain: it is still there"

if [ "$failed" -gt 0 ]; then
  echo "$failed failed"
  exit 1
fi
echo "all passed"
