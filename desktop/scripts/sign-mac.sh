#!/usr/bin/env bash
# Surogate.app signed, and the disk image a person installs it from (Apple, "Notarizing macOS software before
# distribution"; codesign(1), hdiutil(1), notarytool, stapler, spctl(8)). Signed inside out, each code object by itself and
# never with --deep, which would give every one the same entitlements: each Mach-O file that is no bundle's own program,
# then each framework, then each helper app, then the app. Each with the entitlements its file in macos/entitlements
# names, and every other with none: Surogate, its four helper apps and the app's node, com.apple.security.cs.allow-jit
# (app.plist, helper.plist, node.plist); surogate-vm, com.apple.security.virtualization (vm.plist).
#
#   sign-mac.sh trial <keychain> <name>        a code signing identity of this run's own, its certificate self-signed for
#                                              <name> and kept nowhere, in a keychain of its own at <keychain> (a path that
#                                              ends in .keychain-db), unlocked and searched; says the identity's SHA-1
#   sign-mac.sh import <keychain> <team>       the Developer ID Application identity in MACOS_SIGNING_CERTIFICATE_P12
#                                              (base64) and MACOS_SIGNING_CERTIFICATE_PASSWORD, in a keychain of its own at
#                                              <keychain> with Apple's Developer ID intermediate, by its pin; says its
#                                              SHA-1, and refuses a certificate that is not the one identity of team <team>
#   sign-mac.sh forget <keychain>              the keychain gone, and out of the search list
#   sign-mac.sh sign <app> <identity> [<keychain>]
#                                              every code object of <app>, by the identity of that SHA-1, with the hardened
#                                              runtime and a secure timestamp; or by - , ad hoc, as a development build and
#                                              the runner run it (scripts/package-mac.sh), with neither
#   sign-mac.sh verify <app> adhoc | trial <name> | developer-id <team>
#                                              each Mach-O file of <app> signed by that signer alone, with exactly its
#                                              entitlements, and but ad hoc with the hardened runtime and a secure
#                                              timestamp; the app by its identifier and that signer; and codesign's own
#                                              strict check of the whole
#   sign-mac.sh pack <app> <tarball>           the update payload: <app>, which is Surogate.app, its one top folder, with
#                                              none of macOS's metadata
#   sign-mac.sh dmg <app> <dmg> <identity> [<keychain>]
#                                              the disk image: <app> and a link to /Applications, signed as sign signs
#   sign-mac.sh dmg-check <dmg> <app> adhoc | trial <name> | developer-id <team>
#                                              the image signed by that signer, holding <app> by its own signature and the
#                                              link to /Applications, and nothing else, hidden or not, but what macOS keeps
#                                              on any volume
#   sign-mac.sh notarize <app or dmg>          notarized by Apple's notary service and stapled, with the App Store Connect
#                                              API key in APPLE_NOTARY_KEY_P8 (base64), APPLE_NOTARY_KEY_ID and
#                                              APPLE_NOTARY_ISSUER_ID
#   sign-mac.sh assess <app> <dmg>             Gatekeeper's own assessment of both, as a Mac that downloaded them makes it
#
# notarize and assess are the release's alone (release.yml's desktop-mac-sign, with the Developer ID): Apple notarizes
# no other signature, and Gatekeeper accepts none. Nothing here runs npm or node.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ENTITLEMENTS="$HERE/../macos/entitlements"
# Apple's Developer ID Certification Authority (G2), which issues a Developer ID Application certificate, by its sha256.
DEVELOPER_ID_CA=https://www.apple.com/certificateauthority/DeveloperIDG2CA.cer
DEVELOPER_ID_CA_SHA256=f16cd3c54c7f83cea4bf1a3e6a0819c8aaa8e4a1528fd144715f350643d2df3a
IDENTIFIER=ai.invergent.surogate

fail() {
  echo "sign-mac.sh: $*" >&2
  exit 1
}
usage() {
  echo "usage: sign-mac.sh trial|import|forget|sign|verify|pack|dmg|dmg-check|notarize|assess ... (see the script's head)" >&2
  exit 2
}

# Each Mach-O file of bundle $1, by its path from the bundle, one a line: macOS's own file(1) reads each file's magic.
machos() {
  (cd "$1" && find . -type f -exec file --mime-type {} + | sed -n 's|^\./\(.*\): *application/x-mach-binary$|\1|p' | LC_ALL=C sort)
}

# Whether Mach-O file $1, by its path from the app, is its bundle's own program: the app's, a helper app's or a
# framework's, which is signed with its bundle and never by itself.
is_main() {
  local name
  case "$1" in
    Contents/MacOS/Surogate) return 0 ;;
    Contents/Frameworks/*.app/Contents/MacOS/*)
      name="${1#Contents/Frameworks/}"
      name="${name%%.app/*}"
      [ "$1" = "Contents/Frameworks/$name.app/Contents/MacOS/$name" ]
      ;;
    Contents/Frameworks/*.framework/Versions/*)
      name="${1#Contents/Frameworks/}"
      name="${name%%.framework/*}"
      [ "$1" = "Contents/Frameworks/$name.framework/Versions/A/$name" ]
      ;;
    *) return 1 ;;
  esac
}

# The entitlements file of Mach-O file $1, by its path from the app, or nothing where it has none.
entitlements_of() {
  case "$1" in
    Contents/MacOS/Surogate) echo "$ENTITLEMENTS/app.plist" ;;
    Contents/MacOS/surogate-vm) echo "$ENTITLEMENTS/vm.plist" ;;
    Contents/Resources/app/bin/node) echo "$ENTITLEMENTS/node.plist" ;;
    "Contents/Frameworks/Surogate Helper"*.app/Contents/MacOS/"Surogate Helper"*) if is_main "$1"; then echo "$ENTITLEMENTS/helper.plist"; fi ;;
    *) ;;
  esac
}

# A keychain of this run's own at $1, with password $2, made here and kept nowhere: unlocked for six hours, and first in
# the user's search list, where codesign finds its identity.
keychain() {
  local listed
  case "$1" in /*.keychain-db) ;; *) fail "a run's keychain is a whole path that ends in .keychain-db, not $1" ;; esac
  [ ! -e "$1" ] || fail "$1 is there already: a run's keychain is its own"
  security create-keychain -p "$2" "$1"
  security set-keychain-settings -lut 21600 "$1"
  security unlock-keychain -p "$2" "$1"
  listed="$(security list-keychains -d user | sed -e 's/^[[:space:]]*"//' -e 's/"$//')"
  local IFS="
"
  # shellcheck disable=SC2086 # one keychain a line
  security list-keychains -d user -s "$1" $listed
}

# Lets codesign use the keys of keychain $1, whose password is $2, with no prompt (Apple, security(1), set-key-partition-list).
let_codesign() {
  security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$2" "$1" >/dev/null
}

# Whether the signature that codesign -dvvv describes as $2, of code $1, is the signer's that $3 and $4 name, and, but
# ad hoc, with a secure timestamp and, for code that runs ($5 is code), with the hardened runtime.
signer_is() {
  local authorities team first
  authorities="$(printf '%s\n' "$2" | sed -n 's/^Authority=//p')"
  team="$(printf '%s\n' "$2" | sed -n 's/^TeamIdentifier=//p')"
  case "$3" in
    adhoc)
      grep -qx 'Signature=adhoc' <<<"$2" && [ -z "$authorities" ] || fail "$1 is not signed ad hoc"
      return 0
      ;;
    trial)
      [ "$authorities" = "$4" ] && [ "$team" = "not set" ] || fail "$1 is not signed by $4 alone: its signers are ${authorities:-none}"
      ;;
    developer-id)
      first="$(printf '%s\n' "$authorities" | sed -n 1p)"
      case "$first" in "Developer ID Application: "*" ($4)") ;; *) fail "$1 is not signed by team $4's Developer ID Application certificate: its signers are ${authorities:-none}" ;; esac
      [ "$(printf '%s\n' "$authorities" | sed 1d)" = "Developer ID Certification Authority
Apple Root CA" ] || fail "$1's certificate is not issued by Apple's Developer ID authority: its signers are $authorities"
      [ "$team" = "$4" ] || fail "$1's Team ID is $team, not $4"
      ;;
  esac
  grep -q '^Timestamp=' <<<"$2" || fail "$1's signature has no secure timestamp"
  [ "$5" != code ] || grep -Eq '^CodeDirectory .* flags=0x[0-9a-f]+\(([a-z-]+,)*runtime(,[a-z-]+)*\)' <<<"$2" \
    || fail "$1 is not signed with the hardened runtime"
}

# The signer verify and dmg-check are told, as $1 and $2, is one of the three.
a_signer() {
  case "$1" in
    adhoc) [ "$#" -eq 1 ] ;;
    trial) [ "$#" -eq 2 ] && [ -n "$2" ] ;;
    developer-id) [ "$#" -eq 2 ] && [[ "$2" =~ ^[A-Z0-9]{10}$ ]] ;;
    *) false ;;
  esac || usage
}

# The requirement app $1 is checked by, as the updater checks a new bundle: its identifier, and its signer's.
requirement() {
  case "$1" in
    adhoc) echo "identifier \"$IDENTIFIER\"" ;;
    trial) echo "identifier \"$IDENTIFIER\" and certificate leaf[subject.CN] = \"$2\"" ;;
    developer-id) echo "identifier \"$IDENTIFIER\" and anchor apple generic and certificate leaf[subject.OU] = \"$2\"" ;;
  esac
}

VERB="${1:-}"
[ "$#" -gt 0 ] && shift
case "$VERB" in
  trial | import | pack | assess) [ "$#" -eq 2 ] || usage ;;
  forget | notarize) [ "$#" -eq 1 ] || usage ;;
  sign) [ "$#" -eq 2 ] || [ "$#" -eq 3 ] || usage ;;
  dmg) [ "$#" -eq 3 ] || [ "$#" -eq 4 ] || usage ;;
  verify) [ "$#" -ge 2 ] || usage; a_signer "${@:2}" ;;
  dmg-check) [ "$#" -ge 3 ] || usage; a_signer "${@:3}" ;;
  *) usage ;;
esac
[ "$(uname -s)" = Darwin ] || fail "it signs on a Mac, with Apple's own tools"

case "$VERB" in
  trial)
    kc="$1"
    name="$2"
    work="$(mktemp -d)"
    trap 'rm -rf "$work"' EXIT
    pass="$(/usr/bin/openssl rand -hex 24)"
    # A certificate for code signing alone, of one day, whose key is made here: macOS's own LibreSSL, whose PKCS#12
    # security(1) imports.
    printf '[req]\ndistinguished_name = name\nx509_extensions = signing\n[name]\n[signing]\nbasicConstraints = critical,CA:FALSE\nkeyUsage = critical,digitalSignature\nextendedKeyUsage = critical,codeSigning\nsubjectKeyIdentifier = hash\n' >"$work/signing.cnf"
    /usr/bin/openssl req -new -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=$name" -config "$work/signing.cnf" \
      -keyout "$work/key.pem" -out "$work/certificate.pem" 2>/dev/null || fail "openssl made no certificate for $name"
    /usr/bin/openssl pkcs12 -export -inkey "$work/key.pem" -in "$work/certificate.pem" -name "$name" -passout "pass:$pass" -out "$work/identity.p12"
    keychain "$kc" "$pass"
    security import "$work/identity.p12" -k "$kc" -f pkcs12 -P "$pass" -T /usr/bin/codesign >/dev/null
    let_codesign "$kc" "$pass"
    hash="$(/usr/bin/openssl x509 -in "$work/certificate.pem" -noout -fingerprint -sha1 | sed 's/.*=//' | tr -d :)"
    grep -q "$hash" <<<"$(security find-identity -p codesigning "$kc")" || fail "$kc holds no identity of $name's"
    echo "$hash"
    ;;
  import)
    kc="$1"
    team="$2"
    [[ "$team" =~ ^[A-Z0-9]{10}$ ]] || fail "$team is no Team ID, which is ten capital letters and digits"
    : "${MACOS_SIGNING_CERTIFICATE_P12:?}" "${MACOS_SIGNING_CERTIFICATE_PASSWORD:?}"
    work="$(mktemp -d)"
    trap 'rm -rf "$work"' EXIT
    (umask 077 && printf '%s' "$MACOS_SIGNING_CERTIFICATE_P12" | tr -d '\r\n ' | base64 --decode >"$work/identity.p12") \
      || fail "MACOS_SIGNING_CERTIFICATE_P12 is not base64"
    # -q first, or curl reads the user's curlrc, whose headers, logins and netrc would go out with it.
    curl -q -fsSL --proto '=https' -o "$work/intermediate.cer" "$DEVELOPER_ID_CA"
    echo "$DEVELOPER_ID_CA_SHA256  $work/intermediate.cer" | shasum -a 256 --check --status || fail "$DEVELOPER_ID_CA is not the pinned certificate"
    pass="$(/usr/bin/openssl rand -hex 24)"
    keychain "$kc" "$pass"
    # The certificate's password reaches security(1) on its command line, which has no other way to take one: the job
    # that signs runs nothing but Apple's tools and this script.
    security import "$work/identity.p12" -k "$kc" -f pkcs12 -P "$MACOS_SIGNING_CERTIFICATE_PASSWORD" -T /usr/bin/codesign >/dev/null \
      || fail "MACOS_SIGNING_CERTIFICATE_P12 is not a certificate and key that MACOS_SIGNING_CERTIFICATE_PASSWORD opens"
    security import "$work/intermediate.cer" -k "$kc" >/dev/null
    let_codesign "$kc" "$pass"
    found="$(security find-identity -v -p codesigning "$kc" | grep -E '^ *[0-9]+\) [0-9A-F]{40} "' || :)"
    [ -n "$found" ] && [ "$(printf '%s\n' "$found" | wc -l | tr -d ' ')" = 1 ] \
      || fail "MACOS_SIGNING_CERTIFICATE_P12 holds not one valid code signing identity, but: ${found:-none}"
    grep -Eq "^ *1\) [0-9A-F]{40} \"Developer ID Application: .+ \\($team\\)\"$" <<<"$found" \
      || fail "MACOS_SIGNING_CERTIFICATE_P12 is not team $team's Developer ID Application certificate, but $found: a certificate of another team signs nothing"
    printf '%s\n' "$found" | sed -E 's/^ *1\) ([0-9A-F]{40}) .*/\1/'
    ;;
  forget)
    [ ! -e "$1" ] || security delete-keychain "$1"
    ;;
  sign)
    app="$1"
    identity="$2"
    [ -f "$app/Contents/Info.plist" ] || fail "$app is no app bundle"
    [ "$identity" = - ] || [[ "$identity" =~ ^[0-9A-F]{40}$ ]] || fail "$identity is neither - nor an identity's SHA-1"
    opts=(--force --sign "$identity")
    if [ "$identity" != - ]; then
      opts+=(--options runtime --timestamp)
      [ "$#" -lt 3 ] || opts+=(--keychain "$3")
    fi
    # $1 signed, with the entitlements of its program, $2 by its path from the app.
    signed() {
      local entitlements
      entitlements="$(entitlements_of "$2")"
      if [ -n "$entitlements" ]; then
        codesign "${opts[@]}" --entitlements "$entitlements" "$1"
      else
        codesign "${opts[@]}" "$1"
      fi
    }
    files="$(machos "$app")"
    [ -n "$files" ] || fail "$app holds no Mach-O file"
    while IFS= read -r file; do
      is_main "$file" || signed "$app/$file" "$file"
    done <<<"$files"
    for framework in "$app"/Contents/Frameworks/*.framework; do
      signed "$framework" none
    done
    for helper in "$app"/Contents/Frameworks/*.app; do
      name="$(basename "$helper" .app)"
      signed "$helper" "Contents/Frameworks/$name.app/Contents/MacOS/$name"
    done
    signed "$app" Contents/MacOS/Surogate
    echo "signed $app"
    ;;
  verify)
    app="$1"
    files="$(machos "$app")"
    [ -n "$files" ] || fail "$app holds no Mach-O file"
    # Who signed each, and with what, first: a bundle's own check fails on any file of it that changed, and would say
    # less of which one did.
    while IFS= read -r file; do
      info="$(codesign -dvvv "$app/$file" 2>&1)" || fail "$file is not signed"
      signer_is "$file" "$info" "$2" "${3:-}" code
      # Each by plutil's own form of it, which orders a dict's keys: the entitlements a file names, or none.
      entitlements="$(entitlements_of "$file")"
      wanted=
      [ -z "$entitlements" ] || wanted="$(plutil -convert xml1 -o - "$entitlements")"
      held="$(codesign -d --entitlements - --xml "$app/$file" 2>/dev/null)" || held=
      [ -z "$held" ] || held="$(printf '%s' "$held" | plutil -convert xml1 -o - -)"
      [ "$held" = "$wanted" ] || fail "$file has other entitlements than ${entitlements:-none}: ${held:-none}"
    done <<<"$files"
    while IFS= read -r file; do
      said="$(codesign --verify --strict "$app/$file" 2>&1)" || fail "$file's signature does not check out: $said"
    done <<<"$files"
    said="$(codesign --verify --strict --deep --verbose=2 "$app" 2>&1)" || fail "$app's signature does not check out: $said"
    codesign --verify -R="$(requirement "$2" "${3:-}")" "$app" 2>/dev/null || fail "$app is not $IDENTIFIER by that signer"
    echo "checked $app: $(printf '%s\n' "$files" | wc -l | tr -d ' ') programs and libraries, each signed by its signer with its own entitlements"
    ;;
  pack)
    app="$1"
    out="$2"
    [ "$(basename "$app")" = Surogate.app ] && [ -d "$app" ] || fail "the update payload's one folder is Surogate.app, which $app is not"
    rm -f "$out"
    # No AppleDouble file and no extended attribute: the bundle's signature is in its files.
    COPYFILE_DISABLE=1 tar --no-mac-metadata --no-xattrs -czf "$out" -C "$(dirname "$app")" Surogate.app
    members="$(tar -tzf "$out")"
    [ -z "$(printf '%s\n' "$members" | grep -Ev '^Surogate\.app(/|$)' || :)" ] || fail "$out holds another member than Surogate.app's"
    [ -z "$(printf '%s\n' "$members" | grep -E '(^|/)\._' || :)" ] || fail "$out holds AppleDouble files"
    echo "packed $out"
    ;;
  dmg)
    app="$1"
    out="$2"
    identity="$3"
    [ "$(basename "$app")" = Surogate.app ] && [ -d "$app" ] || fail "the disk image holds Surogate.app, which $app is not"
    stage="$(mktemp -d)"
    trap 'rm -rf "$stage"' EXIT
    ditto "$app" "$stage/Surogate.app"
    ln -s /Applications "$stage/Applications"
    rm -f "$out"
    # hdiutil can find the image's new volume busy for a moment, as Spotlight or XProtect reads it: tried three times.
    made=
    for _ in 1 2 3; do
      if hdiutil create -quiet -volname Surogate -srcfolder "$stage" -fs HFS+ -format UDZO "$out"; then
        made=1
        break
      fi
      rm -f "$out"
      sleep 10
    done
    [ -n "$made" ] || fail "hdiutil made no disk image of $app"
    opts=(--force --sign "$identity")
    if [ "$identity" != - ]; then
      opts+=(--timestamp)
      [ "$#" -lt 4 ] || opts+=(--keychain "$4")
    fi
    codesign "${opts[@]}" "$out"
    echo "made $out"
    ;;
  dmg-check)
    dmg="$1"
    app="$2"
    info="$(codesign -dvvv "$dmg" 2>&1)" || fail "$dmg is not signed"
    codesign --verify --strict "$dmg" 2>/dev/null || fail "$dmg's signature does not check out"
    signer_is "$dmg" "$info" "$3" "${4:-}" image
    mounted="$(mktemp -d)"
    trap 'hdiutil detach -quiet "$mounted" 2>/dev/null || hdiutil detach -quiet -force "$mounted" 2>/dev/null || :; rmdir "$mounted" 2>/dev/null || :' EXIT
    hdiutil attach -quiet -nobrowse -readonly -noautoopen -mountpoint "$mounted" "$dmg" || fail "$dmg could not be attached"
    # Every entry at the volume's top, hidden ones too: the app and the link, and else only what macOS itself keeps on a
    # volume (an HFS+ volume's journal and its folder of hard links' data, the file system's event log, Spotlight's
    # index, the trash and the folder of temporary items), each said as it is found.
    found=
    own=
    while IFS= read -r -d '' entry; do
      name="${entry#"$mounted"/}"
      case "$name" in
        Applications | Surogate.app) found="$found+$name" ;;
        .fseventsd | .Trashes | .Spotlight-V100 | .TemporaryItems | .journal | .journal_info_block | ".HFS+ Private Directory Data"$'\r')
          own="$own $(printf '%q' "$name")" ;;
        *) fail "$dmg holds other than Surogate.app and the link to /Applications: $(printf '%q' "$name")" ;;
      esac
    done < <(find "$mounted" -mindepth 1 -maxdepth 1 -print0)
    case "$found" in +Applications+Surogate.app | +Surogate.app+Applications) ;; *) fail "$dmg holds other than Surogate.app and the link to /Applications" ;; esac
    [ -L "$mounted/Applications" ] && [ "$(readlink "$mounted/Applications")" = /Applications ] || fail "$dmg's Applications is not a link to /Applications"
    [ -d "$mounted/Surogate.app" ] && [ ! -L "$mounted/Surogate.app" ] || fail "$dmg's Surogate.app is no folder"
    # The app it holds is the one signed and checked, by its code directory's hashes.
    hashes() { codesign -dvvv "$1" 2>&1 | grep '^CDHash=' || :; }
    [ -n "$(hashes "$app")" ] && [ "$(hashes "$mounted/Surogate.app")" = "$(hashes "$app")" ] || fail "$dmg holds another Surogate.app than $app"
    said="$(codesign --verify --strict --deep --verbose=2 "$mounted/Surogate.app" 2>&1)" || fail "$dmg's Surogate.app's signature does not check out: $said"
    echo "checked $dmg: Surogate.app and the link to /Applications, beside the volume's own:${own:- none}"
    ;;
  notarize)
    file="$1"
    : "${APPLE_NOTARY_KEY_P8:?}" "${APPLE_NOTARY_KEY_ID:?}" "${APPLE_NOTARY_ISSUER_ID:?}"
    work="$(mktemp -d)"
    trap 'rm -rf "$work"' EXIT
    (umask 077 && printf '%s' "$APPLE_NOTARY_KEY_P8" | tr -d '\r\n ' | base64 --decode >"$work/key.p8") || fail "APPLE_NOTARY_KEY_P8 is not base64"
    case "$file" in
      *.app)
        # The notary service takes an app in a zip, which ditto makes as Finder does, the bundle's folder kept.
        ditto -c -k --keepParent "$file" "$work/submitted.zip"
        submitted="$work/submitted.zip"
        ;;
      *.dmg) submitted="$file" ;;
      *) fail "$file is neither an app nor a disk image" ;;
    esac
    key=(--key "$work/key.p8" --key-id "$APPLE_NOTARY_KEY_ID" --issuer "$APPLE_NOTARY_ISSUER_ID")
    xcrun notarytool submit "$submitted" "${key[@]}" --wait --timeout 1h --output-format json >"$work/answer.json" || :
    status="$(plutil -extract status raw -o - "$work/answer.json" 2>/dev/null)" || status=
    id="$(plutil -extract id raw -o - "$work/answer.json" 2>/dev/null)" || id=
    if [ "$status" != Accepted ]; then
      [ -z "$id" ] || xcrun notarytool log "$id" "${key[@]}" >&2 || :
      fail "Apple's notary service did not accept $file: ${status:-no answer}"
    fi
    xcrun stapler staple "$file"
    xcrun stapler validate "$file"
    echo "notarized $file"
    ;;
  assess)
    for each in "$1:execute" "$2:open"; do
      file="${each%:*}"
      kind="${each##*:}"
      # A disk image is assessed by its own signature, as Gatekeeper assesses one that is opened (spctl(8), --context).
      context=(--verbose=4)
      [ "$kind" = execute ] || context+=(--context context:primary-signature)
      said="$(spctl --assess --type "$kind" "${context[@]}" "$file" 2>&1)" || fail "Gatekeeper refuses $file: $said"
      grep -qx 'source=Notarized Developer ID' <<<"$said" || fail "Gatekeeper takes $file for other than notarized Developer ID software: $said"
      xcrun stapler validate "$file" >/dev/null || fail "$file has no notarization ticket stapled to it"
    done
    echo "assessed $1 and $2: notarized Developer ID software"
    ;;
esac
