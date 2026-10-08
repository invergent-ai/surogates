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
#                                             # one of the keys whose public halves install.sh trusts
#   release/publish.sh send <version> <out>   # the release, then the install script and latest.json
#                                             # with its signature, each read back, then the release's
#                                             # own manifest; never a release again, and latest.json
#                                             # only for the newest version
# Environment for send: S3_ENDPOINT (R2's https://<account>.r2.cloudflarestorage.com), S3_BUCKET,
# AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY.
set -euo pipefail

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
    : "${DESKTOP_RELEASE_KEY:?}"
    [ -f "$OUT/$TARBALL" ] || fail "$OUT/$TARBALL is not there: run scripts/package.sh first"
    # The release keys the installed apps and the install script trust: install.sh's RELEASE_KEYS,
    # read as install.sh sets them, from its functions alone (its last line runs it).
    public="$(openssl pkey -pubout -in <(printf '%s\n' "$DESKTOP_RELEASE_KEY"))"
    bash -c '. <(sed "\$d" "$1") && settings && for key in "${RELEASE_KEYS[@]}"; do [ "$key" != "$2" ] || exit 0; done; exit 1' _ "$HERE/install.sh" "$public" \
      || fail "DESKTOP_RELEASE_KEY is not a key whose public half install.sh trusts"
    # The tarball by its hash and its size in bytes, a number: the root helper takes no manifest
    # without either, and copies no more of a tarball than the size its manifest names.
    jq -cn --arg version "$VERSION" --arg sha256 "$(sha256sum <"$OUT/$TARBALL" | cut -d' ' -f1)" --argjson size "$(stat -c %s "$OUT/$TARBALL")" \
      '{version: $version, channel: "stable", platform: "linux", arch: "x64",
        url: "releases/\($version)/surogate-desktop-\($version)-linux-x64.tar.gz", sha256: $sha256, size: $size}' >"$OUT/manifest.json"
    openssl pkeyutl -sign -inkey <(printf '%s\n' "$DESKTOP_RELEASE_KEY") -rawin -in "$OUT/manifest.json" -out "$OUT/manifest.json.sig"
    echo "signed $OUT/manifest.json"
    ;;
  send)
    : "${S3_ENDPOINT:?}" "${S3_BUCKET:?}" "${AWS_ACCESS_KEY_ID:?}" "${AWS_SECRET_ACCESS_KEY:?}"
    bucket="${S3_ENDPOINT%/}/$S3_BUCKET/desktop"
    # A request to the bucket, signed; its HTTP status on stdout.
    s3() {
      curl -q -sS --aws-sigv4 "aws:amz:auto:s3" --user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY" -w '%{http_code}' "$@"
    }
    # $1 sent as $2, of type $3 and served with cache control $4 when it is given, with its sha256 as
    # the payload's hash, then read back: an S3 need not check a body against its hash.
    put() {
      local status
      status="$(s3 -o /dev/null -T "$1" -H "x-amz-content-sha256: $(sha256sum <"$1" | cut -d' ' -f1)" -H "content-type: $3" ${4:+-H "cache-control: $4"} "$bucket/$2")"
      [ "$status" = 200 ] || fail "sending desktop/$2 got $status"
      status="$(s3 -o "$OUT/sent" "$bucket/$2")"
      [ "$status" = 200 ] && cmp -s "$1" "$OUT/sent" || fail "the bucket's desktop/$2 is not what was sent"
      rm -f "$OUT/sent"
    }
    status="$(s3 -o /dev/null -I "$bucket/releases/$VERSION/manifest.json")"
    case "$status" in
      404) ;;
      200) fail "desktop/releases/$VERSION is published already, and is not sent again" ;;
      *) fail "looking for desktop/releases/$VERSION/manifest.json got $status" ;;
    esac
    # The newest release the bucket names, which an older one does not replace.
    status="$(s3 -o "$OUT/latest.json" "$bucket/latest.json")"
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
      # does not verify, and tries again later.
      put "$OUT/manifest.json" latest.json application/json no-cache
      put "$OUT/manifest.json.sig" latest.json.sig application/octet-stream no-cache
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
