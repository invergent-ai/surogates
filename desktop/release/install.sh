#!/bin/bash -p
# Surogate Desktop's install script, and each version's bin/surogate-apply-update, the root
# helper that applies a verified release: the copy at /opt/surogate/bin/surogate-apply-update is
# the one pkexec runs, the newest release's that this computer has installed.
#
#   curl -fsSL https://surogate.ai/desktop/install.sh | bash                      install, update or repair
#   curl -fsSL https://surogate.ai/desktop/install.sh | bash -s -- --uninstall    remove it
#   install.sh --base <url>                                   install from another server (an enterprise's)
#   curl -fsSL https://surogate.ai/desktop/install.sh | bash -s -- --version <x.y.z>    roll back to that release
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
  # The helper pkexec runs, and beside it the manifest of the release it is of. The helper's own
  # list of release keys is the one this computer trusts (trusted), and neither ever goes back to
  # an older release's (apply).
  HELPER="$ROOT/bin/surogate-apply-update"
  HELPER_MARK="$ROOT/bin/release.json"
  # How long an apply waits, in seconds: for another's lock; for its read of a release's tarball;
  # and for each other thing it has the asking user's own processes do, all of them small. That
  # user can make each last its whole bound. The reads made with the lock held, a manifest's, a
  # signature's and a tarball's, are together shorter than the wait for the lock: no one who was
  # let apply once keeps the next one waiting until it gives up.
  LOCK_WAIT=300
  READ_WAIT=120
  SMALL_WAIT=5
  # How long a download from the base may wait, as curl is told: 30 seconds to be connected, and a
  # minute at under 1024 bytes a second. A base that takes the connection and never answers, or
  # stops in the middle of an answer, would otherwise hold an install or a rollback for good.
  TIMELY=(--connect-timeout 30 --speed-limit 1024 --speed-time 60)
  # Who reads the files an apply is handed, by user and group number, and by name: root, unless the
  # helper was run for another user (asker).
  READER=(0 0 root)
  # Folders of this run's own, which go however it ends.
  OWN=()
  # The release keys' public halves: a release's manifest is signed by the private half of one of
  # them (Ed25519). A rotation lists the old key and the new for one release, which the old signs.
  # On a computer that has a helper, the helper's list is the one that counts, and not this one.
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

# A folder of this run's own, for it alone, which goes however the run ends: made and named in one
# step, as mktemp makes one for $2 and what follows, so that no one else has its name before it is
# there, and only what this run made is ever listed as its own. Its name is put in the variable $1
# names. From before the folder is made until it is listed, no signal stops the script, nor
# mktemp, which a terminal's signal reaches too: stopped between the two, the run would end with a
# folder that is made and listed nowhere, and leave it. A signal that comes then is let by, as one
# is from an apply's first rename to its last: the script goes on.
scratch() {
  local -n made="$1"
  trap '' HUP INT PIPE TERM
  made="$(mktemp -d "${@:2}")"
  OWN+=("$made")
  stoppable
}

# Ubuntu 24.04 LTS or a later LTS release, on x64. Nothing is changed before this passes. A
# computer with no /etc/os-release, as one that is no Linux, is no Ubuntu: it is told the same, and
# nothing of bash's own. The sentence is said as it is, and not as a line of this script's, which
# would say the app's name twice.
supported() {
  local id version version_id
  id="$(. /etc/os-release 2>/dev/null && echo "${ID:-}")" || true
  version="$(. /etc/os-release 2>/dev/null && echo "${VERSION:-}")" || true
  version_id="$(. /etc/os-release 2>/dev/null && echo "${VERSION_ID:-}")" || true
  [ "$id" = ubuntu ] && [[ "$version" == *LTS* ]] && dpkg --compare-versions "$version_id" ge 24.04 && [ "$(uname -m)" = x86_64 ] \
    || { echo "Surogate Desktop supports Ubuntu 24.04 LTS or a later LTS release (x64)" >&2; exit 1; }
}

# Whether $1 is a file as an apply leaves one, read as numbers alone, as the lock's folder is: its
# kind and its mode as one, $2 (81ed: a file, and no link, at 0755; 81a4: one at 0644), and 0,
# root's own.
roots_own() {
  [ "$(stat -c '%f %u' -- "$1" 2>/dev/null)" = "$2 0" ]
}

# Whether $1 is a file of root's own that no one else may write, at whichever mode of reading and
# running: a file and no link, root's, with no write bit for its group or for others, and with no
# set-id or sticky bit, which nothing this script writes has. Read as numbers alone, as roots_own
# reads.
roots_alone() {
  local seen mode
  seen="$(stat -c '%f %u' -- "$1" 2>/dev/null)" || return 1
  [ "${seen#* }" = 0 ] || return 1
  mode=$(( 16#${seen% *} ))
  (( (mode & 0170000) == 0100000 && (mode & 07022) == 0 ))
}

# The release keys this computer trusts, into the array $1 names. One file says which: the helper
# pkexec runs, whose own list they are, whichever script asks, that helper or an install script
# of any age. Its list is read as settings writes one, each entry between its two quotes, and the
# helper is not run. Only a computer with no helper and no version, at its first install, takes
# this script's own list. A helper that is not as an apply leaves one, or lists nothing, is
# refused, and so is a version that has no helper, which an apply puts in before it switches to
# one: this script's list never stands in for the helper's, and would be an older one where the
# script is.
trusted() {
  local -n list="$1"
  list=("${RELEASE_KEYS[@]}")
  if [ ! -e "$HELPER" ] && [ ! -L "$HELPER" ]; then
    [ -e "$ROOT/current" ] || [ -L "$ROOT/current" ] || return 0
    list=()
    fail "$HELPER is not as Surogate Desktop's install leaves it: remove Surogate Desktop with --uninstall, and install it again"
  fi
  list=()
  roots_own "$HELPER" 81ed || fail "$HELPER is not as Surogate Desktop's install leaves it: remove Surogate Desktop with --uninstall, and install it again"
  listed list "$HELPER"
  [ "${#list[@]}" -gt 0 ] || fail "$HELPER lists no release key: remove Surogate Desktop with --uninstall, and install it again"
}

# The release keys that helper $2 lists, into the array $1 names: its list as settings writes one,
# each entry between its two quotes. The helper is read, and not run.
listed() {
  local -n entries="$1"
  local text
  entries=()
  text="$(sed -n '/^[[:space:]]*RELEASE_KEYS=($/,/^[[:space:]]*)$/p' "$2")"
  while [[ "$text" == *\'*\'* ]]; do
    text="${text#*\'}"
    entries+=("${text%%\'*}")
    text="${text#*\'}"
  done
}

# Whether manifest $1 is signed, in signature $2, by the private half of one of the release keys
# this computer trusts.
signed() {
  local keys
  trusted keys
  signed_by "$1" "$2" "${keys[@]}"
}

# Whether manifest $1 is signed, in signature $2, by the private half of one of the release keys
# that follow them.
signed_by() {
  local key
  for key in "${@:3}"; do
    openssl pkeyutl -verify -pubin -inkey <(printf '%s\n' "$key") -rawin -in "$1" -sigfile "$2" >/dev/null 2>&1 && return 0
  done
  return 1
}

# The JSON object that file $1 holds, on one line, where the file is a manifest as a release signs
# one and as an apply copies one, to a version's mark and to the helper's: one object on one line,
# whose newline is the file's last byte, of 4096 bytes at most. No more of the file is read than
# those and one byte. Of two documents, the first would be applied and both kept as a mark; and
# each would name a version, where dpkg calls a version of two lines older than any other. With
# "any" as $2 the object may be on any number of lines, as the install record is written. $3 is
# its bound in bytes where that is not a manifest's: the release job reads the app's own package
# by this rule, for the state schema it writes into a manifest (publish.sh describe). A bound is
# a number of bytes from 1 to a megabyte, written in the ten digits with no zero before it, and
# nothing is read by any other: it goes into the shell's own arithmetic, where a name is a
# variable's and what stands in its brackets is run. Asked letter by letter, and of no range of
# letters, which is another range in another locale.
#
# It is JSON as JSON is written, and no more of what jq reads besides: the app reads a manifest
# with a reader of its own (oneObject in src/shell/updates.ts), and takes what this takes and
# nothing else. So: no byte order mark before it; no replacement character, which is what jq makes
# of a byte that is no UTF-8, and counts three bytes for; each number in JSON's own spelling (jq
# also reads +1, 01, 1., .5, nan and infinity) and in 17 digits at most (jq rounds a longer one to
# 17 digits before it makes a number of it: 137438953472.000015 is whole to another reader, and
# not to jq); and no value more than 64 fields and places down (how deep jq reads at all is its
# own, and changes with jq).
one_object() {
  local most="${3:-4096}"
  case "$most" in 0* | *[!0123456789]*) return 1 ;; esac
  [ "${#most}" -le 7 ] && [ "$most" -le 1048576 ] || return 1
  head -c "$(( most + 1 ))" -- "$1" 2>/dev/null \
    | jq -ceRs --arg lines "${2:-one}" --argjson most "$most" '
      select(utf8bytelength <= $most and ($lines == "any" or test("\\A[^\\n]*\\n\\z")) and (test("\ufffd") | not)
        and test("\\A(?:[ \\t\\r\\n\\[\\]{}:,]|\"(?:[^\"\\\\]|\\\\.)*\"|(?:true|false|null|-?(?!(?:[0-9]\\.?){18})(?:0|[1-9][0-9]*)(?:\\.[0-9]+)?(?:[eE][-+]?[0-9]+)?)(?![^ \\t\\r\\n\\[\\]{}:,\"]))*\\z"))
      | fromjson | select(type == "object" and ([paths | length] | max // 0) <= 64)' 2>/dev/null
}

# A signed manifest's fields: a release of this channel for this platform, its version x.y.z with
# no zero before a part (dpkg reads 1.2.03 as 1.2.3, and a version has one spelling), its tarball
# where every release's is, its hash, each whole (jq's $ also matches before a last newline), its
# tarball's size in bytes, a whole number from 1 and below 10^15, which jq writes in digits
# alone, and the state schema of what the app keeps in each user's home, a whole number from 1
# and below 10^15, each as it rounds: jq compares a number as it is written, where
# 999999999999999.99 is 10^15 and 1e-400 is nothing. The manifest is one JSON object on one line
# (one_object). Printed as "<version> <sha256> <size>".
release_of() {
  one_object "$1" | jq -er --arg channel "$CHANNEL" '
    select((.version | type == "string" and test("\\A(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\z"))
      and .channel == $channel and .platform == "linux" and .arch == "x64"
      and .url == "releases/\(.version)/surogate-desktop-\(.version)-linux-x64.tar.gz"
      and (.sha256 | type == "string" and test("\\A[0-9a-f]{64}\\z"))
      and (.size | type == "number" and . == floor and (floor | . >= 1 and . < 1e15))
      and (.stateSchema | type == "number" and . == floor and (floor | . >= 1 and . < 1e15)))
    | "\(.version) \(.sha256) \(.size | floor)"' 2>/dev/null
}

# The version /opt/surogate/current names, or nothing.
installed_version() {
  local target
  target="$(readlink "$ROOT/current" 2>/dev/null)" || return 0
  basename "$target"
}

# The release that the helper's mark names. Fails where there is no mark of root's own that is a
# release's manifest as an apply copies one (one_object), and names a release.
marked() {
  roots_own "$HELPER_MARK" 81a4 \
    && one_object "$HELPER_MARK" | jq -er '.version | select(type == "string" and test("\\A(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\z"))' 2>/dev/null
}

# The release the helper pkexec runs is of, as its mark beside it names it: nothing on a computer
# with no helper. Fails where a helper has no mark that names one (marked).
helper_release() {
  [ -e "$HELPER" ] || [ -L "$HELPER" ] || return 0
  marked
}

# The release whose helper's pair is found half done, into the variable $1 names, or nothing: an
# apply was stopped between its mark's rename and the helper's. The mark is root's own word for a
# release (marked), that release is here, its folder's mark the helper's mark byte for byte, and
# the helper pkexec runs is not, byte for byte, that folder's own: it is other bytes, or is not
# there, or the folder has no helper of its own to show which it is. A folder that cannot give
# its helper is half of a pair no less: were it passed over, the helper there now, which may be
# the release's before, would decide what is installed next.
# Nothing is half done where there is no such mark, or where its release is not here: an older
# version applied since, with the helper kept, has taken its folder away; or the folder is the
# same version built again, stopped before its mark's rename. Nor is the folder of any other
# version than the mark names a part of the pair: one that has lost its helper is unpacked again
# by an apply of that version.
half_done() {
  local -n found="$1"
  local own
  found="$(marked)" || found=""
  [ -n "$found" ] || return 0
  own="$ROOT/versions/$found/bin/surogate-apply-update"
  if cmp -s "$HELPER_MARK" "$ROOT/versions/$found/release.json" && ! cmp -s "$own" "$HELPER"; then return 0; fi
  found=""
}

# Finishes a helper's pair that an apply left half done (half_done): the mark names a release,
# and the helper is still the one before, or at a first install none. That release's folder is
# here whole, as it had its name before the mark had: its own helper is put where pkexec runs
# one, by one rename and never written into, from a copy in $1, a folder of this apply's own. So
# the keys this computer trusts are those of the release its mark names before anything is asked
# of them: stopped there, an update that dropped a key would otherwise leave the key trusted
# until a later release came, and what the key signed meanwhile would be taken.
#
# This is the one change that an apply makes and may then refuse: whatever it is handed, a pair
# found half done is finished first. Every other refusal leaves all as it was.
#
# Such a pair is finished or refused, and never passed over, which would leave the release before
# trusted for what is applied next:
# - The helper there now is one whose keys are taken, or this is a first install (trusted).
# - The folder is whole for the mark, with a helper of its own, and its mark and its helper are
#   root's own, the helper at any mode that an apply takes and that lets no one else write it.
# - The helper is never finished toward an older release, from a mark that is behind it, as an
#   install script that writes no mark leaves one under a newer release: where a newer version
#   than the mark names is installed, or the helper is the own one of a newer version that is here.
paired() {
  local version of own keys installed other
  local refused="$HELPER is not as Surogate Desktop's install leaves it: remove Surogate Desktop with --uninstall, and install it again"
  half_done version
  [ -n "$version" ] || return 0
  of="$ROOT/versions/$version"
  own="$of/bin/surogate-apply-update"
  trusted keys
  whole "$HELPER_MARK" "$of" && roots_alone "$own" || fail "$refused"
  installed="$(installed_version)"
  if a_version "$installed" && dpkg --compare-versions "$installed" gt "$version"; then fail "$refused"; fi
  for other in "$ROOT"/versions/*; do
    if a_version "${other##*/}" && dpkg --compare-versions "${other##*/}" gt "$version" && cmp -s "$other/bin/surogate-apply-update" "$HELPER"; then
      fail "$refused"
    fi
  done
  install -m 0755 "$own" "$1/paired"
  sync -f "$1"
  mv -T "$1/paired" "$HELPER"
}

# Ends an install or a rollback whose release, $1 by its address, with manifest $2 and signature
# $3, no release key this computer trusts has signed. Before any lock is held, the keys asked are
# those of the helper that is there, and a helper's pair that is half done is finished only by an
# apply: where the release that the pair's mark names lists a key that did sign this one, an
# update that added the key was stopped before its end, and what is said is what ends it. Not for
# that update itself, $4 where a rollback asks for one: it would be told to run itself first.
unsigned() {
  local half own keys
  half_done half
  own="$ROOT/versions/$half/bin/surogate-apply-update"
  if [ -n "$half" ] && [ "$half" != "${4:-}" ] && whole "$HELPER_MARK" "$ROOT/versions/$half" && roots_alone "$own"; then
    listed keys "$own"
    ! signed_by "$2" "$3" "${keys[@]}" \
      || fail "$1 is signed by a release key that the update to $half brings, and that update was stopped before its end: run Surogate Desktop's install script with --version $half first"
  fi
  fail "$1 is not signed by Surogate's release key"
}

# The state schema of what the installed version keeps in each user's home, as its mark names it:
# that version's manifest as an apply copied it (one_object), in a file of root's own at the mode
# an apply gives it. Which releases read the installed one's state is this file's word: one that
# is a link, or another's to write, says nothing. Fails where the mark is not so, or names no
# schema.
installed_schema() {
  roots_own "$ROOT/current/release.json" 81a4 \
    && one_object "$ROOT/current/release.json" | jq -er '.stateSchema | select(type == "number" and . == floor and (floor | . >= 1 and . < 1e15)) | floor' 2>/dev/null
}

# Refuses release $2, whose signed manifest is $1, unless it can read what the installed version
# keeps in each user's home: its state schema is the installed one's or later.
reads_state() {
  local installed schema now
  installed="$(installed_version)"
  [ -n "$installed" ] || fail "Surogate Desktop is not installed: run its install script first"
  now="$(installed_schema)" || fail "the installed $installed names no state schema: run Surogate Desktop's install script again"
  schema="$(jq -r '.stateSchema | floor' "$1")"
  [ "$schema" -ge "$now" ] \
    || fail "$2 cannot read what the installed $installed keeps for its users: its state schema is $schema, and $installed's $now"
}

# Whether a process runs from version folder $1: its program is in it.
in_use() {
  local exe
  for exe in /proc/[0-9]*/exe; do
    [[ "$(readlink "$exe" 2>/dev/null)" == "$1"/* ]] && return 0
  done
  return 1
}

# Whether version folder $2 is whole, for manifest $1: its mark is that manifest, in a file of
# root's own at the mode an apply gives it, and the app and its helper are there to run, each a
# file of the folder's own and no link. A mark that is a link, or another's to write, is the word
# of no one for what the version keeps (installed_schema): its folder is unpacked again, by the
# install script that such a mark's refusal names. Nothing else in the folder is looked at, where
# only root writes: a version damaged elsewhere is taken as it is.
whole() {
  cmp -s "$1" "$2/release.json" && roots_own "$2/release.json" 81a4 && [ -f "$2/surogate" ] && [ ! -L "$2/surogate" ] && [ -x "$2/surogate" ] \
    && [ -f "$2/bin/surogate-apply-update" ] && [ ! -L "$2/bin/surogate-apply-update" ] && [ -x "$2/bin/surogate-apply-update" ]
}

# /opt/surogate is a folder of the computer's own, or a disk mounted there, and never a link: through
# one, root would make another folder its own and work in it, would find no app running from the
# tree, and at a removal would take the link away and leave all that it names, or say of a disk
# behind it that it was emptied. Refused before anything follows it; $1 is what to do then.
unlinked() {
  [ ! -L "$ROOT" ] || fail "$ROOT is a link, where Surogate Desktop keeps a folder of its own or a disk mounted there: ${1:-remove the link, and run this again}"
}

# One install, update or removal at a time, by a lock on a file in a folder only root can open: any
# user may open what all may read, hold a lock on it, and so stop every update. The folder is under
# /run and not in /opt/surogate: a removal takes the tree away, and an apply that waited for it
# would hold a lock on a folder that is gone. Root makes the folder itself, closed to everyone else
# from its first moment. One that is there already is used only when it is as root makes it: a
# folder, root's, with nothing for anyone else, and no link. Any other is refused and never taken
# over: what someone else could have put in it would still be there. Its kind, its mode and its
# owner are read as numbers alone (41c0: a folder, at 0700; and 0, root's): a tool's word for a
# folder is another word in another language. Where nothing is there and no folder can be made,
# what is wrong is the folder it would be in, and that is what is said. The lock is waited for
# LOCK_WAIT at most, and held until this script ends or closes it.
lock() {
  if ! mkdir -m 0700 "$LOCKS" 2>/dev/null && [ ! -e "$LOCKS" ] && [ ! -L "$LOCKS" ]; then
    local within="${LOCKS%/*}" why
    why="is $within full?"
    [ -w "$within" ] || why="$within is read-only"
    [ -d "$within" ] || why="$within is no folder"
    [ -e "$within" ] || [ -L "$within" ] || why="$within is missing"
    fail "$LOCKS, the folder of its lock, could not be made: $why"
  fi
  [ "$(stat -c '%f %u' -- "$LOCKS" 2>/dev/null)" = "41c0 0" ] \
    || fail "$LOCKS must be a folder of root's own that no one else opens (mode 700), and no link: remove what is there, and run this again"
  exec 9>>"$LOCKS/lock"
  flock -w "$LOCK_WAIT" 9 || fail "another install or update of Surogate Desktop is still running: try again once it has finished"
}

# The user the helper was run for, who reads the files it is handed: pkexec's caller, or sudo's.
# Each names that user by number in the helper's environment, and sets it itself, whatever its own
# caller's environment held. Nothing else is asked who it was. Naming a user only ever lowers the
# helper's rights to read, from root's to that user's.
asker() {
  local name uid gid entry reads_as groups listed
  for name in PKEXEC_UID SUDO_UID; do
    uid="${!name:-}"
    [ -n "$uid" ] || continue
    [[ "$uid" =~ ^(0|[1-9][0-9]{0,9})$ ]] || fail "$name is not a user's number"
    # Compared as it is written: what is no number is then never taken for root's.
    [ "$uid" != 0 ] || continue
    entry="$(getent passwd "$uid")" && gid="$(cut -d: -f4 <<<"$entry")" && [[ "$gid" =~ ^[0-9]+$ ]] || fail "$name names no user of this computer"
    READER=("$uid" "$gid" "${entry%%:*}")
    # The reader is that user, and no other: setpriv takes digits for a user's name where one is so
    # named, and counts a number past the last one from 0 again. Asked in two commands, as wherever
    # this script would put two $( ) in one: a signal that comes while the first is answered ends
    # Ubuntu 24.04's bash with an error of its own, before this script's handler has run.
    reads_as="$(as_reader "$SMALL_WAIT" id -u 2>/dev/null)" || reads_as=
    reads_as+=":$(as_reader "$SMALL_WAIT" id -g 2>/dev/null)" || reads_as=
    [ "$reads_as" = "$uid:$gid" ] || fail "$name names no user of this computer"
    # And the reader has no group but those the system's own list gives a user of that name, as
    # root reads the list: the reader's other groups are looked up by the name its number has, and
    # a number under another user's name would have that user's. Each group the reader has is
    # looked for among them: one that is not there is one it gained.
    groups="$(as_reader "$SMALL_WAIT" id -G 2>/dev/null)" || groups=
    listed=" $(id -G -- "${READER[2]}" 2>/dev/null) " || listed=
    [ -n "$groups" ] || fail "$name names no user of this computer"
    for gid in $groups; do
      [[ "$listed" == *" $gid "* ]] || fail "$name names no user of this computer"
    done
    return 0
  done
}

# Runs what follows $1 as the user who reads an apply's files, as that user's own login would run
# it: as that user, in their own group, and in the other groups the system's own list gives them
# (--init-groups), which is root's word and no more than the user's own rights. In their own group
# alone, a user who reaches their cache home only as a member of another, as under a folder that a
# department's group alone may enter, could read the update themselves and never have it applied.
# Never in a group of root's, which the helper's own are, and never in one its caller names. With
# none of the helper's open files, and for $1 seconds at most: a filesystem of the user's own may
# never answer, and the user can stop what runs as them. The command alone is killed then
# (--foreground): GNU's timeout otherwise kills itself with it, and bash says so in words of its
# own.
as_reader() {
  timeout --foreground -s KILL "$1" setpriv --reuid "${READER[0]}" --regid "${READER[1]}" --init-groups "${@:2}" 9<&- </dev/null
}

# Refuses file $1, which the reader could not read as a file. Root never looks at a file it is
# handed, so nothing is said of what the file is: where the reader is another user, only that it
# is not theirs to read, and what makes one so. A name that is no whole path is looked for where
# the helper was started, which under pkexec is root's home.
unread() {
  local file reader
  file="$(named "$1")"
  [ "${READER[0]}" -ne 0 ] || fail "$file is not a downloaded release's file"
  reader="$(named "${READER[2]}")"
  fail "$file cannot be read by $reader: name it by its whole path, in a folder of that user's own"
}

# Copies file $1, which an apply was handed, to $2 in root's staging: read once, as the user who
# asked, for $4 seconds at most, never through a link and never waiting on a pipe, whatever it has
# become since it was named. A pipe gives an empty copy at once. No more than $3 bytes and one are
# copied, whatever the file holds: root's end of the pipe counts them, and closes it. The reader
# counts nothing: a count of its own would bound only a reader that kept to it. Whether the file
# held no more than $3.
taken() {
  local file="$1" copy="$2" most="$3" wait="$4" ends
  as_reader "$wait" dd if="$file" iflag=nofollow,nonblock bs=64K status=none 2>/dev/null \
    | head -c "$(( most + 1 ))" 2>/dev/null >"$copy" && ends=(0 0) || ends=("${PIPESTATUS[@]}")
  # Root's own end of the pipe failed: the disk's fault, and not the file's.
  [ "${ends[1]}" -eq 0 ] || fail "$ROOT/staging could not be written: is its disk full?"
  # More than its own bytes: the pipe then closed on the reader, and how it ended says nothing.
  [ "$(stat -c %s "$copy")" -le "$most" ] || return 1
  [ "${ends[0]}" -eq 0 ] || unread "$file"
}

# Applies a release as root: its manifest $1, signature $2 and tarball $3, which the user who
# downloaded them can still change, so each is read once, as that user, into root's staging, and
# only the copies are checked and used. The tree is extracted in staging, refused when anything in
# it is not a plain file, folder or link inside it, moved into versions/<version> with its own copy
# of bwrap, and /opt/surogate/current is switched to it by one rename. Its helper is the one
# pkexec runs from just before that, unless this computer has installed a newer release: that
# one's helper stays, and the release keys it lists with it. A helper's pair that an earlier apply
# left half done is finished first (paired), and stays finished where this apply is then refused:
# the one change that a refused apply leaves. An older version than the installed one is refused,
# unless $4 is "older", as only an administrator's --version asks; it is then refused when it
# cannot read what the installed one keeps for its users. The previous version is kept, and older
# ones not running are removed, by an update; a repair removes none. A version that is here whole
# is repaired as it is, and its tarball is not read: $3 may then be empty.
apply() {
  local manifest="$1" signature="$2" tarball="$3" older="${4:-}" file
  # A folder or a missing file is refused here; a link, as each is copied, below.
  for file in "$manifest" "$signature" ${tarball:+"$tarball"}; do
    as_reader "$SMALL_WAIT" test -f "$file" || unread "$file"
  done
  # Before the tree is touched: a removal that runs now takes it away, and this apply makes it again.
  lock
  unlinked
  # Its folders are root's own, whoever made them. The tree's first: from then on no one else puts
  # anything in it. Then the three in it, each a folder of the tree's own and never a link, which
  # would have root make another folder its own, and work there. Each loses the set-id bits a
  # folder takes from the one it is made in, as under an /opt that hands its group on: a mode in
  # digits keeps them, and all that is unpacked below would take them too, and be refused for it.
  mkdir -p "$ROOT"
  chown 0:0 "$ROOT"
  chmod u=rwx,go=rx,a-st "$ROOT"
  local inner
  for inner in versions bin staging; do
    [ ! -L "$ROOT/$inner" ] || fail "$ROOT/$inner is a link, where Surogate Desktop keeps a folder of its own: remove it, and run this again"
  done
  mkdir -p "$ROOT/versions" "$ROOT/bin"
  # Root's alone from its first moment: what an apply copies and unpacks is in it.
  mkdir -p -m 0700 "$ROOT/staging"
  chown 0:0 "$ROOT/versions" "$ROOT/bin" "$ROOT/staging"
  chmod u=rwx,go=rx,a-st "$ROOT/versions" "$ROOT/bin"
  chmod u=rwx,go=,a-st "$ROOT/staging"
  # What an apply that was killed left in staging goes, before any room is measured. This one's
  # own folder goes however it ends.
  find "$ROOT/staging" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  local work
  scratch work "$ROOT/staging/apply.XXXXXX"
  # Before anything is read or checked: the keys asked below are those of the release that the
  # helper's mark names, where an apply was stopped between the two.
  paired "$work"
  # A manifest is a line, and its signature Ed25519's 64 bytes.
  taken "$manifest" "$work/manifest.json" 4096 "$SMALL_WAIT" || fail "$(named "$manifest") is not a downloaded release's file"
  taken "$signature" "$work/manifest.json.sig" 64 "$SMALL_WAIT" || fail "$(named "$signature") is not a downloaded release's file"

  signed "$work/manifest.json" "$work/manifest.json.sig" || fail "the release's manifest is not signed by Surogate's release key"
  local release version sha256 size
  release="$(release_of "$work/manifest.json")" || fail "the release's manifest is not a release of Surogate Desktop for this computer"
  read -r version sha256 size <<<"$release"
  local previous
  previous="$(installed_version)"
  if [ -n "$previous" ] && [ "$older" != older ] && dpkg --compare-versions "$version" lt "$previous"; then
    fail "$version is older than the installed $previous"
  fi
  # A rollback's state schema is compared here, with the lock held: the version it was compared
  # with before the lock may have been updated since.
  [ "$older" != older ] || reads_state "$work/manifest.json" "$version"
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
    taken "$tarball" "$work/release.tar.gz" "$size" "$READ_WAIT" && [ "$(stat -c %s "$work/release.tar.gz")" -eq "$size" ] \
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
    # No link names a whole path, climbs out of the tree as it is written, or leaves it as it
    # resolves. A whole path is refused in words of its own: it leaves the tree as it resolves too,
    # wherever it leads, so that those words alone would never be this check's.
    while IFS= read -r -d '' link; do
      target="$(readlink "$link")"
      [[ "$target" != /* ]] || fail "the release's archive holds a link to a whole path: $(named "${link#"$top"/}")"
      [[ "$(realpath -ms "${link%/*}/$target")" == "$top"/* ]] && [[ "$(realpath -m "$link" 2>/dev/null)" == "$top"/* ]] \
        || fail "the release's archive links outside itself: $(named "${link#"$top"/}")"
    done < <(find "$top" -type l -print0)
    # The app and its helper are programs, no folders and no links, and bin a folder of the tree's
    # own, where this version's bwrap goes.
    [ -f "$top/surogate" ] && [ ! -L "$top/surogate" ] && [ -x "$top/surogate" ] && [ -d "$top/bin" ] && [ ! -L "$top/bin" ] \
      && [ -f "$top/bin/surogate-apply-update" ] && [ ! -L "$top/bin/surogate-apply-update" ] && [ -x "$top/bin/surogate-apply-update" ] \
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
  # The helper pkexec runs, and the release keys it lists, are the newest release's that this
  # computer has installed, and never go back: its mark says which release that is. An older
  # release, as a rollback applies one, a later update that is still below the newest, and a repair
  # after either, each leave the helper and its mark as they are.
  local newest keep=""
  newest="$(helper_release)" || fail "$HELPER_MARK does not say which release $HELPER is of: remove Surogate Desktop with --uninstall, and install it again"
  # On a computer with no helper, its mark is not read: this release's is put in its place, by
  # a rename. Nothing but a file may stand there then. A folder would stop that rename, with the
  # version's folder in its place already and its helper not; and a link or a pipe is no mark
  # that an apply left.
  if [ -L "$HELPER_MARK" ] || { [ -e "$HELPER_MARK" ] && [ ! -f "$HELPER_MARK" ]; }; then
    fail "$HELPER_MARK is not as Surogate Desktop's install leaves it: remove Surogate Desktop with --uninstall, and install it again"
  fi
  if [ -n "$newest" ] && dpkg --compare-versions "$version" lt "$newest"; then keep=1; fi
  if [ -z "$keep" ]; then
    install -m 0755 "${top:-$folder}/bin/surogate-apply-update" "$work/helper"
    install -m 0644 "$work/manifest.json" "$work/helper.json"
  fi
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
  # The helper pkexec runs, at a path with no link in it: polkit 127 (Ubuntu 26.04) matches an
  # action's exec.path against the program's resolved path, polkit 124 (24.04) against the path given.
  # Its mark first: stopped between the two, the helper is still the release's before, under a
  # mark that no older release passes, and the next apply puts the mark's own helper in before
  # anything else (paired). With the helper first, its mark would name an older release than it
  # is of, and a release between the two could then take its place.
  if [ -z "$keep" ]; then
    mv -T "$work/helper.json" "$HELPER_MARK"
    mv -T "$work/helper" "$HELPER"
  fi
  # And current last: a version that is installed has its helper. So one with none is no first
  # install that was stopped half way, and an update is not installed before the keys it brings
  # are the ones this computer trusts.
  mv -T "$work/current" "$ROOT/current"
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
# virtiofsd, and uidmap's newuidmap and newgidmap for the VM; zstd for its image; pkexec, which its
# updates run the root helper with; and what this script runs itself.
packages() {
  say "installing the packages it needs"
  export DEBIAN_FRONTEND=noninteractive
  # A package source of the computer's own that fails stops nothing: what is needed may be known already.
  apt-get update -qq || say "apt-get update failed for a package source of this computer's: installing from what apt knows already"
  # Soon after a desktop's first boot, its unattended upgrades hold dpkg's lock for a while.
  apt-get install -y -qq -o DPkg::Lock::Timeout=300 bubblewrap socat ripgrep virtiofsd uidmap zstd pkexec openssl jq curl desktop-file-utils \
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
  local base="$1" download release version size installed tarball=""
  # In /tmp, whatever TMPDIR root's own shell has: /tmp is root's, and no one there renames what
  # is another's. Whoever owns a folder that TMPDIR named could put one of their own in this
  # one's name, and root's downloads would be written through whatever stood in it.
  scratch download -p /tmp tmp.XXXXXXXXXX
  # curl reads its address's letters as UTF-8 (C.UTF-8, which has no language of its own): a
  # server's name may have letters outside ASCII, as the name the user fetched this script from
  # may, and in a locale without them curl refuses the name before it looks it up.
  # No download is longer than what it is for, whatever its server sends, into root's /tmp, which
  # may be memory: a manifest is a line of 4096 bytes at most, as an apply takes one; its signature
  # is Ed25519's 64 bytes, and one more shows one that is too long; and a tarball is the size its
  # signed manifest names.
  LC_ALL=C.UTF-8 curl -q -fsSL --proto '=https,http' --max-filesize 4096 "${TIMELY[@]}" -o "$download/manifest.json" "$base/desktop/latest.json" \
    || fail "could not download $base/desktop/latest.json"
  LC_ALL=C.UTF-8 curl -q -fsSL --proto '=https,http' --max-filesize 65 "${TIMELY[@]}" -o "$download/manifest.json.sig" "$base/desktop/latest.json.sig" \
    || fail "could not download $base/desktop/latest.json.sig"
  signed "$download/manifest.json" "$download/manifest.json.sig" \
    || unsigned "$base/desktop/latest.json" "$download/manifest.json" "$download/manifest.json.sig"
  release="$(release_of "$download/manifest.json")" \
    || fail "$base/desktop/latest.json is not a release of Surogate Desktop for this computer"
  read -r version _ size <<<"$release"
  installed="$(installed_version)"
  if [ -n "$installed" ] && dpkg --compare-versions "$version" lt "$installed"; then
    say "kept the installed $installed, newer than the server's $version"
    return 0
  fi
  # A version that is here whole is not downloaded again: apply repairs it as it is.
  if ! whole "$download/manifest.json" "$ROOT/versions/$version"; then
    say "downloading Surogate Desktop $version"
    tarball="$download/release.tar.gz"
    LC_ALL=C.UTF-8 curl -q -fSL --proto '=https,http' --max-filesize "$size" "${TIMELY[@]}" -o "$tarball" "$base/desktop/$(jq -r .url "$download/manifest.json")" \
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

# Release $1 from the base the install record names, by its own signed manifest, applied even when
# it is older than the installed one: an administrator's rollback. A version that is still here
# whole is switched to as it is, and only its manifest and signature are asked of the base; any
# other is downloaded again, no more of it than its manifest's size. Refused when a release key
# this computer trusts now did not sign it, and when it cannot read what the installed version
# keeps in its users' homes, as its state schema says.
roll_back() {
  local version="$1" base installed download release its size tarball=""
  # The record is root's own word for the server this computer installs from: a file of root's
  # own that no one else may write, at whichever mode (an administrator may have closed it to
  # others, and it is root's word no less), one JSON document of 4096 bytes at most, and its base
  # an http or https URL as --base takes one. curl is handed no other word of it: one that begins
  # with a dash would be options to curl, and name it a file to read and one to write.
  [ -e "$RECORD" ] || [ -L "$RECORD" ] || fail "Surogate Desktop is not installed: run its install script first"
  roots_alone "$RECORD" || fail "$RECORD is not as Surogate Desktop's install leaves it: run its install script again"
  base="$(one_object "$RECORD" any | jq -er '.base | strings' 2>/dev/null)" && http_url "$base" \
    || fail "$RECORD names no server to roll back from: run Surogate Desktop's install script again"
  installed="$(installed_version)"
  [ -n "$installed" ] || fail "Surogate Desktop is not installed: run its install script first"
  # Before anything is asked of the base: where what the installed version keeps is not known, no
  # release is one that reads it.
  installed_schema >/dev/null || fail "the installed $installed names no state schema: run Surogate Desktop's install script again"
  # Its folder in /tmp, as the install's is, whatever TMPDIR root's own shell has.
  scratch download -p /tmp tmp.XXXXXXXXXX
  # Each download as the install's own (install_latest): its address's letters read as UTF-8, no
  # more of it than it is for, a manifest's 4096 bytes and a signature's 64 and one more, and for
  # no longer than a download may wait.
  LC_ALL=C.UTF-8 curl -q -fsSL --proto '=https,http' --max-filesize 4096 "${TIMELY[@]}" -o "$download/manifest.json" "$base/desktop/releases/$version/manifest.json" \
    || fail "could not download $base/desktop/releases/$version/manifest.json"
  LC_ALL=C.UTF-8 curl -q -fsSL --proto '=https,http' --max-filesize 65 "${TIMELY[@]}" -o "$download/manifest.json.sig" "$base/desktop/releases/$version/manifest.json.sig" \
    || fail "could not download $base/desktop/releases/$version/manifest.json.sig"
  signed "$download/manifest.json" "$download/manifest.json.sig" \
    || unsigned "$base/desktop/releases/$version/manifest.json" "$download/manifest.json" "$download/manifest.json.sig" "$version"
  release="$(release_of "$download/manifest.json")" && read -r its _ size <<<"$release" && [ "$its" = "$version" ] \
    || fail "$base/desktop/releases/$version/manifest.json is not release $version of Surogate Desktop for this computer"
  # Before its tarball is asked for; apply compares them again, with the lock held.
  reads_state "$download/manifest.json" "$version"
  # A version that is here whole is not downloaded again: apply takes it as it is.
  if ! whole "$download/manifest.json" "$ROOT/versions/$version"; then
    say "downloading Surogate Desktop $version"
    tarball="$download/release.tar.gz"
    LC_ALL=C.UTF-8 curl -q -fSL --proto '=https,http' --max-filesize "$size" "${TIMELY[@]}" -o "$tarball" "$base/desktop/releases/$version/surogate-desktop-$version-linux-x64.tar.gz" \
      || fail "could not download Surogate Desktop $version from $base"
  fi
  apply "$download/manifest.json" "$download/manifest.json.sig" "$tarball" older
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

# Whether $1 is an http or https URL with no white space in it, read byte for byte in no locale of
# its caller's: what is a base is then the same for whoever runs the script, and for root's part
# of it. In most locales, more characters than ASCII's six are white space, and bytes that are no
# characters match nothing.
http_url() {
  local LC_ALL=C
  [[ "$1" =~ ^https?://[^[:space:]]+$ ]]
}

# Whether $1 is a version, x.y.z in the ten digits, read in no locale of its caller's as a base is:
# in most locales, more than ten characters are digits. No part has a zero before it, as none has
# in a manifest (release_of).
a_version() {
  local LC_ALL=C
  [[ "$1" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]
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
  # The two folders as this script says a name, each in a command of its own.
  local in_data in_cache
  in_data="$(named "$data/surogate")"
  in_cache="$(named "$cache/surogate")"
  # Asked on the terminal, as this script's input is itself; with none to ask on, the data stays.
  if (exec </dev/tty) 2>/dev/null; then
    read -r -p "Surogate Desktop: also delete $user's sign-in, device token and browser profiles, in $in_data? Chat folders stay. [y/N] " answer </dev/tty || answer=
  fi
  if [[ "$answer" == [Yy]* ]]; then
    runuser -u "$user" -- rm -rf -- "$data/surogate" "$cache/surogate" 2>/dev/null \
      || fail "could not delete all of $user's app data: what $user may not change stays, in $in_data and $in_cache"
    say "deleted $user's app data"
  else
    say "kept $user's app data, in $in_data"
  fi
}

main() {
  set -Eeuo pipefail
  umask 022
  # As root, it reads what the system's tools say, and has them read what they are handed, in no
  # locale and no language of its caller's, which sudo and pkexec both pass on: in another
  # language a tool's words are other words; in most locales, more than ten characters are digits;
  # and a name's other letters are written as they are. LANG too: Ubuntu 26.04's own tools take
  # their language from it, whatever LC_ALL names. And LANGUAGE goes: some take theirs from it
  # before any locale.
  [ "$EUID" -ne 0 ] || { export LC_ALL=C LANG=C; unset LANGUAGE; }
  settings
  trap cleanup EXIT
  stoppable
  # Where a failure ends the script (-e), and not inside a $( ): there, its caller decides.
  trap '[ "$BASH_SUBSHELL" -gt 0 ] || unexpected "$BASH_COMMAND"' ERR
  case "${1:-}" in
    --apply)
      # The helper runs the system's own tools, wherever its caller's PATH points.
      export PATH=/usr/sbin:/usr/bin:/sbin:/bin
      [ "$#" -eq 4 ] && [ -n "$4" ] || fail "usage: surogate-apply-update --apply <manifest> <signature> <tarball>"
      [ "$EUID" -eq 0 ] || fail "applying a release needs administrator rights"
      supported
      asker
      apply "$2" "$3" "$4"
      ;;
    --base | "")
      # What installs as root is the system's own tools, wherever its caller's PATH points, from
      # before it runs the first of them, as an apply's are: an openssl of another's there would
      # call any release signed.
      [ "$EUID" -ne 0 ] || export PATH=/usr/sbin:/usr/bin:/sbin:/bin
      local base=https://surogate.ai
      if [ "${1:-}" = --base ]; then
        [ "$#" -eq 2 ] && http_url "$2" || fail "usage: install.sh --base <http or https URL>"
        base="${2%/}"
      fi
      supported
      unlinked
      if [ "$EUID" -ne 0 ]; then
        say "installing needs administrator rights: sudo asks for your password once"
        # Again as root, from this script's own functions: a script piped to bash has no file to name.
        # sudo resets the environment, so the proxy the user's shell names goes with them. The shell
        # that reads them is named by its whole path: a sudo with no secure_path looks for it on
        # its caller's PATH.
        { declare -f; declare -p http_proxy https_proxy HTTPS_PROXY all_proxy ALL_PROXY no_proxy NO_PROXY 2>/dev/null || true; echo 'main "$@"'; } \
          | sudo -- /bin/bash -s -- "$@" || exit "$?"
        return
      fi
      install_all "$base"
      ;;
    --version)
      # What rolls back as root is the system's own tools, wherever its caller's PATH points, from
      # before it runs the first of them, as an apply's are.
      [ "$EUID" -ne 0 ] || export PATH=/usr/sbin:/usr/bin:/sbin:/bin
      [ "$#" -eq 2 ] && a_version "$2" || fail "usage: install.sh --version <x.y.z>"
      supported
      unlinked
      if [ "$EUID" -ne 0 ]; then
        say "rolling back needs administrator rights: sudo asks for your password once"
        { declare -f; declare -p http_proxy https_proxy HTTPS_PROXY all_proxy ALL_PROXY no_proxy NO_PROXY 2>/dev/null || true; echo 'main "$@"'; } \
          | sudo -- /bin/bash -s -- "$@" || exit "$?"
        return
      fi
      roll_back "$2"
      ;;
    --uninstall)
      unlinked "nothing was removed. Remove the link, and run this again: what it names is then yours to remove"
      if [ "$EUID" -ne 0 ]; then
        # Nothing may stand after it: an argument this script does not know is refused before
        # sudo is asked, never dropped and the app removed all the same.
        [ "$#" -eq 1 ] || fail "usage: install.sh --uninstall"
        say "removing it needs administrator rights: sudo asks for your password once"
        # The user's own folders go with it, as their session names them: sudo resets the environment.
        local config data cache
        config="$(xdg "${XDG_CONFIG_HOME:-}" "$HOME/.config")"
        data="$(xdg "${XDG_DATA_HOME:-}" "$HOME/.local/share")"
        cache="$(xdg "${XDG_CACHE_HOME:-}" "$HOME/.cache")"
        { declare -f; echo 'main "$@"'; } | sudo -- /bin/bash -s -- --uninstall "$config" "$data" "$cache" || exit "$?"
        return
      fi
      # What removes it is the system's own tools, wherever its caller's PATH points.
      export PATH=/usr/sbin:/usr/bin:/sbin:/bin
      [ "$#" -eq 1 ] || [ "$#" -eq 4 ] || fail "usage: install.sh --uninstall"
      shift
      uninstall "$@"
      ;;
    *)
      fail "usage: install.sh [--base <url>] [--version <x.y.z>] [--uninstall]"
      ;;
  esac
}

main "$@"
