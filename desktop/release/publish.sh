#!/usr/bin/env bash
# Surogate Desktop's release on the release bucket (spec, Section 9), served at <base>/desktop/:
#
#   desktop/install.sh                                    the newest install script
#   desktop/latest.json                                   the newest release's manifest; its signature is that release's own
#   desktop/releases/<version>/manifest.json, .sig        each release's own, kept
#   desktop/releases/<version>/surogate-desktop-<version>-linux-x64.tar.gz
#
# Each for the target DESKTOP_TARGET names, the platform and architecture the release is built for:
# linux-x64, as above, where it names none; darwin-arm64's and win32-x64's in folders of their own,
# with no install script, which installs linux-x64's alone (see target_files):
#
#   desktop/darwin-arm64/latest.json
#   desktop/darwin-arm64/Surogate.dmg                     the newest release's disk image, under a name that stays, for the website
#   desktop/releases/<version>/darwin-arm64/manifest.json, .sig
#   desktop/releases/<version>/darwin-arm64/surogate-desktop-<version>-darwin-arm64.tar.gz   Surogate.app, which the app updates from
#   desktop/releases/<version>/darwin-arm64/surogate-desktop-<version>-darwin-arm64.dmg      the disk image a person installs it from
#
#   desktop/win32-x64/latest.json
#   desktop/win32-x64/Surogate.msi                        the newest release's MSI, under a name that stays, for the website
#   desktop/releases/<version>/win32-x64/manifest.json, .sig
#   desktop/releases/<version>/win32-x64/surogate-desktop-<version>-win32-x64.msi
#
# Usage, after scripts/package.sh <version> <vm manifest> <out>:
#   release/publish.sh describe <version> <out>   # <out>/manifest.json, of the tarball whose sha256
#                                                 # is DESKTOP_TARBALL_SHA256 (the build's own word for
#                                                 # what it made) and whose root helper is the
#                                                 # install.sh beside this script; for win32-x64, of the
#                                                 # MSI the release signed, by DESKTOP_TARBALL_SHA256 and
#                                                 # DESKTOP_TARBALL_SIZE, and of the layout it is made of,
#                                                 # <out>/Surogate (describe_layout); for darwin-arm64, of
#                                                 # the tarball of Surogate.app the release signed, by the
#                                                 # same two words (describe_bundle). All that reads the
#                                                 # build's bytes is here, and here is no release key:
#                                                 # with DESKTOP_RELEASE_KEY in its environment, set or
#                                                 # empty, it refuses
#   release/publish.sh sign <version> <out>       # <out>/manifest.json and <out>/manifest.json.sig:
#                                                 # the release's manifest, written here from four
#                                                 # words, and its Ed25519 signature, with
#                                                 # DESKTOP_RELEASE_KEY (its PEM), which must be one of
#                                                 # the keys install.sh lists. The words: the version;
#                                                 # DESKTOP_TARBALL_SHA256 and DESKTOP_TARBALL_SIZE, the
#                                                 # build's own for its tarball; and
#                                                 # DESKTOP_STATE_SCHEMA, which describe read. It opens
#                                                 # no tarball, and reads no file that another wrote
#   release/publish.sh send <version> <out>       # the release, once its manifest and signature are a
#                                                 # pair by install.sh's keys and its tarball the one
#                                                 # the manifest names: each object sent and read back,
#                                                 # in the order said at send; never a release again,
#                                                 # and latest.json only for the newest version. For
#                                                 # darwin-arm64, its disk image too, <out>/surogate-
#                                                 # desktop-<version>-darwin-arm64.dmg, only as the one
#                                                 # whose sha256 is DESKTOP_DMG_SHA256, the signing job's
#                                                 # own word for it
# Environment for send: S3_ENDPOINT (R2's https://<account>.r2.cloudflarestorage.com), S3_BUCKET,
# AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY.
set -euo pipefail
# What reads the build's tarball, or what it unpacks to, runs where no release key is: a program
# reads its parent's first environment whatever the parent takes out of its own later, so a key
# that this shell was started with is one read away from every program it starts. Refused before
# the first of them is started.
if [ "${1:-}" = describe ] && [ -n "${DESKTOP_RELEASE_KEY+in}" ]; then
  echo "publish.sh: describe reads the build's tarball, and runs only where no release key is: DESKTOP_RELEASE_KEY is in its environment" >&2
  exit 1
fi
# In a signing the release key is this shell's own from here on, and in the environment of no
# program it starts: it is handed to openssl alone, through a pipe.
export -n DESKTOP_RELEASE_KEY
# All it reads, it reads in no locale and no language of its caller's: in most locales, more than
# ten characters are digits, and a version's are the ten.
export LC_ALL=C LANG=C
unset LANGUAGE

HERE="$(cd "$(dirname "$0")" && pwd)"
VERB="${1:-}"
VERSION="${2:-}"
OUT="${3:-}"
# A version is x.y.z with no zero before a part, as the install script takes one: dpkg reads
# 1.2.03 as 1.2.3, and a release has one name.
[[ "$VERSION" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] && [ -d "$OUT" ] || { echo "usage: publish.sh describe|sign|send <x.y.z> <out>" >&2; exit 2; }
# The names of the release's files for target $1: its manifest's platform and arch; its payload, a
# tarball or Windows's MSI, and the payload's type; the folder under desktop/ that the release's own
# three are sent to and kept in; the manifest of the newest release; and the object, if any, that
# goes last, after latest.json, under a name that stays: linux-x64's install script, win32-x64's MSI
# and darwin-arm64's disk image for the website; and darwin-arm64's disk image under its version,
# IMAGE, sent with the release's own. The app names them the same (TARGETS, in src/platform.ts).
target_files() {
  case "$1" in
    linux-x64) PLATFORM=linux ARCH=x64 RELEASE="releases/$VERSION" LATEST=latest.json ARCHIVE="surogate-desktop-$VERSION-$1.tar.gz" ARCHIVE_TYPE=application/gzip
      LAST=install.sh LAST_FROM="$HERE/install.sh" LAST_TYPE=text/x-shellscript IMAGE= ;;
    darwin-arm64) PLATFORM=darwin ARCH=arm64 RELEASE="releases/$VERSION/darwin-arm64" LATEST=darwin-arm64/latest.json ARCHIVE="surogate-desktop-$VERSION-$1.tar.gz" ARCHIVE_TYPE=application/gzip
      IMAGE="surogate-desktop-$VERSION-$1.dmg" LAST=darwin-arm64/Surogate.dmg LAST_FROM="$OUT/surogate-desktop-$VERSION-$1.dmg" LAST_TYPE=application/x-apple-diskimage ;;
    win32-x64) PLATFORM=win32 ARCH=x64 RELEASE="releases/$VERSION/win32-x64" LATEST=win32-x64/latest.json ARCHIVE="surogate-desktop-$VERSION-$1.msi" ARCHIVE_TYPE=application/x-msi
      LAST=win32-x64/Surogate.msi LAST_FROM="$OUT/$ARCHIVE" LAST_TYPE=application/x-msi IMAGE= ;;
    *) return 1 ;;
  esac
}
DESKTOP_TARGET="${DESKTOP_TARGET:-linux-x64}"
target_files "$DESKTOP_TARGET" || { echo "publish.sh: DESKTOP_TARGET is $DESKTOP_TARGET, and a release is made for linux-x64, darwin-arm64 or win32-x64" >&2; exit 2; }
fail() {
  echo "publish.sh: $*" >&2
  exit 1
}
# The manifest of release $1, whose tarball has sha256 $2 and is $3 bytes, and whose app keeps
# state of schema $4: one line, as every install takes one, and the bytes that are signed.
manifest() {
  jq -cn --arg version "$1" --arg sha256 "$2" --argjson size "$3" --argjson schema "$4" --arg platform "$PLATFORM" --arg arch "$ARCH" --arg url "$RELEASE/$ARCHIVE" \
    '{version: $version, channel: "stable", platform: $platform, arch: $arch, url: $url, sha256: $sha256, size: $size, stateSchema: $schema}'
}

# The release that manifest $1 of this version is to its readers, as "<version> <sha256> <size>",
# or nothing. linux-x64's is read by the root helper's own check (release_of in install.sh), which
# every install and every update asks, from the script's functions without its last line, which
# runs it. Another target has no root helper, and its manifest is read by that check's rules for
# its own platform, arch and tarball, as its app reads one (signedRelease in src/shell/updates.ts):
# one JSON object by the script's own reader (one_object), of the script's channel.
release_read() {
  if [ "$DESKTOP_TARGET" = linux-x64 ]; then
    bash -c '. <(sed "\$d" "$1") && settings && release_of "$2"' _ "$HERE/install.sh" "$1"
    return
  fi
  bash -c '. <(sed "\$d" "$1") && settings && one_object "$2" | jq -er --arg channel "$CHANNEL" --arg version "$3" --arg platform "$4" --arg arch "$5" --arg url "$6" '"'"'
    select(.version == $version and .channel == $channel and .platform == $platform and .arch == $arch and .url == $url
      and (.sha256 | type == "string" and test("\\A[0-9a-f]{64}\\z"))
      and (.size | type == "number" and . == floor and (floor | . >= 1 and . < 1e15))
      and (.stateSchema | type == "number" and . == floor and (floor | . >= 1 and . < 1e15)))
    | "\(.version) \(.sha256) \(.size | floor)"'"'"'' _ "$HERE/install.sh" "$1" "$VERSION" "$PLATFORM" "$ARCH" "$RELEASE/$ARCHIVE"
}

# Whether the install script beside this one lists its release keys in the one form that an
# install reads (see the list, in install.sh): read by the script's own reader of a helper's list
# (listed, from its functions without its last line, which runs it), they are the list as bash
# set it, key for key, and at least one. Each is then an Ed25519 public key as OpenSSL writes one,
# which is that reader's own form of a key: no key of another kind is signed with or sent for,
# and so no signature of other than Ed25519's 64 bytes, which is all an install takes. Every install
# asks that reader, and the app asks one of its own that takes the same form: a list that bash
# alone reads would be signed for, installed, and trust no key. With $1, that key is one of them.
keys_listed() {
  bash -c '. <(sed "\$d" "$1") && settings && listed read "$1" && [ "${#read[@]}" -gt 0 ] && [ "${#read[@]}" -eq "${#RELEASE_KEYS[@]}" ] || exit 1
    among="${2:+no}"
    for at in "${!read[@]}"; do
      [ "${read[at]}" = "${RELEASE_KEYS[at]}" ] || exit 1
      [ "${read[at]}" != "${2:-}" ] || among=
    done
    [ -z "$among" ] || exit 3' _ "$HERE/install.sh" "${1:-}"
}
NOT_LISTED="install.sh's list of release keys is not in the one form that an install reads (see the list in install.sh): a computer that installed this release would take no later one"

# Windows's describe: the layout the MSI is made from, <out>/Surogate as scripts/package-win.sh lays it out and the
# release signs it, read as linux-x64's tarball is, with nothing unpacked: its app's state schema and version, by the
# install script's own reader, and its release keys, which the app checks its updates by, install.sh's key for key; and
# the MSI by the two words of the job that signed it, DESKTOP_TARBALL_SHA256 and DESKTOP_TARBALL_SIZE, as no MSI is
# opened here. What it writes is this job's own check of the release, as linux-x64's is.
describe_layout() {
  : "${DESKTOP_TARBALL_SHA256:?}" "${DESKTOP_TARBALL_SIZE:?}"
  [[ "$DESKTOP_TARBALL_SHA256" =~ ^[0-9a-f]{64}$ ]] || fail "DESKTOP_TARBALL_SHA256 is not a sha256, as the signing job gives its MSI's"
  [[ "$DESKTOP_TARBALL_SIZE" =~ ^[1-9][0-9]{0,14}$ ]] || fail "DESKTOP_TARBALL_SIZE is not a size in bytes, as the signing job gives its MSI's"
  [ "$(tail -n 1 "$HERE/install.sh")" = 'main "$@"' ] || fail 'install.sh does not end with the line that runs it (main "$@"): no package is read with it'
  keys_listed || fail "$NOT_LISTED"
  local tree package keys schema reader
  tree="$(realpath "$OUT")"
  # Each a file of the layout's own: no link, and under no folder that is one.
  package=Surogate/resources/app/package.json
  [ -f "$OUT/$package" ] && [ "$(realpath "$OUT/$package")" = "$tree/$package" ] || fail "the layout's resources/app/package.json names no stateSchema"
  reader='. <(sed "\$d" "$1") && settings && one_object "$2" any 1048576 | jq -e ".stateSchema | select(type == \"number\" and . == floor and (floor | . >= 1 and . < 1e15)) | floor"'
  schema="$(bash -c "$reader" _ "$HERE/install.sh" "$OUT/$package" 2>/dev/null)" || fail "the layout's resources/app/package.json names no stateSchema"
  reader='. <(sed "\$d" "$1") && settings && one_object "$2" any 1048576 | jq -e --arg version "$3" ".version == \$version" >/dev/null'
  bash -c "$reader" _ "$HERE/install.sh" "$OUT/$package" "$VERSION" 2>/dev/null || fail "the layout's app is not version $VERSION, by its resources/app/package.json"
  keys=Surogate/resources/release-keys
  [ -f "$OUT/$keys" ] && [ "$(realpath "$OUT/$keys")" = "$tree/$keys" ] \
    && bash -c '. <(sed "\$d" "$1") && settings && listed read "$2" && [ "${#read[@]}" -gt 0 ] && [ "${#read[@]}" -eq "${#RELEASE_KEYS[@]}" ] || exit 1
      for at in "${!read[@]}"; do [ "${read[at]}" = "${RELEASE_KEYS[at]}" ] || exit 1; done' _ "$HERE/install.sh" "$OUT/$keys" \
    || fail "the layout's resources/release-keys is not install.sh's list of release keys, key for key: its app would check its updates by others"
  # A signature that is here is of another manifest than the one written now.
  rm -f "$OUT/manifest.json.sig"
  manifest "$VERSION" "$DESKTOP_TARBALL_SHA256" "$DESKTOP_TARBALL_SIZE" "$schema" >"$OUT/manifest.json"
  echo "wrote $OUT/manifest.json"
}

# macOS's describe: the update payload, the tarball of the signed and stapled Surogate.app that the signing job packed
# (scripts/sign-mac.sh pack), by that job's two words for it, DESKTOP_TARBALL_SHA256 and DESKTOP_TARBALL_SIZE, unpacked
# whole as linux-x64's is: one top folder, Surogate.app, of files of one name each, folders and links that each lead
# inside it, which is all the app's updater takes. Its app's state schema and version by the install script's own reader, the version its
# Info.plist says, and its release keys, which the app checks its updates by, install.sh's key for key. What it writes
# is this job's own check of the release, as linux-x64's is.
describe_bundle() {
  : "${DESKTOP_TARBALL_SHA256:?}" "${DESKTOP_TARBALL_SIZE:?}"
  [[ "$DESKTOP_TARBALL_SHA256" =~ ^[0-9a-f]{64}$ ]] || fail "DESKTOP_TARBALL_SHA256 is not a sha256, as the signing job gives its tarball's"
  [[ "$DESKTOP_TARBALL_SIZE" =~ ^[1-9][0-9]{0,14}$ ]] || fail "DESKTOP_TARBALL_SIZE is not a size in bytes, as the signing job gives its tarball's"
  [ -f "$OUT/$ARCHIVE" ] || fail "$OUT/$ARCHIVE is not there: the signing job packs it (scripts/sign-mac.sh pack)"
  local sha256 size tree package keys schema reader link target shown
  sha256="$(sha256sum <"$OUT/$ARCHIVE" | cut -d' ' -f1)"
  [ "$sha256" = "$DESKTOP_TARBALL_SHA256" ] || fail "$OUT/$ARCHIVE is not the tarball the signing job made: its sha256 is $sha256, and the signing job's $DESKTOP_TARBALL_SHA256"
  size="$(stat -c %s "$OUT/$ARCHIVE")"
  [ "$size" = "$DESKTOP_TARBALL_SIZE" ] || fail "$OUT/$ARCHIVE is not the tarball the signing job made: its size is $size bytes, and the signing job's $DESKTOP_TARBALL_SIZE"
  [ "$(tail -n 1 "$HERE/install.sh")" = 'main "$@"' ] || fail 'install.sh does not end with the line that runs it (main "$@"): no package is read with it'
  keys_listed || fail "$NOT_LISTED"
  # Unpacked, read and removed as linux-x64's tarball is, by the same rules of signals (see describe).
  trap '' HUP INT PIPE TERM
  unpacked="$(mktemp -d --tmpdir release-unpacked-XXXXXXXXXX)"
  cleanup() {
    trap '' HUP INT PIPE TERM
    chmod -R u+rwX "$unpacked" 2>/dev/null || :
    rm -rf "$unpacked"
  }
  trap 'cleanup || :' EXIT
  trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 141' PIPE; trap 'exit 143' TERM
  tar -xzf "$OUT/$ARCHIVE" -C "$unpacked" --no-same-owner --no-same-permissions 2>/dev/null || fail "$OUT/$ARCHIVE could not be unpacked"
  tree="$(realpath "$unpacked")"
  # Surogate.app alone at its top, a folder and no link, of files, folders and links alone, and every link leading to
  # something of the bundle's own.
  shown="$(ls -A "$unpacked")"
  [ "$shown" = Surogate.app ] && [ -d "$unpacked/Surogate.app" ] && [ ! -L "$unpacked/Surogate.app" ] || fail "$OUT/$ARCHIVE holds other than Surogate.app at its top"
  shown="$(find "$unpacked/Surogate.app" ! -type f ! -type d ! -type l -print -quit)"
  [ -z "$shown" ] || fail "$OUT/$ARCHIVE holds other than files, folders and links: ${shown#"$unpacked"/}"
  while IFS= read -r -d '' link; do
    target="$(realpath -e "$link" 2>/dev/null)" || target=
    case "$target" in "$tree/Surogate.app"/*) ;; *) fail "$OUT/$ARCHIVE holds a link that leads out of Surogate.app, or to nothing: ${link#"$unpacked"/}" ;; esac
  done < <(find "$unpacked/Surogate.app" -type l -print0)
  # No AppleDouble file, which a tar of macOS's leaves for a file's extended attributes: its name would break the seal.
  shown="$(find "$unpacked/Surogate.app" -name '._*' -print -quit)"
  [ -z "$shown" ] || fail "$OUT/$ARCHIVE holds an AppleDouble file, which breaks the bundle's seal: ${shown#"$unpacked"/}"
  # No file of two names, which a hard link of the archive's unpacks as: the updater takes a file of one name alone, and
  # the app its release keys only so.
  shown="$(find "$unpacked/Surogate.app" -type f -links +1 -print -quit)"
  [ -z "$shown" ] || fail "$OUT/$ARCHIVE holds a file of more than one name, which the app's updater takes for none of its own: ${shown#"$unpacked"/}"
  # Each a file of the bundle's own: no link, and under no folder that is one.
  package=Surogate.app/Contents/Resources/app/package.json
  [ -f "$unpacked/$package" ] && [ "$(realpath "$unpacked/$package")" = "$tree/$package" ] || fail "the tarball's $package names no stateSchema"
  reader='. <(sed "\$d" "$1") && settings && one_object "$2" any 1048576 | jq -e ".stateSchema | select(type == \"number\" and . == floor and (floor | . >= 1 and . < 1e15)) | floor"'
  schema="$(bash -c "$reader" _ "$HERE/install.sh" "$unpacked/$package" 2>/dev/null)" || fail "the tarball's $package names no stateSchema"
  reader='. <(sed "\$d" "$1") && settings && one_object "$2" any 1048576 | jq -e --arg version "$3" ".version == \$version" >/dev/null'
  bash -c "$reader" _ "$HERE/install.sh" "$unpacked/$package" "$VERSION" 2>/dev/null || fail "the tarball's app is not version $VERSION, by its $package"
  # The version the bundle says of itself, in the XML that plutil keeps it in: its updater takes no other.
  shown="$(sed -n '/^[[:space:]]*<key>CFBundleShortVersionString<\/key>$/{n;s/^[[:space:]]*<string>\(.*\)<\/string>$/\1/p;}' "$unpacked/Surogate.app/Contents/Info.plist" 2>/dev/null)" || shown=
  [ "$shown" = "$VERSION" ] || fail "the tarball's Surogate.app is not version $VERSION, by its Info.plist's CFBundleShortVersionString"
  # The release keys' member as the archive has it, whose mode the unpacking here did not keep: one regular file, of
  # one name, that no group and no other may write, as the app takes its keys only so.
  keys=Surogate.app/Contents/Resources/release-keys
  shown="$(tar -tvzf "$OUT/$ARCHIVE" "$keys" 2>/dev/null)" || shown=
  [[ "$shown" == -????[!w]??[!w]?\ * && "$shown" != *$'\n'* ]] || fail "the tarball's $keys is not one regular file that only its owner may write"
  [ -f "$unpacked/$keys" ] && [ "$(realpath "$unpacked/$keys")" = "$tree/$keys" ] \
    && bash -c '. <(sed "\$d" "$1") && settings && listed read "$2" && [ "${#read[@]}" -gt 0 ] && [ "${#read[@]}" -eq "${#RELEASE_KEYS[@]}" ] || exit 1
      for at in "${!read[@]}"; do [ "${read[at]}" = "${RELEASE_KEYS[at]}" ] || exit 1; done' _ "$HERE/install.sh" "$unpacked/$keys" \
    || fail "the tarball's $keys is not install.sh's list of release keys, key for key: its app would check its updates by others"
  cleanup || fail "the unpacked tarball could not be removed from $unpacked: no manifest is written"
  # A signature that is here is of another manifest than the one written now.
  rm -f "$OUT/manifest.json.sig"
  manifest "$VERSION" "$sha256" "$size" "$schema" >"$OUT/manifest.json"
  echo "wrote $OUT/manifest.json"
}

case "$VERB" in
  describe)
    if [ "$DESKTOP_TARGET" = win32-x64 ]; then
      describe_layout
      exit 0
    fi
    if [ "$DESKTOP_TARGET" = darwin-arm64 ]; then
      describe_bundle
      exit 0
    fi
    : "${DESKTOP_TARBALL_SHA256:?}"
    [ -f "$OUT/$ARCHIVE" ] || fail "$OUT/$ARCHIVE is not there: run scripts/package.sh first"
    # The tarball is the one the build made, by the hash the build's job gave for it: an artifact
    # is its run's, and any job of the run may put another file under its name. What is no sha256 is
    # never said back.
    [[ "$DESKTOP_TARBALL_SHA256" =~ ^[0-9a-f]{64}$ ]] || fail "DESKTOP_TARBALL_SHA256 is not a sha256, as the build's job gives its tarball's"
    sha256="$(sha256sum <"$OUT/$ARCHIVE" | cut -d' ' -f1)"
    [ "$sha256" = "$DESKTOP_TARBALL_SHA256" ] || fail "$OUT/$ARCHIVE is not the tarball the build made: its sha256 is $sha256, and the build's $DESKTOP_TARBALL_SHA256"
    # The app's package is read below with the install script's own reader, from its functions
    # alone: its last line, which runs it, is left out. Were its last line any other, the line
    # that runs it would be left in, and its refusal taken for a package that names no schema
    # (see sign). Said before anything is unpacked.
    [ "$(tail -n 1 "$HERE/install.sh")" = 'main "$@"' ] || fail 'install.sh does not end with the line that runs it (main "$@"): no package is read with it'
    keys_listed || fail "$NOT_LISTED"
    # The tarball's root helper is the install script beside this one, byte for byte: installed,
    # it is what pkexec runs as root at the next update, and its release keys are the ones every
    # later update is checked against. The build holds no key, and a helper of its own would need
    # none. The helper is read from the tarball unpacked whole, as the helper that installs it
    # unpacks it: a member under the helper's own name may be replaced by a later one, or through
    # a link to its folder.
    # What is unpacked is gone however this ends, whatever the modes of its folders, which are the
    # build's too. A signal ends it once the command it runs has ended, as the script would end by
    # itself: removed beside a tar that still writes, the folder would keep what tar writes after.
    # No signal comes between the folder's making and its name being kept, nor stops mktemp then;
    # and none stops the removal, where a second one would end its rm.
    trap '' HUP INT PIPE TERM
    unpacked="$(mktemp -d --tmpdir release-unpacked-XXXXXXXXXX)"
    cleanup() {
      trap '' HUP INT PIPE TERM
      chmod -R u+rwX "$unpacked" 2>/dev/null || :
      rm -rf "$unpacked"
    }
    # A removal that fails as this ends leaves its status as it was.
    trap 'cleanup || :' EXIT
    trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 141' PIPE; trap 'exit 143' TERM
    tar -xzf "$OUT/$ARCHIVE" -C "$unpacked" --no-same-owner --no-same-permissions 2>/dev/null || fail "$OUT/$ARCHIVE could not be unpacked"
    helper="surogate-desktop-$VERSION-linux-x64/bin/surogate-apply-update"
    # A file of the tree's own: no link, and under no folder that is one. Asked in two commands: a
    # signal that comes while the first of two $( ) of one command is answered ends Ubuntu 24.04's
    # bash with an error of its own, before this script's handler has run.
    tree="$(realpath "$unpacked")"
    [ -f "$unpacked/$helper" ] && [ "$(realpath "$unpacked/$helper")" = "$tree/$helper" ] || fail "the tarball has no root helper of its own at $helper"
    cmp -s "$unpacked/$helper" "$HERE/install.sh" \
      || fail "the tarball's root helper, $helper, is not the install.sh beside this script, byte for byte: every later update is checked by the release keys it lists"
    # The state schema of what the app keeps in each user's home, as the tarball's own package names
    # it: a rollback takes only a release whose schema is the installed one's or later. Read as the
    # helper is: from the tarball unpacked whole, which is what an install leaves, and from a file
    # of the tree's own, asked in a command of its own as the helper's path is.
    # The package is read by the rule a manifest is read by, with the install script's own reader
    # (one_object, from its functions without its last line, which runs it): one JSON object, as
    # JSON is written, of a megabyte at most. jq alone reads more than JSON: a schema written +1,
    # 01, or in 20 digits that round to 1 would be signed as 1, where no reader of a manifest
    # takes it so written, and no other reader of a package reads the first two at all. And the
    # schema is a whole number from 1 and below 10^15 as it rounds, which is what is signed:
    # 999999999999999.99, compared as it is written, is below 10^15, and is written
    # 1000000000000000.
    package="surogate-desktop-$VERSION-linux-x64/resources/app/package.json"
    [ -f "$unpacked/$package" ] && [ "$(realpath "$unpacked/$package")" = "$tree/$package" ] || fail "the tarball's resources/app/package.json names no stateSchema"
    reader='. <(sed "\$d" "$1") && settings && one_object "$2" any 1048576 | jq -e ".stateSchema | select(type == \"number\" and . == floor and (floor | . >= 1 and . < 1e15)) | floor"'
    schema="$(bash -c "$reader" _ "$HERE/install.sh" "$unpacked/$package" 2>/dev/null)" \
      || fail "the tarball's resources/app/package.json names no stateSchema"
    # And the app in it is the version this release is: the app says its own version from that
    # package, and one that named another would be offered its own release as an update.
    reader='. <(sed "\$d" "$1") && settings && one_object "$2" any 1048576 | jq -e --arg version "$3" ".version == \$version" >/dev/null'
    bash -c "$reader" _ "$HERE/install.sh" "$unpacked/$package" "$VERSION" 2>/dev/null \
      || fail "the tarball's app is not version $VERSION, by its resources/app/package.json"
    # All that is read of the unpacked tarball is read by here. It is removed before the manifest is
    # written, and from its removal on no signal ends this: one would leave a manifest half
    # written, or end what has just said it wrote one.
    cleanup || fail "the unpacked tarball could not be removed from $unpacked: no manifest is written"
    # A signature that is here is of another manifest than the one written now.
    rm -f "$OUT/manifest.json.sig"
    # The tarball by its hash and its size in bytes, a number: the root helper takes no manifest
    # without either, and copies no more of a tarball than the size its manifest names.
    manifest "$VERSION" "$sha256" "$(stat -c %s "$OUT/$ARCHIVE")" "$schema" >"$OUT/manifest.json"
    echo "wrote $OUT/manifest.json"
    ;;
  sign)
    : "${DESKTOP_RELEASE_KEY:?}" "${DESKTOP_TARBALL_SHA256:?}" "${DESKTOP_TARBALL_SIZE:?}" "${DESKTOP_STATE_SCHEMA:?}"
    # The build's own words for its tarball, and the state schema that the job which read the
    # tarball says: each as its job gives it, and never said back where it is no such word.
    [[ "$DESKTOP_TARBALL_SHA256" =~ ^[0-9a-f]{64}$ ]] || fail "DESKTOP_TARBALL_SHA256 is not a sha256, as the build's job gives its tarball's"
    [[ "$DESKTOP_TARBALL_SIZE" =~ ^[1-9][0-9]{0,14}$ ]] || fail "DESKTOP_TARBALL_SIZE is not a size in bytes, as the build's job gives its tarball's"
    [[ "$DESKTOP_STATE_SCHEMA" =~ ^[1-9][0-9]{0,14}$ ]] || fail "DESKTOP_STATE_SCHEMA is not a state schema, a whole number from 1 and below 10^15, as describe reads one"
    # The release keys the installed apps and the install script trust: install.sh's list, read as
    # every install reads a helper's (keys_listed), from its functions alone: its last line, which
    # runs it, is left out. Were its last line any other, as after a blank line at its end, the
    # line that runs it would be left in: handed this call's two arguments, the script stops at
    # its own usage, and the key would be said not to be trusted. So the script's end is looked
    # at first, and said.
    [ "$(tail -n 1 "$HERE/install.sh")" = 'main "$@"' ] || fail 'install.sh does not end with the line that runs it (main "$@"): its release keys are not read'
    public="$(openssl pkey -pubout -in <(printf '%s\n' "$DESKTOP_RELEASE_KEY"))"
    keys_listed "$public" && listing=0 || listing="$?"
    [ "$listing" -ne 3 ] || fail "DESKTOP_RELEASE_KEY is not a key whose public half install.sh trusts"
    [ "$listing" -eq 0 ] || fail "$NOT_LISTED"
    # What is signed is a line written here, of four words, and no file that another wrote: the
    # tag's version, the build's own two words for its tarball, and the state schema, which is
    # all that is taken from the job that read the tarball, as a number. No tarball is opened.
    # The line is written into a folder of this signing's own, where no one else has a name for
    # it, is one that the install script beside this one takes for this release, and is signed
    # there: openssl signs a file, and no pipe, as it asks a file's size first. Only then do the
    # two have their names beside the tarball, the manifest and then its signature, each by a
    # rename: whatever stood under either name is replaced, a link too, and never written
    # through. The folder goes however this ends.
    signing="$(mktemp -d --tmpdir release-signing-XXXXXXXXXX)"
    trap 'rm -rf "$signing"' EXIT
    manifest "$VERSION" "$DESKTOP_TARBALL_SHA256" "$DESKTOP_TARBALL_SIZE" "$DESKTOP_STATE_SCHEMA" >"$signing/manifest.json"
    taken="$(release_read "$signing/manifest.json" 2>/dev/null)" || taken=
    [ "$taken" = "$VERSION $DESKTOP_TARBALL_SHA256 $DESKTOP_TARBALL_SIZE" ] || fail "the manifest of $VERSION is none that install.sh takes for it: nothing is signed"
    openssl pkeyutl -sign -inkey <(printf '%s\n' "$DESKTOP_RELEASE_KEY") -rawin -in "$signing/manifest.json" -out "$signing/manifest.json.sig"
    mv -T "$signing/manifest.json" "$OUT/manifest.json"
    mv -T "$signing/manifest.json.sig" "$OUT/manifest.json.sig"
    echo "signed $OUT/manifest.json"
    ;;
  send)
    : "${S3_ENDPOINT:?}" "${S3_BUCKET:?}" "${AWS_ACCESS_KEY_ID:?}" "${AWS_SECRET_ACCESS_KEY:?}"
    bucket="${S3_ENDPOINT%/}/$S3_BUCKET/desktop"
    # What a send keeps beside the release while it runs goes however it ends.
    trap 'rm -f "$OUT/sent" "$OUT/latest.json"' EXIT
    # What is sent is one release, asked before anything is asked of the bucket.
    # Its manifest and its signature are a pair: the signature is of that manifest, by a key that
    # the install script beside this one lists, as every install will ask; and the manifest is
    # this version's. That needs no key. Sent with another release's signature, the release
    # would be one that no install takes, under a version that is never sent again.
    [ "$(tail -n 1 "$HERE/install.sh")" = 'main "$@"' ] || fail 'install.sh does not end with the line that runs it (main "$@"): its release keys are not read'
    keys_listed || fail "$NOT_LISTED"
    pair='. <(sed "\$d" "$1") && settings && listed keys "$1" && signed_by "$2" "$2.sig" "${keys[@]}"'
    # linux-x64's is read in the same run, by the root helper's own check; another target's after it (release_read).
    [ "$DESKTOP_TARGET" = linux-x64 ] && pair="$pair"' && release="$(release_of "$2")" && [ "${release%% *}" = "$3" ]'
    [ -f "$OUT/manifest.json" ] && [ -f "$OUT/manifest.json.sig" ] && bash -c "$pair" _ "$HERE/install.sh" "$OUT/manifest.json" "$VERSION" 2>/dev/null \
      && { [ "$DESKTOP_TARGET" = linux-x64 ] || { release="$(release_read "$OUT/manifest.json" 2>/dev/null)" && [ "${release%% *}" = "$VERSION" ]; }; } \
      || fail "$OUT/manifest.json and $OUT/manifest.json.sig are not the manifest of $VERSION and its signature by a key that install.sh lists: nothing is sent"
    # And its tarball is the one that manifest names, by its hash. The manifest was signed for the
    # build's own word of that hash, where no tarball was opened, and the tarball here is this
    # job's own download of the build's artifact, which is the run's: any job of the run may put
    # another file under its name.
    named="$(jq -r '.sha256 | strings' "$OUT/manifest.json" 2>/dev/null)" || named=
    [ -n "$named" ] && [ -f "$OUT/$ARCHIVE" ] && [ "$(sha256sum <"$OUT/$ARCHIVE" | cut -d' ' -f1)" = "$named" ] \
      || fail "$OUT/$ARCHIVE is not the tarball that $OUT/manifest.json names, by its sha256: nothing is sent"
    # macOS's disk image, which no manifest names, by the signing job's own word for it, DESKTOP_DMG_SHA256: the one here
    # is this job's download of that job's artifact, under whose name any job of the run may put another file.
    if [ -n "$IMAGE" ]; then
      : "${DESKTOP_DMG_SHA256:?}"
      [[ "$DESKTOP_DMG_SHA256" =~ ^[0-9a-f]{64}$ ]] || fail "DESKTOP_DMG_SHA256 is not a sha256, as the signing job gives its disk image's"
      [ -f "$OUT/$IMAGE" ] && [ "$(sha256sum <"$OUT/$IMAGE" | cut -d' ' -f1)" = "$DESKTOP_DMG_SHA256" ] \
        || fail "$OUT/$IMAGE is not the disk image the signing job made, by its sha256: nothing is sent"
    fi
    # A request to the bucket, signed; its HTTP status on stdout. One that cannot connect in half a
    # minute, or that stalls for a minute, stops, and is said: the job's time limit would otherwise
    # be what ends it. The secret reaches curl through a pipe from printf, a builtin, never its
    # command line (R2's are hex, so nothing in it needs quoting for curl's config).
    s3() {
      curl -q -sS --connect-timeout 30 --speed-limit 1024 --speed-time 60 \
        -K <(printf 'user = "%s:%s"\n' "$AWS_ACCESS_KEY_ID" "$AWS_SECRET_ACCESS_KEY") --aws-sigv4 "aws:amz:auto:s3" -w '%{http_code}' "$@"
    }
    # $1 sent as $2, of type $3 and served with cache control $4 when it is given, with its sha256 as
    # the payload's hash, then read back: an S3 need not check a body against its hash. What
    # follows $4 is curl's own, for both requests. The bucket's answer to each goes to a file:
    # Ubuntu 24.04's curl (8.5) ends 23, and tries nothing again, where the answer it would write
    # again goes to /dev/null.
    put() {
      local status
      status="$(s3 "${@:5}" -o "$OUT/sent" -T "$1" -H "x-amz-content-sha256: $(sha256sum <"$1" | cut -d' ' -f1)" -H "content-type: $3" ${4:+-H "cache-control: $4"} "$bucket/$2")" \
        || fail "sending desktop/$2 stopped: curl exit $?"
      [ "$status" = 200 ] || fail "sending desktop/$2 got $status"
      status="$(s3 "${@:5}" -o "$OUT/sent" "$bucket/$2")" || fail "reading desktop/$2 back stopped: curl exit $?"
      [ "$status" = 200 ] && cmp -s "$1" "$OUT/sent" || fail "the bucket's desktop/$2 is not what was sent"
      rm -f "$OUT/sent"
    }
    # Whether this release is on the bucket already, by its own manifest, which is sent once its
    # tarball and its signature are there. A bucket whose token may not list answers 403 for an
    # object that is not there, as R2 does, where another answers 404: both are "not there". A
    # token that could not read at all is found at the first object sent, which is read back.
    status="$(s3 -o "$OUT/sent" "$bucket/$RELEASE/manifest.json")" || fail "looking for desktop/$RELEASE/manifest.json stopped: curl exit $?"
    case "$status" in
      404 | 403) there= ;;
      200)
        cmp -s "$OUT/manifest.json" "$OUT/sent" || fail "desktop/$RELEASE is published already, with another manifest, and is not sent again"
        there=1
        ;;
      *) fail "looking for desktop/$RELEASE/manifest.json got $status" ;;
    esac
    rm -f "$OUT/sent"
    # The newest release the bucket names, which an older one does not replace.
    status="$(s3 -o "$OUT/latest.json" "$bucket/$LATEST")" || fail "looking for desktop/$LATEST stopped: curl exit $?"
    case "$status" in
      200) newest="$(jq -r .version "$OUT/latest.json")" ;;
      404 | 403) newest= ;;
      *) fail "looking for desktop/$LATEST got $status" ;;
    esac
    rm -f "$OUT/latest.json"
    # A release that is there, and older than the one latest.json names, is whole: no object of
    # a release is sent a second time. So is one that latest.json names, once the bucket's last
    # object (its install script, or Windows's MSI or macOS's disk image that stays) is this one's, for
    # a target that has one. Else a send was cut short after the release's own were there, and this
    # one ends it.
    if [ -n "$there" ] && [ -n "$newest" ]; then
      if dpkg --compare-versions "$VERSION" lt "$newest"; then fail "desktop/$RELEASE is published already, and is not sent again"; fi
      if [ "$VERSION" = "$newest" ] && [ -z "$LAST" ]; then fail "desktop/$RELEASE is published already, and is not sent again"; fi
      if [ "$VERSION" = "$newest" ]; then
        status="$(s3 -o "$OUT/sent" "$bucket/$LAST")" || fail "looking for desktop/$LAST stopped: curl exit $?"
        case "$status" in
          200) ! cmp -s "$LAST_FROM" "$OUT/sent" || fail "desktop/$RELEASE is published already, and is not sent again" ;;
          404 | 403) ;;
          *) fail "looking for desktop/$LAST got $status" ;;
        esac
        rm -f "$OUT/sent"
      fi
    fi
    # The order, by who reads what first. Nothing that a reader reads first is sent before what
    # it then asks for, and what a send that is cut short leaves is said at each step.
    # 1. The release's own three, under its version, where nothing names them yet: the tarball,
    #    the signature of its manifest, and last its manifest, the mark that the three are there.
    #    None is ever sent again once that mark is: an install and an app read the signature of
    #    the newest release from here, and --version reads all three. Cut before the mark, the
    #    next send sends the three again. macOS's disk image is its release's own too, sent after its
    #    tarball, and before the mark.
    if [ -z "$there" ]; then
      put "$OUT/$ARCHIVE" "$RELEASE/$ARCHIVE" "$ARCHIVE_TYPE"
      [ -z "$IMAGE" ] || put "$OUT/$IMAGE" "$RELEASE/$IMAGE" application/x-apple-diskimage
      put "$OUT/manifest.json.sig" "$RELEASE/manifest.json.sig" application/octet-stream
      put "$OUT/manifest.json" "$RELEASE/manifest.json" application/json
    fi
    if [ -n "$newest" ] && dpkg --compare-versions "$VERSION" lt "$newest"; then
      published="published desktop/$RELEASE; desktop/$LATEST stays $newest"
    else
      # 2. latest.json: the one object that says which release is the newest, and the one that
      #    every app and every install reads first. It has no signature of its own: its
      #    release's is asked for, and has been there since step 1. So no reader finds a
      #    manifest beside another's signature at any moment, and a send cut before this shows
      #    nothing to any computer. Nothing is written to latest.json.sig, which no reader asks
      #    for. Tried again when the bucket or the way to it fails for a moment. What changes
      #    from release to release is never a cache's.
      # 3. The install script, after the release it will check for a first install. Cut before
      #    it, the bucket's script is the release before's, which lists the key that signs this
      #    one: a key signs only once a release that lists it is out. Sent before latest.json,
      #    a script that has dropped the key before's would refuse the release before, and
      #    every first install would end there until the send was run again. The next send of
      #    this release finds the script owed, and sends it. Windows's MSI and macOS's disk image
      #    go in the script's place, under the name the website links to, after latest.json names
      #    their release, and are owed the same way.
      [ "$VERSION" = "$newest" ] || put "$OUT/manifest.json" "$LATEST" application/json no-cache --retry 3
      [ -z "$LAST" ] || put "$LAST_FROM" "$LAST" "$LAST_TYPE" no-cache
      published="published desktop/$RELEASE as desktop/$LATEST"
    fi
    echo "$published"
    ;;
  *)
    echo "usage: publish.sh describe|sign|send <x.y.z> <out>" >&2
    exit 2
    ;;
esac
