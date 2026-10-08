#!/bin/bash -p
# Surogate Desktop's install script, and each version's bin/surogate-apply-update, the root
# helper that applies a verified release: the current version's copy at
# /opt/surogate/bin/surogate-apply-update is the one pkexec runs.
#
#   curl -fsSL https://surogate.ai/desktop/install.sh | bash                      install, update or repair
#   curl -fsSL https://surogate.ai/desktop/install.sh | bash -s -- --uninstall    remove it
#   install.sh --base <url>                                   install from another server (an enterprise's)
#   surogate-apply-update --apply <manifest> <signature> <tarball>      as root: apply a downloaded release
#
# Every line is in a function, and main runs from the script's last line: a download cut
# short runs nothing. Run as a user, it asks for sudo once and runs itself again as root
# from its own functions. Run as the helper, by its own name, bash starts it with -p: it reads
# no script and takes no function from the environment of whoever asked.

settings() {
  ROOT=/opt/surogate
  CHANNEL=stable
  RECORD=/etc/surogate/install.json
  LAUNCHER=/usr/local/bin/surogate
  ENTRY=/usr/share/applications/surogate.desktop
  PROFILE=/etc/apparmor.d/surogate-desktop
  POLICY=/usr/share/polkit-1/actions/ai.invergent.surogate.update.policy
  # The folder of the lock that one install, update or removal at a time holds: root's alone, and
  # outside /opt/surogate, so that it is there before the tree is made and after it is removed.
  LOCKS=/run/surogate-desktop
  # How long an apply waits for another's lock, and for each read of a file it is handed, in seconds.
  LOCK_WAIT=300
  READ_WAIT=300
  # Who reads the files an apply is handed, by user and group number: root, unless the helper was
  # run for another user (asker).
  READER=(0 0)
  # Folders of this run's own, which go however it ends.
  OWN=()
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

# A file's or a link's name as one word of one line, whatever it holds: a new line in one, or a
# terminal's codes, would otherwise read as lines of this script's own.
named() {
  printf '%q' "$1"
}

# A step that failed where no failure is expected: said in a line of this script's own, after
# whatever the step said itself.
unexpected() {
  fail "stopped, as this step failed: $1"
}

# Stopped by a signal, the script ends as it would by itself, once the command it runs has ended:
# a hangup, as from a terminal that is closed, would otherwise end it with its folders left. It
# clears up before it ends: where the signal comes as the script is ending already, to end again
# would leave out the clearing up that had just begun.
stoppable() {
  trap 'cleanup; exit 129' HUP
  trap 'cleanup; exit 130' INT
  trap 'cleanup; exit 141' PIPE
  trap 'cleanup; exit 143' TERM
}

# Run as main ends, however it ends. No signal stops it: a second one, as Ctrl+C pressed twice
# sends, would end its rm, and leave what that had not removed yet.
cleanup() {
  trap '' HUP INT PIPE TERM
  [ "${#OWN[@]}" -eq 0 ] || rm -rf -- "${OWN[@]}"
}

# A folder of this run's own, for it alone, which goes however the run ends: named as mktemp names
# one for $2 and what follows, listed as this run's, and only then made, so that a signal between
# any two of the three leaves nothing. Its name is put in the variable $1 names.
scratch() {
  local -n made="$1"
  made="$(mktemp -u "${@:2}")"
  OWN+=("$made")
  mkdir -m 0700 "$made"
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
# release's is, its hash, each whole (jq's $ also matches before a last newline), and its tarball's
# size in bytes, a whole number above 0 and below 10^15, which jq writes in digits alone. The
# manifest is one JSON document: of two, the first would be applied and both kept as the version's
# mark. Printed as "<version> <sha256> <size>".
release_of() {
  jq -ers --arg channel "$CHANNEL" '
    select(length == 1) | .[0]
    | select((.version | type == "string" and test("\\A[0-9]+\\.[0-9]+\\.[0-9]+\\z"))
      and .channel == $channel and .platform == "linux" and .arch == "x64"
      and .url == "releases/\(.version)/surogate-desktop-\(.version)-linux-x64.tar.gz"
      and (.sha256 | type == "string" and test("\\A[0-9a-f]{64}\\z"))
      and (.size | type == "number" and . > 0 and . == floor and . < 1e15))
    | "\(.version) \(.sha256) \(.size | floor)"' "$1" 2>/dev/null
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

# Whether version folder $2 is whole, for manifest $1: its mark is that manifest, and the app and
# its helper are there to run.
whole() {
  cmp -s "$1" "$2/release.json" && [ -f "$2/surogate" ] && [ -x "$2/surogate" ] \
    && [ -f "$2/bin/surogate-apply-update" ] && [ -x "$2/bin/surogate-apply-update" ]
}

# One install, update or removal at a time, by a lock on a file in a folder only root can open: any
# user may open what all may read, hold a lock on it, and so stop every update. The folder is under
# /run and not in /opt/surogate: a removal takes the tree away, and an apply that waited for it
# would hold a lock on a folder that is gone. Root makes the folder itself, closed to everyone else
# from its first moment. One that is there already is used only when it is as root makes it: a
# folder, root's, with nothing for anyone else, and no link. Any other is refused and never taken
# over: what someone else could have put in it would still be there. The lock is waited for
# LOCK_WAIT at most, and held until this script ends or closes it.
lock() {
  mkdir -m 0700 "$LOCKS" 2>/dev/null || true
  [ "$(stat -c '%F %u %a' -- "$LOCKS" 2>/dev/null)" = "directory 0 700" ] \
    || fail "$LOCKS must be a folder of root's own that no one else opens (mode 700), and no link: remove what is there, and run this again"
  exec 9>>"$LOCKS/lock"
  flock -w "$LOCK_WAIT" 9 || fail "another install or update of Surogate Desktop is still running: try again once it has finished"
}

# The user the helper was run for, who reads the files it is handed: pkexec's caller, or sudo's.
# Each names that user by number in the helper's environment, and sets it itself, whatever its own
# caller's environment held. Nothing else is asked who it was. Naming a user only ever lowers the
# helper's rights to read, from root's to that user's.
asker() {
  local name uid gid
  for name in PKEXEC_UID SUDO_UID; do
    uid="${!name:-}"
    [ -n "$uid" ] || continue
    [[ "$uid" =~ ^(0|[1-9][0-9]{0,9})$ ]] || fail "$name is not a user's number"
    # Compared as it is written: what is no number is then never taken for root's.
    [ "$uid" != 0 ] || continue
    gid="$(getent passwd "$uid" | cut -d: -f4)" && [[ "$gid" =~ ^[0-9]+$ ]] || fail "$name names no user of this computer"
    READER=("$uid" "$gid")
    # The reader is that user, and no other: setpriv takes digits for a user's name where one is so
    # named, and counts a number past the last one from 0 again.
    [ "$(as_reader id -u 2>/dev/null):$(as_reader id -g 2>/dev/null)" = "$uid:$gid" ] || fail "$name names no user of this computer"
    return 0
  done
}

# Runs "$@" as the user who reads an apply's files, in that user's own group and no other, with
# none of the helper's open files, and for READ_WAIT at most: a filesystem of the user's own may
# never answer. The command alone is killed then (--foreground): GNU's timeout otherwise kills
# itself with it, and bash says so in words of its own.
as_reader() {
  timeout --foreground -s KILL "$READ_WAIT" setpriv --reuid "${READER[0]}" --regid "${READER[1]}" --clear-groups "$@" 9<&- </dev/null
}

# Copies file $1, which an apply was handed, to $2 in root's staging: read once, as the user who
# asked, never through a link and never waiting on a pipe, whatever it has become since it was
# named. A pipe gives an empty copy at once. No more than $3 bytes and one are copied, whatever the
# file holds: root's end of the pipe counts them. Whether the file held no more than $3.
taken() {
  local file="$1" copy="$2" most="$3" ends
  as_reader dd if="$file" iflag=nofollow,nonblock,count_bytes count="$(( most + 1 ))" bs=64K status=none 2>/dev/null \
    | head -c "$(( most + 1 ))" 2>/dev/null >"$copy" && ends=(0 0) || ends=("${PIPESTATUS[@]}")
  # Root's own end of the pipe failed: the disk's fault, and not the file's.
  [ "${ends[1]}" -eq 0 ] || fail "$ROOT/staging could not be written: is its disk full?"
  [ "${ends[0]}" -eq 0 ] || fail "$(named "$file") is not a downloaded release's file"
  [ "$(stat -c %s "$copy")" -le "$most" ]
}

# Applies a release as root: its manifest $1, signature $2 and tarball $3, which the user who
# downloaded them can still change, so each is read once, as that user, into root's staging, and
# only the copies are checked and used. The tree is extracted in staging, refused when anything in
# it is not a plain file, folder or link inside it, moved into versions/<version> with its own copy
# of bwrap, and /opt/surogate/current is switched to it by one rename; its helper is then the one
# pkexec runs. An older version than the installed one is refused. The previous version is kept,
# and older ones not running are removed, by an update; a repair removes none. A version that is
# here whole is repaired as it is, and its tarball is not read: $3 may then be empty.
apply() {
  local manifest="$1" signature="$2" tarball="$3" file
  # A folder or a missing file is refused here; a link, as each is copied, below.
  for file in "$manifest" "$signature" ${tarball:+"$tarball"}; do
    as_reader test -f "$file" || fail "$(named "$file") is not a downloaded release's file"
  done
  # Before the tree is touched: a removal that runs now takes it away, and this apply makes it again.
  lock
  # Its folders are root's own, whoever made them. The tree's first: from then on no one else puts
  # anything in it. Then the three in it, each a folder of the tree's own and never a link, which
  # would have root make another folder its own, and work there.
  mkdir -p "$ROOT"
  chown 0:0 "$ROOT"
  chmod 0755 "$ROOT"
  local inner
  for inner in versions bin staging; do
    [ ! -L "$ROOT/$inner" ] || fail "$ROOT/$inner is a link, where Surogate Desktop keeps a folder of its own: remove it, and run this again"
  done
  mkdir -p "$ROOT/versions" "$ROOT/bin"
  # Root's alone from its first moment: what an apply copies and unpacks is in it.
  ( umask 077 && mkdir -p "$ROOT/staging" )
  chown 0:0 "$ROOT/versions" "$ROOT/bin" "$ROOT/staging"
  chmod 0755 "$ROOT/versions" "$ROOT/bin"
  chmod 0700 "$ROOT/staging"
  # What an apply that was killed left in staging goes, before any room is measured. This one's
  # own folder goes however it ends.
  find "$ROOT/staging" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  local work
  scratch work "$ROOT/staging/apply.XXXXXX"
  # A manifest is a line, and its signature Ed25519's 64 bytes.
  taken "$manifest" "$work/manifest.json" 4096 || fail "$(named "$manifest") is not a downloaded release's file"
  taken "$signature" "$work/manifest.json.sig" 64 || fail "$(named "$signature") is not a downloaded release's file"

  signed "$work/manifest.json" "$work/manifest.json.sig" || fail "the release's manifest is not signed by Surogate's release key"
  local release version sha256 size
  release="$(release_of "$work/manifest.json")" || fail "the release's manifest is not a release of Surogate Desktop for this computer"
  read -r version sha256 size <<<"$release"
  local previous
  previous="$(installed_version)"
  if [ -n "$previous" ] && dpkg --compare-versions "$version" lt "$previous"; then
    fail "$version is older than the installed $previous"
  fi
  # The system's bwrap, which the app gives srt: a copy in the version's folder takes the app's
  # AppArmor profile, not one Ubuntu attaches to /usr/bin/bwrap. Made again at each apply, as apt
  # may have updated it.
  [ -x /usr/bin/bwrap ] || fail "bubblewrap is missing: run Surogate Desktop's install script again"

  # What this apply puts under a name is first made whole in its own folder: the version's tree,
  # when its folder is not here whole already, or its new bwrap alone; the link that current
  # becomes; and the helper pkexec runs.
  local folder="$ROOT/versions/$version" name="surogate-desktop-$version-linux-x64" top=""
  if ! whole "$work/manifest.json" "$folder"; then
    # The install script hands no tarball for a version it found here whole.
    [ -n "$tarball" ] || fail "$version is no longer whole in $ROOT: run Surogate Desktop's install script again"
    # Room for the tarball's copy and the tree it unpacks to, which is about two and a half times
    # its size: the size its signed manifest names, and never what the file it was handed says.
    local need room
    need=$(( size / 256 ))
    room="$(df --output=avail -k "$ROOT" | tail -n 1)"
    [ "$room" -ge "$need" ] || fail "$ROOT needs $(( (need + 1023) / 1024 )) MB free to apply this release, and has $(( room / 1024 )) MB"
    # No more of the tarball is copied than that size, and a copy of any other size is refused
    # before it is read again for its hash.
    taken "$tarball" "$work/release.tar.gz" "$size" && [ "$(stat -c %s "$work/release.tar.gz")" -eq "$size" ] \
      || fail "the downloaded release is not the $size bytes its manifest names"
    [ "$(sha256sum <"$work/release.tar.gz" | cut -d' ' -f1)" = "$sha256" ] || fail "the downloaded release is not the one its manifest names"
    # tar unpacks a set-id member without its bit (--no-same-permissions), so that only the
    # archive's own listing shows one: the fourth and seventh letters of a member's mode.
    tar -tvzf "$work/release.tar.gz" >"$work/listing" 2>/dev/null || fail "the release's archive could not be unpacked"
    ! grep -Eq '^(.{3}|.{6})[sS]' "$work/listing" || fail "the release's archive holds a special file, a set-id file or a hard link"
    mkdir "$work/tree"
    tar -xzf "$work/release.tar.gz" -C "$work/tree" --no-same-owner --no-same-permissions 2>/dev/null \
      || fail "the release's archive could not be unpacked"
    [ "$(ls -A "$work/tree")" = "$name" ] && [ -d "$work/tree/$name" ] && [ ! -L "$work/tree/$name" ] \
      || fail "the release's archive holds more than $name/"
    # The tree's checks are made under a name no archive can know. A link that climbs out of the
    # tree cannot then name its way back in, so one that resolves inside the tree never left it,
    # and resolves the same wherever the tree is put.
    top="$(mktemp -u "$work/tree.XXXXXXXXXX")"
    mv -T "$work/tree/$name" "$top"
    local link target
    # tar keeps no name with .. and nothing outside the tree, and no set-id bit; one that did
    # reach the tree is refused here too.
    [ -z "$(find "$top" \( -type b -o -type c -o -type p -o -type s -o -perm /6000 -o \( -type f -links +1 \) \) -print -quit)" ] \
      || fail "the release's archive holds a special file, a set-id file or a hard link"
    # No link names a whole path, climbs out of the tree as it is written, or leaves it as it resolves.
    while IFS= read -r -d '' link; do
      target="$(readlink "$link")"
      [[ "$target" != /* ]] && [[ "$(realpath -ms "${link%/*}/$target")" == "$top"/* ]] && [[ "$(realpath -m "$link" 2>/dev/null)" == "$top"/* ]] \
        || fail "the release's archive links outside itself: $(named "${link#"$top"/}")"
    done < <(find "$top" -type l -print0)
    # The app and its helper are programs, no folders, and bin a folder of the tree's own, where
    # this version's bwrap goes.
    [ -f "$top/surogate" ] && [ -x "$top/surogate" ] && [ -d "$top/bin" ] && [ ! -L "$top/bin" ] \
      && [ -f "$top/bin/surogate-apply-update" ] && [ -x "$top/bin/surogate-apply-update" ] \
      || fail "the release's archive is not Surogate Desktop"
    chmod -R go-w "$top"
    chmod 0755 "$top"
    # Its mark and its bwrap are made here: whatever the archive has under their names goes, a link
    # or a folder too, and is never written through or into.
    rm -rf -- "$top/release.json" "$top/bin/bwrap"
    install -m 0755 /usr/bin/bwrap "$top/bin/bwrap"
    # Last: a version folder with its manifest is whole.
    cp "$work/manifest.json" "$top/release.json"
  else
    install -m 0755 /usr/bin/bwrap "$work/bwrap"
  fi
  install -m 0755 "${top:-$folder}/bin/surogate-apply-update" "$work/helper"
  ln -s "$folder" "$work/current"
  # All of it is on the disk before any of it has its name. A rename reaches the disk before a
  # new file's bytes do: a power cut soon after would leave a version's folder under its name, its
  # mark there or not, with files that are empty, and current may name it already.
  sync -f "$work"
  # From its first rename to its last, no signal stops it: stopped between the two that replace the
  # installed version's own folder, it would clear up the folder it had taken out, and leave
  # current naming none.
  trap '' HUP INT PIPE TERM
  if [ -n "$top" ]; then
    # One rename gives the tree its name. Where that is the installed version's, its old folder
    # leaves the name first.
    [ ! -e "$folder" ] || mv -T "$folder" "$work/replaced"
    mv -T "$top" "$folder"
  else
    mv -T "$work/bwrap" "$folder/bin/bwrap"
  fi
  mv -T "$work/current" "$ROOT/current"
  # The helper pkexec runs, at a path with no link in it: polkit 127 (Ubuntu 26.04) matches an
  # action's exec.path against the program's resolved path, polkit 124 (24.04) against the path given.
  mv -T "$work/helper" "$ROOT/bin/surogate-apply-update"
  stoppable

  # Kept: this version and the one before it. Removed: the rest, once nothing runs from them.
  # Each leaves versions by one rename, into this apply's folder, so that none is ever left under
  # its name with its mark and without its files. An apply of the installed version removes none:
  # it is a repair, or the run after an update that stopped late, and the version before this one
  # is no longer known to it.
  if [ "$previous" != "$version" ]; then
    local kept
    for kept in "$ROOT"/versions/*; do
      [ "$kept" = "$folder" ] || [ "$kept" = "$ROOT/versions/$previous" ] || in_use "$kept" || mv -T "$kept" "$work/removed.${kept##*/}"
    done
  fi
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
  local base="$1" download release version installed tarball=""
  scratch download --tmpdir tmp.XXXXXXXXXX
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
  # A version that is here whole is not downloaded again: apply repairs it as it is.
  if ! whole "$download/manifest.json" "$ROOT/versions/$version"; then
    say "downloading Surogate Desktop $version"
    tarball="$download/release.tar.gz"
    curl -q -fSL --proto '=https,http' -o "$tarball" "$base/desktop/$(jq -r .url "$download/manifest.json")" \
      || fail "could not download Surogate Desktop $version from $base"
  fi
  apply "$download/manifest.json" "$download/manifest.json.sig" "$tarball"
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

# An XDG folder: $1 when it is absolute, as the XDG specification reads it, else the default $2.
xdg() {
  if [[ "${1:-}" == /* ]]; then echo "$1"; else echo "$2"; fi
}

# The invoking user's XDG config, data and cache folders, one a line, from their login's own
# environment: sudo reset this one's.
login_folders() {
  local home lines config data cache
  home="$(getent passwd "$1" | cut -d: -f6)"
  lines="$(runuser -l "$1" -c 'printf "\n%s\n%s\n%s\n" "${XDG_CONFIG_HOME:-}" "${XDG_DATA_HOME:-}" "${XDG_CACHE_HOME:-}"' 2>/dev/null | tail -n 3)" || lines=
  { read -r config; read -r data; read -r cache; } <<<"$lines" || true
  xdg "${config:-}" "$home/.config"
  xdg "${data:-}" "$home/.local/share"
  xdg "${cache:-}" "$home/.cache"
}

# Removes the app for every user of the computer, and the invoking user's autostart entry; asks
# before deleting that user's data, as that user. Other users' data, every chat's folder and the
# packages stay. $1-$3: the user's XDG config, data and cache folders, when their session gave them.
uninstall() {
  # One at a time with an apply: one that runs now finishes before the tree goes, and one that
  # starts now waits, and makes the tree again once this has ended.
  lock
  in_use "$ROOT" && fail "Surogate is running: quit it first, for every user of this computer"
  if [ -f "$PROFILE" ]; then
    apparmor_parser -R "$PROFILE" 2>/dev/null || true
    rm -f -- "$PROFILE"
  fi
  # A version is whole by its mark, so each loses its mark first: stopped while it removes the
  # tree, this leaves no version that a later install would take as whole, with files of it gone.
  rm -rf -- "$ROOT"/versions/*/release.json
  # Where /opt/surogate is a disk of its own, the folder cannot go: all that is in it does, and
  # the rest of the install with it.
  local left=
  if mountpoint -q "$ROOT"; then
    find "$ROOT" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
    left="$ROOT"
  else
    rm -rf -- "$ROOT"
  fi
  rm -rf -- "$(dirname "$RECORD")"
  rm -f -- "$LAUNCHER" "$ENTRY" "$POLICY"
  if command -v update-desktop-database >/dev/null; then update-desktop-database -q /usr/share/applications; fi
  # The lock is root's alone: nothing that runs as the user below has it open.
  exec 9<&-
  say "removed from this computer"
  [ -z "$left" ] || say "left $left itself, now empty: it is a disk of its own (a mount point)"

  local user="${SUDO_USER:-}" config data cache answer=
  [ -n "$user" ] && [ "$user" != root ] || return 0
  if [ "$#" -eq 3 ]; then
    config="$1" data="$2" cache="$3"
  else
    { read -r config; read -r data; read -r cache; } < <(login_folders "$user")
  fi
  # As the user: only what they may change goes.
  runuser -u "$user" -- rm -f -- "$config/autostart/surogate.desktop"
  # As the user too, whether they have data: root may not see into a home that another computer serves.
  runuser -u "$user" -- test -e "$data/surogate" || runuser -u "$user" -- test -e "$cache/surogate" || return 0
  # Asked on the terminal, as this script's input is itself; with none to ask on, the data stays.
  if (exec </dev/tty) 2>/dev/null; then
    read -r -p "Surogate Desktop: also delete $user's sign-in, device token and browser profiles, in $(named "$data/surogate")? Chat folders stay. [y/N] " answer </dev/tty || answer=
  fi
  if [[ "$answer" == [Yy]* ]]; then
    runuser -u "$user" -- rm -rf -- "$data/surogate" "$cache/surogate" 2>/dev/null \
      || fail "could not delete all of $user's app data: what $user may not change stays, in $(named "$data/surogate") and $(named "$cache/surogate")"
    say "deleted $user's app data"
  else
    say "kept $user's app data, in $(named "$data/surogate")"
  fi
}

main() {
  set -Eeuo pipefail
  umask 022
  settings
  trap cleanup EXIT
  stoppable
  # Where a failure ends the script (-e), and not inside a $( ): there, its caller decides.
  trap '[ "$BASH_SUBSHELL" -gt 0 ] || unexpected "$BASH_COMMAND"' ERR
  case "${1:-}" in
    --apply)
      # The helper runs the system's own tools, wherever its caller's PATH points, and reads what
      # it is handed in no locale of its caller's, which pkexec and sudo both pass on: in most, more
      # than ten characters are digits, and a name's other letters are written as they are.
      export PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C
      [ "$#" -eq 4 ] && [ -n "$4" ] || fail "usage: surogate-apply-update --apply <manifest> <signature> <tarball>"
      [ "$EUID" -eq 0 ] || fail "applying a release needs administrator rights"
      supported
      asker
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
          | sudo -- bash -s -- "$@" || exit "$?"
        return
      fi
      install_all "$base"
      ;;
    --uninstall)
      if [ "$EUID" -ne 0 ]; then
        # Nothing may stand after it: an argument this script does not know is refused before
        # sudo is asked, never dropped and the app removed all the same.
        [ "$#" -eq 1 ] || fail "usage: install.sh --uninstall"
        say "removing it needs administrator rights: sudo asks for your password once"
        # The user's own folders go with it, as their session names them: sudo resets the environment.
        { declare -f; echo 'main "$@"'; } | sudo -- bash -s -- --uninstall \
          "$(xdg "${XDG_CONFIG_HOME:-}" "$HOME/.config")" "$(xdg "${XDG_DATA_HOME:-}" "$HOME/.local/share")" "$(xdg "${XDG_CACHE_HOME:-}" "$HOME/.cache")" \
          || exit "$?"
        return
      fi
      # What removes it is the system's own tools, wherever its caller's PATH points.
      export PATH=/usr/sbin:/usr/bin:/sbin:/bin
      [ "$#" -eq 1 ] || [ "$#" -eq 4 ] || fail "usage: install.sh --uninstall"
      shift
      uninstall "$@"
      ;;
    *)
      fail "usage: install.sh [--base <url>] [--uninstall]"
      ;;
  esac
}

main "$@"
