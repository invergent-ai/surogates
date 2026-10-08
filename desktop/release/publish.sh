#!/usr/bin/env bash
# Surogate Desktop's release on the release bucket (spec, Section 9), served at <base>/desktop/:
#
#   desktop/install.sh                                    the newest install script
#   desktop/latest.json, latest.json.sig                  the newest release's signed manifest
#   desktop/releases/<version>/manifest.json, .sig        each release's own, kept
#   desktop/releases/<version>/surogate-desktop-<version>-linux-x64.tar.gz
#
# Usage, after scripts/package.sh <version> <vm manifest> <out>:
#   release/publish.sh sign <version> <out>   # <out>/manifest.json and its Ed25519 signature,
#                                             # with DESKTOP_RELEASE_KEY (its PEM), which must be
#                                             # one of the keys whose public halves install.sh trusts,
#                                             # of the tarball whose sha256 is DESKTOP_TARBALL_SHA256
#                                             # (the build's own word for what it made), and whose
#                                             # root helper is the install.sh beside this script
#   release/publish.sh send <version> <out>   # the release, then the install script and latest.json
#                                             # with its signature, each read back, then the release's
#                                             # own manifest; never a release again, and latest.json
#                                             # only for the newest version
# Environment for send: S3_ENDPOINT (R2's https://<account>.r2.cloudflarestorage.com), S3_BUCKET,
# AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY.
set -euo pipefail
# The release key is this shell's own from here on, and in the environment of no program it
# starts: sign hands it to openssl alone, through a pipe. All that reads the build's tarball, or
# what it unpacks to, would otherwise have it, and the build holds no key.
export -n DESKTOP_RELEASE_KEY
# All it reads, it reads in no locale and no language of its caller's: in most locales, more than
# ten characters are digits, and a version's are the ten.
export LC_ALL=C LANG=C
unset LANGUAGE

HERE="$(cd "$(dirname "$0")" && pwd)"
VERB="${1:-}"
VERSION="${2:-}"
OUT="${3:-}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] && [ -d "$OUT" ] || { echo "usage: publish.sh sign|send <x.y.z> <out>" >&2; exit 2; }
TARBALL="surogate-desktop-$VERSION-linux-x64.tar.gz"
fail() {
  echo "publish.sh: $*" >&2
  exit 1
}

case "$VERB" in
  sign)
    : "${DESKTOP_RELEASE_KEY:?}" "${DESKTOP_TARBALL_SHA256:?}"
    [ -f "$OUT/$TARBALL" ] || fail "$OUT/$TARBALL is not there: run scripts/package.sh first"
    # The tarball is the one the build made, by the hash the build's job gave for it: an artifact
    # is its run's, and any job of the run may put another file under its name. What is no sha256 is
    # never said back.
    [[ "$DESKTOP_TARBALL_SHA256" =~ ^[0-9a-f]{64}$ ]] || fail "DESKTOP_TARBALL_SHA256 is not a sha256, as the build's job gives its tarball's"
    sha256="$(sha256sum <"$OUT/$TARBALL" | cut -d' ' -f1)"
    [ "$sha256" = "$DESKTOP_TARBALL_SHA256" ] || fail "$OUT/$TARBALL is not the tarball the build made: its sha256 is $sha256, and the build's $DESKTOP_TARBALL_SHA256"
    # The release keys the installed apps and the install script trust: install.sh's RELEASE_KEYS,
    # read as install.sh sets them, from its functions alone: its last line, which runs it, is left
    # out. Were its last line any other, as after a blank line at its end, the line that runs it
    # would be left in: handed this call's two arguments, the script stops at its own usage, and
    # the key would be said not to be trusted. So the script's end is looked at first, and said.
    [ "$(tail -n 1 "$HERE/install.sh")" = 'main "$@"' ] || fail 'install.sh does not end with the line that runs it (main "$@"): its release keys are not read'
    public="$(openssl pkey -pubout -in <(printf '%s\n' "$DESKTOP_RELEASE_KEY"))"
    bash -c '. <(sed "\$d" "$1") && settings && for key in "${RELEASE_KEYS[@]}"; do [ "$key" != "$2" ] || exit 0; done; exit 1' _ "$HERE/install.sh" "$public" \
      || fail "DESKTOP_RELEASE_KEY is not a key whose public half install.sh trusts"
    # The tarball's root helper is the install script beside this one, byte for byte: installed,
    # it is what pkexec runs as root at the next update, and its release keys are the ones every
    # later update is checked against. The build holds no key, and a helper of its own would need
    # none. The helper is read from the tarball unpacked whole, as the helper that installs it
    # unpacks it: a member under the helper's own name may be replaced by a later one, or through
    # a link to its folder. The tarball is the build's, and is unpacked, as it is read, without
    # the key: no program this script starts has it.
    # What is unpacked is gone however the signing ends, whatever the modes of its folders, which
    # are the build's too. A signal ends the signing once the command it runs has ended, as the
    # script would end by itself: removed beside a tar that still writes, the folder would keep
    # what tar writes after. No signal comes between the folder's making and its name being kept,
    # nor stops mktemp then; and none stops the removal, where a second one would end its rm.
    trap '' HUP INT PIPE TERM
    unpacked="$(mktemp -d --tmpdir release-unpacked-XXXXXXXXXX)"
    cleanup() {
      trap '' HUP INT PIPE TERM
      chmod -R u+rwX "$unpacked" 2>/dev/null || :
      rm -rf "$unpacked"
    }
    # A removal that fails as the signing ends leaves its status as it was.
    trap 'cleanup || :' EXIT
    trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 141' PIPE; trap 'exit 143' TERM
    tar -xzf "$OUT/$TARBALL" -C "$unpacked" --no-same-owner --no-same-permissions 2>/dev/null || fail "$OUT/$TARBALL could not be unpacked"
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
    # of the tree's own, asked in a command of its own as the helper's path is. The package is one
    # JSON document: of two, each would name a schema. And the schema is a whole number from 1 and
    # below 10^15 as it rounds, which is what is signed: 999999999999999.99, compared as it is
    # written, is below 10^15, and is written 1000000000000000.
    package="surogate-desktop-$VERSION-linux-x64/resources/app/package.json"
    [ -f "$unpacked/$package" ] && [ "$(realpath "$unpacked/$package")" = "$tree/$package" ] || fail "the tarball's resources/app/package.json names no stateSchema"
    schema="$(jq -es 'select(length == 1) | .[0].stateSchema | select(type == "number" and . == floor and (floor | . >= 1 and . < 1e15)) | floor' "$unpacked/$package" 2>/dev/null)" \
      || fail "the tarball's resources/app/package.json names no stateSchema"
    # All that is read of the unpacked tarball is read by here. It is removed before anything is
    # signed, and from its removal on no signal ends the signing: one would leave a manifest
    # without its signature, or end a signing that has just said it signed.
    cleanup || fail "the unpacked tarball could not be removed from $unpacked: nothing is signed"
    # The tarball by its hash and its size in bytes, a number: the root helper takes no manifest
    # without either, and copies no more of a tarball than the size its manifest names.
    jq -cn --arg version "$VERSION" --arg sha256 "$sha256" --argjson size "$(stat -c %s "$OUT/$TARBALL")" --argjson schema "$schema" \
      '{version: $version, channel: "stable", platform: "linux", arch: "x64",
        url: "releases/\($version)/surogate-desktop-\($version)-linux-x64.tar.gz", sha256: $sha256, size: $size, stateSchema: $schema}' >"$OUT/manifest.json"
    openssl pkeyutl -sign -inkey <(printf '%s\n' "$DESKTOP_RELEASE_KEY") -rawin -in "$OUT/manifest.json" -out "$OUT/manifest.json.sig"
    echo "signed $OUT/manifest.json"
    ;;
  send)
    : "${S3_ENDPOINT:?}" "${S3_BUCKET:?}" "${AWS_ACCESS_KEY_ID:?}" "${AWS_SECRET_ACCESS_KEY:?}"
    bucket="${S3_ENDPOINT%/}/$S3_BUCKET/desktop"
    # What a send keeps beside the release while it runs goes however it ends.
    trap 'rm -f "$OUT/sent" "$OUT/latest.json"' EXIT
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
    status="$(s3 -o /dev/null -I "$bucket/releases/$VERSION/manifest.json")" || fail "looking for desktop/releases/$VERSION/manifest.json stopped: curl exit $?"
    case "$status" in
      404) ;;
      200) fail "desktop/releases/$VERSION is published already, and is not sent again" ;;
      *) fail "looking for desktop/releases/$VERSION/manifest.json got $status" ;;
    esac
    # The newest release the bucket names, which an older one does not replace.
    status="$(s3 -o "$OUT/latest.json" "$bucket/latest.json")" || fail "looking for desktop/latest.json stopped: curl exit $?"
    case "$status" in
      200) newest="$(jq -r .version "$OUT/latest.json")" ;;
      404) newest= ;;
      *) fail "looking for desktop/latest.json got $status" ;;
    esac
    rm -f "$OUT/latest.json"
    put "$OUT/$TARBALL" "releases/$VERSION/$TARBALL" application/gzip
    put "$OUT/manifest.json.sig" "releases/$VERSION/manifest.json.sig" application/octet-stream
    if [ -n "$newest" ] && dpkg --compare-versions "$VERSION" lt "$newest"; then
      published="published desktop/releases/$VERSION; desktop/latest.json stays $newest"
    else
      # What changes from release to release is never a cache's: one could pair a manifest with
      # another's signature, or serve an older install script.
      put "$HERE/install.sh" install.sh text/x-shellscript no-cache
      # Its manifest, then its signature: a reader between the two finds a manifest whose signature
      # does not verify, and tries again later. A send that stopped between the two would leave
      # them so until it is run again: each of these two, which are small, is tried again when
      # the bucket or the way to it fails for a moment.
      put "$OUT/manifest.json" latest.json application/json no-cache --retry 3
      put "$OUT/manifest.json.sig" latest.json.sig application/octet-stream no-cache --retry 3
      published="published desktop/releases/$VERSION as desktop/latest.json"
    fi
    # Last, the release's own manifest: the mark that it is published, so a send cut short is sent
    # again whole.
    put "$OUT/manifest.json" "releases/$VERSION/manifest.json" application/json
    echo "$published"
    ;;
  *)
    echo "usage: publish.sh sign|send <x.y.z> <out>" >&2
    exit 2
    ;;
esac
