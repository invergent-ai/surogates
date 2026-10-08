#!/usr/bin/env bash
# Surogate Desktop's install script (spec, Section 9), and each version's
# bin/surogate-apply-update, the root helper that applies a verified release: the current
# version's copy at /opt/surogate/bin/surogate-apply-update is the one pkexec runs.
#
#   curl -fsSL https://surogate.ai/desktop/install.sh | bash                      install, update or repair
#   install.sh --base <url>                                   install from another server (an enterprise's)
#   surogate-apply-update --apply <manifest> <signature> <tarball>      as root: apply a downloaded release
#
# Every line is in a function, and main runs from the script's last line: a download cut
# short runs nothing. Run as a user, it asks for sudo once and runs itself again as root
# from its own functions.

settings() {
  ROOT=/opt/surogate
  CHANNEL=stable
  RECORD=/etc/surogate/install.json
  LAUNCHER=/usr/local/bin/surogate
  ENTRY=/usr/share/applications/surogate.desktop
  PROFILE=/etc/apparmor.d/surogate-desktop
  POLICY=/usr/share/polkit-1/actions/ai.invergent.surogate.update.policy
  # How long an apply waits for another's lock, in seconds.
  LOCK_WAIT=300
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
  mkdir -p "$ROOT/versions"
  # Root's alone from its first moment: the update's lock is on it.
  ( umask 077 && mkdir -p "$ROOT/staging" )
  chmod 0755 "$ROOT" "$ROOT/versions"
  chmod 0700 "$ROOT/staging"
  # Room for the tarball's copy and the tree it unpacks to, which is about two and a half times its size.
  local need room
  need=$(( $(stat -c %s -- "$tarball") * 4 / 1024 ))
  room="$(df --output=avail -k "$ROOT" | tail -n 1)"
  [ "$room" -ge "$need" ] || fail "$ROOT needs $(( need / 1024 )) MB free to apply this release, and has $(( room / 1024 )) MB"
  # One at a time, by a lock on a folder only root can open: any user may open one that all may
  # read, hold a lock on it, and so stop every update. What an apply that stopped part way left in
  # staging goes.
  exec 9<"$ROOT/staging"
  flock -w "$LOCK_WAIT" 9 || fail "another install or update of Surogate Desktop is still running: try again once it has finished"
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

# What the app needs of the system: bubblewrap, socat and ripgrep for the file helper's srt; QEMU,
# virtiofsd, and uidmap's newuidmap and newgidmap for the VM; zstd for its image; and what this
# script runs itself.
packages() {
  say "installing the packages it needs"
  export DEBIAN_FRONTEND=noninteractive
  # A package source of the computer's own that fails stops nothing: what is needed may be known already.
  apt-get update -qq || say "apt-get update failed for a package source of this computer's: installing from what apt knows already"
  # Soon after a desktop's first boot, its unattended upgrades hold dpkg's lock for a while.
  apt-get install -y -qq -o DPkg::Lock::Timeout=300 bubblewrap socat ripgrep virtiofsd uidmap zstd openssl jq curl desktop-file-utils \
    && apt-get install -y -qq -o DPkg::Lock::Timeout=300 --no-install-recommends qemu-system-x86 \
    || fail "could not install the packages it needs: ripgrep and virtiofsd are in Ubuntu's universe, which this computer's package sources must include"
}

# Under Ubuntu's restriction of unprivileged user namespaces, the app's Electron gets them from a
# profile of its own, at the fixed path only root can write; its bwrap copy takes the same profile.
apparmor_profile() {
  [ "$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null)" = 1 ] || return 0
  cat >"$PROFILE" <<'PROFILE'
abi <abi/4.0>,
include <tunables/global>

profile surogate-desktop /opt/surogate/versions/*/surogate flags=(unconfined) {
  userns,

  include if exists <local/surogate-desktop>
}
PROFILE
  apparmor_parser -r "$PROFILE"
}

# The user who ran the script, in the kvm group: the VM runs on KVM from their next login.
kvm_group() {
  local user="${SUDO_USER:-}"
  [ -n "$user" ] && [ "$user" != root ] && getent group kvm >/dev/null || return 0
  id -nG "$user" | tr ' ' '\n' | grep -qx kvm && return 0
  gpasswd -a "$user" kvm >/dev/null
  RELOGIN=1
}

# The newest release at $1, checked as the user's update would be, then applied. An installed
# version newer than it stays (a mirror can lag, or a cache): the rest of the install repairs around it.
install_latest() {
  local base="$1" download release version installed
  download="$(mktemp -d)"
  trap "rm -rf -- '$download'" EXIT
  curl -q -fsSL --proto '=https,http' -o "$download/manifest.json" "$base/desktop/latest.json" \
    || fail "could not download $base/desktop/latest.json"
  curl -q -fsSL --proto '=https,http' -o "$download/manifest.json.sig" "$base/desktop/latest.json.sig" \
    || fail "could not download $base/desktop/latest.json.sig"
  signed "$download/manifest.json" "$download/manifest.json.sig" \
    || fail "$base/desktop/latest.json is not signed by Surogate's release key"
  release="$(release_of "$download/manifest.json")" \
    || fail "$base/desktop/latest.json is not a release of Surogate Desktop for this computer"
  read -r version _ <<<"$release"
  installed="$(installed_version)"
  if [ -n "$installed" ] && dpkg --compare-versions "$version" lt "$installed"; then
    say "kept the installed $installed, newer than the server's $version"
    return 0
  fi
  say "downloading Surogate Desktop $version"
  curl -q -fSL --proto '=https,http' -o "$download/release.tar.gz" "$base/desktop/$(jq -r .url "$download/manifest.json")" \
    || fail "could not download Surogate Desktop $version from $base"
  apply "$download/manifest.json" "$download/manifest.json.sig" "$download/release.tar.gz"
}

# The launcher, the desktop entry that registers surogate:// for every user, and the polkit
# action under which an administrator approves an update the app downloaded.
integrate() {
  cat >"$LAUNCHER.new" <<'LAUNCHER'
#!/bin/sh
# Surogate Desktop, as its install script installed it. VS Code's terminals export
# ELECTRON_RUN_AS_NODE: the app's Electron ignores it, but what the app starts would inherit it.
unset ELECTRON_RUN_AS_NODE
exec /opt/surogate/current/surogate "$@"
LAUNCHER
  chmod 0755 "$LAUNCHER.new"
  mv -T "$LAUNCHER.new" "$LAUNCHER"
  cat >"$ENTRY" <<'ENTRY'
[Desktop Entry]
Type=Application
Name=Surogate
Comment=Lets your agents work on folders of this computer
Exec=/usr/local/bin/surogate %u
Icon=/opt/surogate/current/resources/surogate.svg
Terminal=false
Categories=Development;
MimeType=x-scheme-handler/surogate;
StartupWMClass=Surogate
ENTRY
  update-desktop-database -q /usr/share/applications
  cat >"$POLICY" <<'POLICY'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">
<policyconfig>
  <vendor>Surogate</vendor>
  <action id="ai.invergent.surogate.update">
    <description>Install an update of Surogate Desktop</description>
    <message>Authentication is required to install an update of Surogate Desktop for every user of this computer</message>
    <defaults>
      <allow_any>auth_admin_keep</allow_any>
      <allow_inactive>auth_admin_keep</allow_inactive>
      <allow_active>auth_admin_keep</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">/opt/surogate/bin/surogate-apply-update</annotate>
    <annotate key="org.freedesktop.policykit.exec.argv1">--apply</annotate>
  </action>
</policyconfig>
POLICY
}

# Where the app updates from, and downloads its VM's image from: the base this script installed from.
record() {
  mkdir -p "$(dirname "$RECORD")"
  jq -n --arg base "$1" --arg channel "$CHANNEL" '{base: $base, channel: $channel}' >"$RECORD.new"
  chmod 0644 "$RECORD.new"
  mv -T "$RECORD.new" "$RECORD"
}

# What the computer lacks that the app would use, said and never a failure.
notes() {
  local browser
  for browser in /opt/google/chrome/chrome /opt/microsoft/msedge/msedge /opt/brave.com/brave/brave /opt/vivaldi/vivaldi /usr/lib/chromium/chromium; do
    [ -x "$browser" ] && break
    browser=
  done
  [ -n "$browser" ] || say "no supported browser is installed, so the agent cannot use a browser on this computer. Install Google Chrome, Microsoft Edge, Brave or Vivaldi; the Snap build of Chromium is not supported."
  [ -e /dev/kvm ] || say "This computer has no hardware virtualization (VT-x or AMD-V), or it is turned off in the firmware settings. Surogate will run the agent's commands emulated, several times slower."
  [ -z "${RELOGIN:-}" ] || say "$SUDO_USER was added to the kvm group: log out and back in to make the agent's commands fast."
}

install_all() {
  packages
  apparmor_profile
  kvm_group
  install_latest "$1"
  integrate
  record "$1"
  notes
  say "open Surogate from your applications, or run surogate"
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
    --base | "")
      local base=https://surogate.ai
      if [ "${1:-}" = --base ]; then
        [ "$#" -eq 2 ] && [[ "$2" =~ ^https?://[^[:space:]]+$ ]] || fail "usage: install.sh --base <http or https URL>"
        base="${2%/}"
      fi
      supported
      if [ "$EUID" -ne 0 ]; then
        say "installing needs administrator rights: sudo asks for your password once"
        # Again as root, from this script's own functions: a script piped to bash has no file to name.
        # sudo resets the environment, so the proxy the user's shell names goes with them.
        { declare -f; declare -p http_proxy https_proxy HTTPS_PROXY all_proxy ALL_PROXY no_proxy NO_PROXY 2>/dev/null || true; echo 'main "$@"'; } \
          | sudo -- bash -s -- "$@"
        return
      fi
      install_all "$base"
      ;;
    *)
      fail "usage: install.sh [--base <url>]"
      ;;
  esac
}

main "$@"
