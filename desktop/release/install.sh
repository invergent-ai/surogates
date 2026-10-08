#!/usr/bin/env bash
# Surogate Desktop's install script (spec, Section 9), and each version's
# bin/surogate-apply-update, the root helper that applies a verified release: the current
# version's copy at /opt/surogate/bin/surogate-apply-update is the one pkexec runs.
#
#   surogate-apply-update --apply <manifest> <signature> <tarball>      as root: apply a downloaded release
#
# Every line is in a function, and main runs from the script's last line: a download cut
# short runs nothing.

settings() {
  ROOT=/opt/surogate
  CHANNEL=stable
  # The release keys' public halves: a release's manifest is signed by the private half of one of
  # them (Ed25519). A rotation lists the old key and the new for one release, which the old signs.
  RELEASE_KEYS=(
    '-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEA9SZBZHM7o/wDBWPfbhPMxucA2139J9j+nFHJYNwPA1w=
-----END PUBLIC KEY-----'
  )
}

fail() {
  echo "Surogate Desktop: $*" >&2
  exit 1
}

say() {
  echo "Surogate Desktop: $*"
}

# Ubuntu 24.04 LTS or a later LTS release, on x64. Nothing is changed before this passes.
supported() {
  local id version version_id
  id="$(. /etc/os-release && echo "${ID:-}")"
  version="$(. /etc/os-release && echo "${VERSION:-}")"
  version_id="$(. /etc/os-release && echo "${VERSION_ID:-}")"
  [ "$id" = ubuntu ] && [[ "$version" == *LTS* ]] && dpkg --compare-versions "$version_id" ge 24.04 && [ "$(uname -m)" = x86_64 ] \
    || fail "Surogate Desktop supports Ubuntu 24.04 LTS or a later LTS release (x64)"
}

# Whether manifest $1 is signed, in signature $2, by the private half of one of the release keys.
signed() {
  local key
  for key in "${RELEASE_KEYS[@]}"; do
    openssl pkeyutl -verify -pubin -inkey <(printf '%s\n' "$key") -rawin -in "$1" -sigfile "$2" >/dev/null 2>&1 && return 0
  done
  return 1
}

# A signed manifest's fields: a release of this channel for this platform, its tarball where every
# release's is, and its hash, each whole (jq's $ also matches before a last newline). Printed as
# "<version> <sha256>".
release_of() {
  jq -er --arg channel "$CHANNEL" '
    select((.version | type == "string" and test("\\A[0-9]+\\.[0-9]+\\.[0-9]+\\z"))
      and .channel == $channel and .platform == "linux" and .arch == "x64"
      and .url == "releases/\(.version)/surogate-desktop-\(.version)-linux-x64.tar.gz"
      and (.sha256 | type == "string" and test("\\A[0-9a-f]{64}\\z")))
    | "\(.version) \(.sha256)"' "$1" 2>/dev/null
}

# The version /opt/surogate/current names, or nothing.
installed_version() {
  local target
  target="$(readlink "$ROOT/current" 2>/dev/null)" || return 0
  basename "$target"
}

# Whether a process runs from version folder $1: its program is in it.
in_use() {
  local exe
  for exe in /proc/[0-9]*/exe; do
    [[ "$(readlink "$exe" 2>/dev/null)" == "$1"/* ]] && return 0
  done
  return 1
}

# Applies a release as root: its manifest $1, signature $2 and tarball $3, which the user who
# downloaded them can still change, so each is read once, into root's staging, and only the copies
# are checked and used. The tree is extracted in staging, refused when anything in it is not a
# plain file, folder or link inside it, moved into versions/<version> with its own copy of bwrap,
# and /opt/surogate/current is switched to it by one rename; its helper is then the one pkexec
# runs. An older version than the installed one is refused. The previous version is kept, and
# older ones not running are removed.
apply() {
  local manifest="$1" signature="$2" tarball="$3" file
  # A device such as /dev/zero would never end. A link is refused as each is copied, below.
  for file in "$manifest" "$signature" "$tarball"; do
    [ -f "$file" ] || fail "$file is not a downloaded release's file"
  done
  mkdir -p "$ROOT/versions" "$ROOT/staging"
  chmod 0755 "$ROOT" "$ROOT/versions"
  chmod 0700 "$ROOT/staging"
  # Room for the tarball's copy and the tree it unpacks to, which is about two and a half times its size.
  local need room
  need=$(( $(stat -c %s -- "$tarball") * 4 / 1024 ))
  room="$(df --output=avail -k "$ROOT" | tail -n 1)"
  [ "$room" -ge "$need" ] || fail "$ROOT needs $(( need / 1024 )) MB free to apply this release, and has $(( room / 1024 )) MB"
  # One at a time; what an apply that stopped part way left in staging goes.
  exec 9<"$ROOT"
  flock 9
  find "$ROOT/staging" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  local work
  work="$(mktemp -d "$ROOT/staging/apply.XXXXXX")"
  # Each read once, never through a link and never waiting on a pipe, whatever it has become since
  # it was named: a pipe gives an empty copy at once.
  dd if="$manifest" of="$work/manifest.json" iflag=nofollow,nonblock bs=1M status=none 2>/dev/null || fail "$manifest is not a downloaded release's file"
  dd if="$signature" of="$work/manifest.json.sig" iflag=nofollow,nonblock bs=1M status=none 2>/dev/null || fail "$signature is not a downloaded release's file"
  dd if="$tarball" of="$work/release.tar.gz" iflag=nofollow,nonblock bs=1M status=none 2>/dev/null || fail "$tarball is not a downloaded release's file"

  signed "$work/manifest.json" "$work/manifest.json.sig" || fail "the release's manifest is not signed by Surogate's release key"
  local release version sha256
  release="$(release_of "$work/manifest.json")" || fail "the release's manifest is not a release of Surogate Desktop for this computer"
  read -r version sha256 <<<"$release"
  [ "$(sha256sum <"$work/release.tar.gz" | cut -d' ' -f1)" = "$sha256" ] || fail "the downloaded release is not the one its manifest names"
  local previous
  previous="$(installed_version)"
  if [ -n "$previous" ] && dpkg --compare-versions "$version" lt "$previous"; then
    fail "$version is older than the installed $previous"
  fi

  local folder="$ROOT/versions/$version" name="surogate-desktop-$version-linux-x64"
  if ! cmp -s "$work/manifest.json" "$folder/release.json"; then
    mkdir "$work/tree"
    tar -xzf "$work/release.tar.gz" -C "$work/tree" --no-same-owner --no-same-permissions 2>/dev/null \
      || fail "the release's archive could not be unpacked"
    [ "$(ls -A "$work/tree")" = "$name" ] && [ -d "$work/tree/$name" ] && [ ! -L "$work/tree/$name" ] \
      || fail "the release's archive holds more than $name/"
    local top="$work/tree/$name" link
    # tar keeps no name with .. and nothing outside the tree, and no set-id bit (--no-same-permissions);
    # a set-id file is refused here too.
    [ -z "$(find "$top" \( -type b -o -type c -o -type p -o -type s -o -perm /6000 -o \( -type f -links +1 \) \) -print -quit)" ] \
      || fail "the release's archive holds a special file, a set-id file or a hard link"
    while IFS= read -r -d '' link; do
      [[ "$(realpath -m "$link")" == "$top"/* ]] || fail "the release's archive links outside itself: ${link#"$top"/}"
    done < <(find "$top" -type l -print0)
    [ -x "$top/surogate" ] && [ -x "$top/bin/surogate-apply-update" ] || fail "the release's archive is not Surogate Desktop"
    chmod -R go-w "$top"
    # Last: a version folder with its manifest is whole. A release.json of the archive's is replaced,
    # never written through.
    cp --remove-destination "$work/manifest.json" "$top/release.json"
    [ ! -e "$folder" ] || mv -T "$folder" "$work/replaced"
    mv -T "$top" "$folder"
  fi
  # The system's bwrap, which the app gives srt: a copy here takes the app's AppArmor profile,
  # not one Ubuntu attaches to /usr/bin/bwrap. Made again at each apply, as apt may have updated it.
  [ -x /usr/bin/bwrap ] || fail "bubblewrap is missing: run Surogate Desktop's install script again"
  install -m 0755 /usr/bin/bwrap "$work/bwrap"
  mv -T "$work/bwrap" "$folder/bin/bwrap"
  ln -s "$folder" "$work/current"
  mv -T "$work/current" "$ROOT/current"
  # The helper pkexec runs, at a path with no link in it: polkit 127 (Ubuntu 26.04) matches an
  # action's exec.path against the program's resolved path, polkit 124 (24.04) against the path given.
  mkdir -p "$ROOT/bin"
  install -m 0755 "$folder/bin/surogate-apply-update" "$work/helper"
  mv -T "$work/helper" "$ROOT/bin/surogate-apply-update"

  # Kept: this version and the one before it. Removed: the rest, once nothing runs from them.
  local kept
  for kept in "$ROOT"/versions/*; do
    [ "$kept" = "$folder" ] || [ "$kept" = "$ROOT/versions/$previous" ] || in_use "$kept" || rm -rf -- "$kept"
  done
  rm -rf -- "$work"
  say "$version is installed"
}

main() {
  set -euo pipefail
  umask 022
  settings
  case "${1:-}" in
    --apply)
      [ "$#" -eq 4 ] || fail "usage: surogate-apply-update --apply <manifest> <signature> <tarball>"
      [ "$EUID" -eq 0 ] || fail "applying a release needs administrator rights"
      supported
      apply "$2" "$3" "$4"
      ;;
    *)
      fail "usage: surogate-apply-update --apply <manifest> <signature> <tarball>"
      ;;
  esac
}

main "$@"
