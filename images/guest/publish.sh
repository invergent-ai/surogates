#!/usr/bin/env bash
# The guest image in the release's bucket, under desktop/vm/<key>/ (images/guest/inputs.sh
# names the key). The bucket is storage, never the truth: the release that publishes a key
# attaches its manifest to itself on GitHub, as desktop-vm-<key>.json, and every later
# release takes the manifest from there. Its tarball carries that one, and the bucket's
# manifest and files are checked against it, each file by both its hashes, before any
# tarball names them. An image whose manifest is there is never built or sent again.
#
# Usage:
#   images/guest/publish.sh fetch <out>   # prints "published", with the release's manifest in
#                                         # <out>/manifest.json and the bucket checked against
#                                         # it, or "missing"
#   images/guest/publish.sh send <out>    # after images/guest/build.sh <out>: sends
#                                         # rootfs.img.zst and vmlinuz.zst, then manifest.json,
#                                         # so a manifest that is there names files that are
#                                         # there; then checks what the bucket holds
# Environment: S3_ENDPOINT (R2's https://<account>.r2.cloudflarestorage.com), S3_BUCKET,
# AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY; for fetch, GITHUB_REPOSITORY and GH_TOKEN,
# with which gh lists the repository's releases. A key in the bucket that no release carries
# yet is looked for again PUBLISH_POLLS times, PUBLISH_POLL_S seconds apart (15 and 60): the
# release run that sent it attaches its manifest only once its release exists.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VERB="${1:-}"
OUT="${2:?usage: publish.sh fetch|send <out>}"
: "${S3_ENDPOINT:?}" "${S3_BUCKET:?}" "${AWS_ACCESS_KEY_ID:?}" "${AWS_SECRET_ACCESS_KEY:?}"

key="$("$REPO_ROOT/images/guest/inputs.sh")"
prefix="${S3_ENDPOINT%/}/$S3_BUCKET/desktop/vm/$key"
fail() {
  echo "publish.sh: $*" >&2
  exit 1
}
# A request to the bucket, signed; its HTTP status on stdout.
s3() {
  curl -q -sS --aws-sigv4 "aws:amz:auto:s3" --user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY" -w '%{http_code}' "$@"
}
# The key's manifest's HTTP status, 200 or 404, as curl's options ask for it: into a file, or
# its headers alone (-I). Any other status stops the script, through the assignment it is called in.
manifest() {
  local status
  status="$(s3 "$@" "$prefix/manifest.json")"
  case "$status" in
    200 | 404) echo "$status" ;;
    *) fail "looking for desktop/vm/$key/manifest.json got $status" ;;
  esac
}
# An object sent with its sha256 as the payload's hash.
put() {
  local status
  status="$(s3 -o /dev/null -T "$OUT/$1" -H "x-amz-content-sha256: $(sha256sum "$OUT/$1" | cut -d' ' -f1)" -H "content-type: $2" "$prefix/$1")"
  [ "$status" = 200 ] || fail "sending $1 got $status"
}
# The key of the manifest at $1, or nothing.
keyOf() {
  jq -r '.key // empty' "$1" 2>/dev/null || true
}
# The bucket against <out>/manifest.json: its manifest byte for byte, and each file read
# back, its size and sha256 as downloaded and unpacked.
check() {
  local status files name size sha256 download downloadSize downloadSha256
  # The image's two files, or nothing of the bucket's would be checked.
  jq -e '[.files[]?.download] | sort == ["rootfs.img.zst", "vmlinuz.zst"]' "$OUT/manifest.json" > /dev/null \
    || fail "desktop/vm/$key's manifest does not name rootfs.img.zst and vmlinuz.zst"
  files="$(jq -r '.files[] | "\(.name) \(.size) \(.sha256) \(.download) \(.downloadSize) \(.downloadSha256)"' "$OUT/manifest.json")"
  status="$(manifest -o "$OUT/bucket.json")"
  if [ "$status" != 200 ] || ! cmp -s "$OUT/bucket.json" "$OUT/manifest.json"; then
    rm -f "$OUT/bucket.json"
    fail "the bucket's desktop/vm/$key/manifest.json is not the release's"
  fi
  rm -f "$OUT/bucket.json"
  while read -r name size sha256 download downloadSize downloadSha256; do
    status="$(s3 -o "$OUT/bucket.zst" "$prefix/$download")"
    [ "$status" = 200 ] || fail "fetching desktop/vm/$key/$download got $status"
    if [ "$(stat -c %s "$OUT/bucket.zst") $(sha256sum < "$OUT/bucket.zst" | cut -d' ' -f1)" != "$downloadSize $downloadSha256" ] \
      || [ "$(zstd -q -dc "$OUT/bucket.zst" | wc -c) $(zstd -q -dc "$OUT/bucket.zst" | sha256sum | cut -d' ' -f1)" != "$size $sha256" ]; then
      rm -f "$OUT/bucket.zst"
      fail "the bucket's desktop/vm/$key/$download is not the release's"
    fi
    rm -f "$OUT/bucket.zst"
  done <<< "$files"
}

case "$VERB" in
  fetch)
    mkdir -p "$OUT"
    : "${GITHUB_REPOSITORY:?}"
    status="$(manifest -o /dev/null -I)"
    # The manifest the release that published the key attached to itself, from the API's list. A
    # key in the bucket may be one whose release run has yet to attach it, as when a second
    # release is pushed while the first runs: waited for, at most for about a release's run.
    lookup() {
      gh api --paginate "repos/$GITHUB_REPOSITORY/releases" --jq ".[].assets[] | select(.name == \"desktop-vm-$key.json\") | .url" | sed -n 1p
    }
    asset="$(lookup)"
    for ((poll = 0; poll < ${PUBLISH_POLLS:-15}; poll++)); do
      [ -z "$asset" ] && [ "$status" = 200 ] || break
      sleep "${PUBLISH_POLL_S:-60}"
      asset="$(lookup)"
    done
    if [ -z "$asset" ]; then
      [ "$status" = 404 ] \
        || fail "desktop/vm/$key is in the bucket, but no release of ours carries its manifest (desktop-vm-$key.json): re-run this job once the release run that sent it has attached it, or attach that run's desktop-vm-manifest artifact to its release as desktop-vm-$key.json"
      echo missing
      exit 0
    fi
    gh api -H "Accept: application/octet-stream" "$asset" > "$OUT/manifest.json"
    [ "$(keyOf "$OUT/manifest.json")" = "$key" ] || fail "desktop-vm-$key.json is not the manifest of desktop/vm/$key"
    check
    echo published
    ;;
  send)
    [ "$(keyOf "$OUT/manifest.json")" = "$key" ] || fail "$OUT/manifest.json is not the build of desktop/vm/$key"
    status="$(manifest -o /dev/null -I)"
    [ "$status" = 404 ] || fail "desktop/vm/$key is published already, and is not sent again"
    put rootfs.img.zst application/zstd
    put vmlinuz.zst application/zstd
    put manifest.json application/json
    # Read back: an S3 need not check a body against its payload hash, and SeaweedFS does not.
    check
    echo "published desktop/vm/$key"
    ;;
  *)
    echo "usage: publish.sh fetch|send <out>" >&2
    exit 2
    ;;
esac
