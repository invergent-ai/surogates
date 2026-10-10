#!/bin/bash -p
# Surogate Desktop's install script, and each version's bin/surogate-apply-update, the root
# helper that applies a verified release: the copy at /opt/surogate/bin/surogate-apply-update is
# the one pkexec runs, the newest release's that this computer has installed.
#
#   curl -fsSL https://surogate.ai/desktop/install.sh | bash                      install, update or repair
#   curl -fsSL https://surogate.ai/desktop/install.sh | bash -s -- --uninstall    remove it
#   install.sh --base <url>                                   install from another server (an enterprise's)
#   install.sh --ca-cert <file>                               trust the company's CA, a PEM file, in every user's app
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
  # The company's certificate authority, where an administrator gave one with --ca-cert: every
  # user's app trusts it at its start, and so do this script's own downloads.
  COMPANY_CA=/etc/surogate/ca.pem
  # The one this run was given, checked and not kept yet: its downloads are made with it first.
  GIVEN_CA=
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
  # How long an apply that has put its release in place waits for what it started as root to end,
  # before it says that the release is installed (settled).
  LEFT_WAIT=30
  # How long a download from the base may wait, as curl is told: 30 seconds to be connected, and a
  # minute at under 1024 bytes a second. A base that takes the connection and never answers, or
  # stops in the middle of an answer, would otherwise hold an install or a rollback for good.
  TIMELY=(--connect-timeout 30 --speed-limit 1024 --speed-time 60)
  # Who reads the files an apply is handed, by user and group number, by name, and by the numbers
  # of all its groups: root, unless the helper was run for another user (asker).
  READER=(0 0 root 0)
  # How a command is run as that reader: perl's own line, handed the user's number, their group's
  # and the numbers of all their groups, then the command. The groups are set first, as numbers
  # (perl writes them from the list it is given, and looks no name up: setpriv reads each as a
  # group's name before it reads it as a number); then the group and the user, each in all three
  # of a process's numbers, so that nothing of root's is kept to go back to; then the command in
  # the process's place. 126 where one could not be set, and 127 for a command that is not there.
  AS_READER='use POSIX (); ($u, $g, $l) = splice(@ARGV, 0, 3); $l =~ tr/,/ /; $) = join(q( ), $g, $l); POSIX::setgid($g) && POSIX::setuid($u) or exit 126; exec { $ARGV[0] } @ARGV; exit 127'
  # How a program is started in a user's home that runs what that user chooses, as certutil runs
  # the modules their own database names: perl's own line again, run as that user (as_reader) and
  # handed their home, then the program. With nothing of root's shell: no descriptor but the three
  # standard ones, the root folder to start in, a session of its own with no terminal, and an
  # environment of three names, as the app starts certutil (src/shell/company-ca.ts). 126 where one
  # could not be set, and 127 for a program that is not there.
  AS_THEIRS='use POSIX (); $home = shift; opendir($dir, "/proc/self/fd") or exit 126; @open = grep { /^\d+$/ && $_ > 2 } readdir($dir); closedir($dir); POSIX::close($_) for @open; chdir("/") && POSIX::setsid() > 0 or exit 126; %ENV = (PATH => "/usr/bin:/bin", LC_ALL => "C", HOME => $home); exec { $ARGV[0] } @ARGV; exit 127'
  # What is said of a base with a user or a password in it. The base is written into the install
  # record, which every user of the computer reads and the app reads its base from: a password
  # there would be every user's, and the app takes no base that has one.
  CREDENTIALS="a base with a user or a password in it is not taken: it would be written where every user of this computer reads it. Name the server alone"
  # Folders of this run's own, which go however it ends.
  OWN=()
  # The release keys' public halves: a release's manifest is signed by the private half of one of
  # them (Ed25519). A rotation lists the old key and the new for one release, which the old signs.
  # On a computer that has a helper, the helper's list is the one that counts, and not this one.
  #
  # The list has one form, and what is not in it is no list to any reader: to this script's own
  # (listed), which reads a helper's list and never runs it; to the release job, which writes and
  # signs no manifest for a script whose list reads otherwise (publish.sh); and to the app
  # (releaseKeys in src/shell/updates.ts). Bash would take more, and a list that bash alone
  # reads gives a helper that lists no key: a computer that installed it takes no later release.
  # The form, line by line:
  # - one line that opens the list, blanks and then the list's name, an equals sign and an
  #   opening bracket, and nothing after; the script assigns the list nowhere else, and adds to
  #   it nowhere;
  # - each key as OpenSSL writes an Ed25519 public key (openssl pkey -pubout): a line of blanks,
  #   a single quote and the BEGIN line; its one line of base64 from the line's start, 60
  #   letters; and the END line from the line's start, with a single quote and nothing after;
  # - one line that closes it, blanks and a closing bracket.
  # No comment, no empty line and no other quoting in it, and at least one key.
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

# Ends a run that would write or remove in the folder of the install record and the company's CA
# while that folder is not root's own: a folder and no link, root's, with no write bit for its
# group or for others. In one that is another's to write, that other's link would stand where root
# writes a file, and root would write through it. Only root can have given the folder away, and
# only root gives it back. A folder that is not there yet is root's when this script makes it.
# $1: what the run did not do.
roots_folder() {
  local folder seen mode
  folder="$(dirname "$RECORD")"
  [ -e "$folder" ] || [ -L "$folder" ] || return 0
  seen="$(stat -c '%f %u' -- "$folder" 2>/dev/null)" || seen="0 x"
  mode=$(( 16#${seen% *} ))
  [ "${seen#* }" = 0 ] && (( (mode & 0170000) == 0040000 && (mode & 0022) == 0 )) \
    || fail "$folder must be a folder of root's own that no one else may write, and no link: $1. Give it back to root (sudo chown root:root $folder && sudo chmod 755 $folder), look at what it holds, and run this again"
}

# Whether $1 is a program of root's own that no one else may write and that others may read: a
# file and no link, root's, with no write bit for its group or for others, one that someone may
# run, and one that others may read. The helper pkexec runs is asked so, by this script and by
# the app (rootsOwn in src/vm/image.ts), which offers an update only by a helper that this takes.
# The app runs as its user and reads its release keys from this file: one closed to that user is
# a helper no app can offer an update by, and is none to this script either, so that the two
# never answer otherwise; an install puts it back as it leaves one. A set-id or a sticky bit on
# it is not looked at, here or there: the kernel runs no script as its file's owner, pkexec runs
# this one as root whatever its bits, and no one but root may write it.
roots_program() {
  local seen mode
  seen="$(stat -c '%f %u' -- "$1" 2>/dev/null)" || return 1
  [ "${seen#* }" = 0 ] || return 1
  mode=$(( 16#${seen% *} ))
  (( (mode & 0170000) == 0100000 && (mode & 0022) == 0 && (mode & 0111) != 0 && (mode & 0004) != 0 ))
}

# The release keys this computer trusts, into the array $1 names. One file says which: the helper
# pkexec runs, whose own list they are, whichever script asks, that helper or an install script
# of any age. Its list is read where it is in the list's one form (listed), and the helper is not
# run. Only a computer with no helper and no version, at its first install, takes
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
  roots_program "$HELPER" || fail "$HELPER is not as Surogate Desktop's install leaves it: remove Surogate Desktop with --uninstall, and install it again"
  listed list "$HELPER"
  [ "${#list[@]}" -gt 0 ] || fail "$HELPER lists no release key: remove Surogate Desktop with --uninstall, and install it again"
}

# The release keys that helper $2 lists, into the array $1 names: each entry of its list, where
# the list is in its one form (see the list, in settings), and none where it is not, or where the
# script assigns the list a second time or adds to it. The helper is read, and not run: grep
# counts the lines that assign the list, sed takes the list's own lines, from the one that opens
# it to the first that ends in a bracket, and those few are asked line by line, letter for
# letter, in no locale of the caller's: whether that last line closes the list is asked there. A zero byte, which bash would drop from what sed hands it, is handed on
# as a byte that no line of the form holds: the line it is in is read as the app reads it.
listed() {
  local -n entries="$1"
  local LC_ALL=C name=RELEASE_KEYS lists text line entry="" at=before blank=$'^[ \t]*'
  # A key's first line and its last, as OpenSSL writes them. Put together here, so that the
  # script has the first only where a key is.
  local begin="-----BEGIN" end="-----END"
  begin+=" PUBLIC KEY-----" end+=" PUBLIC KEY-----"
  # A key's one line between them: an Ed25519 public key as OpenSSL writes one, which is twelve
  # bytes that say so and the key's 32, in base64, whose last letter holds four bits of a byte
  # and none of its own. OpenSSL reads more spellings of the same key, and keys of other kinds,
  # whose signatures no install takes: here a key has the one, and each entry handed on is a key.
  local ed25519='^MCowBQYDK2VwAyEA[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$'
  entries=()
  lists="$(grep -cE "^[[:blank:]]*${name}\\+?=" -- "$2" 2>/dev/null)" || lists=0
  [ "$lists" = 1 ] || return 0
  text="$(sed -n -e 's/\x00/\x01/g' -e "/^[[:blank:]]*${name}=($/,/)$/p" -- "$2" 2>/dev/null)" || return 0
  # The first line is the one that opens the list, as sed took it. A list that a line made wrong
  # stays wrong to its end, and no line follows the one that closes it.
  while IFS= read -r line; do
    case "$at" in
      before) at=open ;;
      open)
        if [[ "$line" =~ ${blank}\'"$begin"$ ]]; then entry="$begin"; at=key
        elif [[ "$line" =~ ${blank}\)$ ]]; then at=closed
        else at=wrong
        fi
        ;;
      key)
        if [ "$entry" = "$begin" ] && [[ "$line" =~ $ed25519 ]]; then entry+=$'\n'"$line"
        elif [ "$line" = "$end'" ] && [ "$entry" != "$begin" ]; then entries+=("$entry"$'\n'"$end"); at=open
        else at=wrong
        fi
        ;;
    esac
  done <<<"$text"
  [ "$at" = closed ] || entries=()
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
#
# And no field is named twice in any one object, whatever its two values. Of the two, jq keeps the
# last and says nothing of the first; another reader may keep either; and where the first is
# deeper than jq reads at all, one jq refuses the text and another, or another reader, drops it
# unread and takes the rest. So the text is asked first as jq streams it, each value under its
# path as it is read, before any field has replaced another: a path that was ended and comes
# again is a field named twice, as no place of a list is. The file is read twice for it; each
# caller's is root's own, or a copy in a folder of root's own.
one_object() {
  local most="${3:-4096}"
  case "$most" in 0* | *[!0123456789]*) return 1 ;; esac
  [ "${#most}" -le 7 ] && [ "$most" -le 1048576 ] || return 1
  head -c "$(( most + 1 ))" -- "$1" 2>/dev/null \
    | jq -n --stream '
      reduce inputs as $read ({};
        $read[0] as $path
        | if ($read | length) == 2
          then reduce range(1; ($path | length) + 1) as $steps (.; if has($path[:$steps] | tojson) then error("a field named twice") else . end)
            | .[$path | tojson] = 1
          else .[$path[:-1] | tojson] = 1 end)
      | empty' >/dev/null 2>&1 || return 1
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

# The version that the manifest $1 names, where it is one JSON object on one line (one_object) and
# its version a version: x.y.z in the ten digits, with no zero before a part. Nothing else of it
# is asked, and no key: it says where a release's own files are.
named_version() {
  one_object "$1" | jq -er '.version | select(type == "string" and test("\\A(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\z"))' 2>/dev/null
}

# The release that the helper's mark names. Fails where there is no mark of root's own that is a
# release's manifest as an apply copies one (one_object), and names a release.
marked() {
  roots_own "$HELPER_MARK" 81a4 && named_version "$HELPER_MARK"
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
  if cmp -s "$HELPER_MARK" "$ROOT/versions/$found/release.json" 2>/dev/null && ! cmp -s "$own" "$HELPER" 2>/dev/null; then return 0; fi
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
    if a_version "${other##*/}" && dpkg --compare-versions "${other##*/}" gt "$version" && cmp -s "$other/bin/surogate-apply-update" "$HELPER" 2>/dev/null; then
      fail "$refused"
    fi
  done
  install -m 0755 "$own" "$1/paired"
  sync -f "$1"
  mv -T "$1/paired" "$HELPER"
}

# Ends an install or a rollback whose release, $1 by its address, with manifest $2 and signature
# $3, no release key this computer trusts has signed (not_signed). Before any lock is held, the keys asked are
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
  not_signed "$1" "$2" "$3" "${4:+rollback}"
}

# Ends with the words for $1, a release's manifest by its address or by what it is, with manifest
# $2 and signature $3, that no release key this computer trusts has signed: an install's and an
# apply's alike, and the app's own line for one (updateLine in src/shell/updates.ts) says the
# same. No sentence here sends a person to remove Surogate Desktop and install it again: that
# throws away the keys this computer has, and what is not signed by them may be a forgery as well
# as a release from after a change of key. Each says what is so: nothing was installed, and the
# version that is here stays.
# - At a first install there is no helper, and the keys asked were this script's own.
# - A release that a key signed which this computer trusted once, and which a later release
#   dropped: found by the lists of the older versions that are still here, each root's own.
#   Their keys are asked for these words alone, and never for what is installed.
# - A rollback, $4: no more than that it is not signed. Which release an administrator asked for
#   is theirs to know.
# - Any other, of an install or an apply: a computer that never took the release which lists a
#   new key beside the old does not know the new one, and takes nothing the new one signs. The
#   one way on is said that this computer's own keys check: that release itself.
not_signed() {
  local other its newest keys
  [ -e "$HELPER" ] || [ -L "$HELPER" ] || fail "$1 is not signed by Surogate's release key"
  # Only the versions older than the release the helper is of, by its mark: a newer one that is
  # here is an update that did not end, and its keys are ones to come.
  newest="$(marked)" || newest=
  for other in "$ROOT"/versions/*/bin/surogate-apply-update; do
    its="${other#"$ROOT"/versions/}"
    its="${its%%/*}"
    [ -n "$newest" ] && a_version "$its" && dpkg --compare-versions "$its" lt "$newest" && roots_alone "$other" || continue
    listed keys "$other"
    ! signed_by "$2" "$3" "${keys[@]}" \
      || fail "$1 is signed by a release key that Surogate has retired, which this computer no longer trusts: nothing was installed, and Surogate Desktop stays at its version"
  done
  [ -z "${4:-}" ] || fail "$1 is not signed by a release key this computer trusts: nothing was installed, and Surogate Desktop stays at its version"
  fail "$1 is not signed by a release key this computer trusts: nothing was installed, and Surogate Desktop stays at its version. If Surogate's release key has changed since this computer's last update, run Surogate Desktop's install script with --version of the release that brought the new key"
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
  cmp -s "$1" "$2/release.json" 2>/dev/null && roots_own "$2/release.json" 81a4 && [ -f "$2/surogate" ] && [ ! -L "$2/surogate" ] && [ -x "$2/surogate" ] \
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
  local name uid gid entry number groups
  for name in PKEXEC_UID SUDO_UID; do
    uid="${!name:-}"
    [ -n "$uid" ] || continue
    [[ "$uid" =~ ^(0|[1-9][0-9]{0,9})$ ]] || fail "$name is not a user's number"
    # Compared as it is written: what is no number is then never taken for root's.
    [ "$uid" != 0 ] || continue
    # Root asks the system's own list three things, each once and each for a bounded while: who has
    # that number, what number that name has, and which groups it is in. A list that another
    # computer serves may take long to answer, or never answer: that is said as what it is, and
    # is no part of the bound on what the reader reads.
    entry="$(listing getent passwd "$uid")" || unlisted "$?" "$name"
    gid="$(cut -d: -f4 <<<"$entry")"
    [[ "$gid" =~ ^[0-9]+$ ]] || fail "$name names no user of this computer"
    # The name is that number's, and no other user's: the groups are looked up by the name, and
    # where two users have one name, the name's number is the first one's. The second would read
    # in the first's groups.
    # Where two names have one number, the list gives the first name for it, and the reader is in
    # that name's groups, whichever of the two asked: to the kernel the two are one user, and
    # each can already do as that user whatever the other can. So the second name's own
    # group-only folder is not read, and the first's is.
    number="$(listing id -u -- "${entry%%:*}")" || unlisted "$?" "$name"
    [ "$number" = "$uid" ] || fail "$name names no user of this computer"
    groups="$(listing id -G -- "${entry%%:*}")" || unlisted "$?" "$name"
    [[ "$groups" =~ ^[0-9]+(\ [0-9]+)*$ ]] || fail "$name names no user of this computer"
    READER=("$uid" "$gid" "${entry%%:*}" "${groups// /,}")
    # The reader is that user, and no other: a number past the last one is counted from 0 again
    # where it is set. Asked in two commands, as wherever
    # this script would put two $( ) in one: a signal that comes while the first is answered ends
    # Ubuntu 24.04's bash with an error of its own, before this script's handler has run.
    number="$(as_reader "$SMALL_WAIT" id -u 2>/dev/null)" || number=
    number+=":$(as_reader "$SMALL_WAIT" id -g 2>/dev/null)" || number=
    [ "$number" = "$uid:$gid" ] || fail "$name names no user of this computer"
    return 0
  done
}

# Asks the system's own list of users and groups what follows, for SMALL_WAIT seconds at most.
listing() {
  timeout --foreground -s KILL "$SMALL_WAIT" "$@" 2>/dev/null
}

# Whether what ended $1 was killed for its bound: 137 from GNU's timeout for what it killed, and
# 124 from Ubuntu 26.04's.
outlasted() {
  [ "$1" -eq 137 ] || [ "$1" -eq 124 ]
}

# Ends an apply whose question to that list ended $1: unanswered in its time, or answered that
# there is no such user, of the number that $2 names.
unlisted() {
  ! outlasted "$1" || fail "this computer's list of users and groups did not answer within $SMALL_WAIT seconds: try again"
  fail "$2 names no user of this computer"
}

# Runs what follows $1 as the user who reads an apply's files, as that user's own login would run
# it: as that user, in their own group, and in the other groups the system's own list gives them,
# which is root's word and no more than the user's own rights. Root asked the list for them once
# (asker), and hands them on as numbers, which are set as numbers (AS_READER, in settings): no
# question to that list is inside this bound, which is on what the reader reads, and a group
# that is named in digits is not taken for the group of that number. In their own group
# alone, a user who reaches their cache home only as a member of another, as under a folder that a
# department's group alone may enter, could read the update themselves and never have it applied.
# Never in a group of root's, which the helper's own are, and never in one its caller names. With
# none of the helper's open files, and for $1 seconds at most: a filesystem of the user's own may
# never answer, and the user can stop what runs as them. Without the apply's second lock too,
# which what it starts as root holds (settled): the reader is not root. The command alone is killed then
# (--foreground): GNU's timeout otherwise kills itself with it, and bash says so in words of its
# own.
as_reader() {
  timeout --foreground -s KILL "$1" /usr/bin/perl -e "$AS_READER" "${READER[0]}" "${READER[1]}" "${READER[3]}" "${@:2}" 8<&- 9<&- </dev/null
}

# Runs what follows $2 as the reader, for $1 seconds at most, as as_reader does, and with nothing
# else of root's (AS_THEIRS, in settings): $2 is that user's home.
as_theirs() {
  as_reader "$1" /usr/bin/perl -e "$AS_THEIRS" "${@:2}"
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
# become since it was named. A pipe that no one writes gives an empty copy at once; one that has
# a writer is refused at once, or gives what was written first, as dd reads one without waiting.
# Neither is a release's file, and what is copied is checked as any copy is. No more than $3
# bytes and one are copied, whatever the file holds: root's end of the pipe counts them, and
# closes it. The reader counts nothing: a count of its own would bound only a reader that kept to
# it. Whether the file held no more than $3.
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
  # A second lock, of this apply's own, which everything it starts as root holds with it for as
  # long as it runs: how this apply knows at its end that nothing of its own is left (settled).
  # Free at once, but for what an apply before left running, which is waited for as at an end.
  exec 8>>"$LOCKS/running"
  flock -w "$LEFT_WAIT" 8 || fail "something that an update before this one started as root is still running after $LEFT_WAIT seconds: try again once that has ended"
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

  signed "$work/manifest.json" "$work/manifest.json.sig" || not_signed "the release's manifest" "$work/manifest.json" "$work/manifest.json.sig"
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
  settled "$version"
  say "$version is installed"
}

# Waits, at an apply's end, until nothing that the apply started is still running as root, for
# LEFT_WAIT seconds at most. The app starts again when its helper ends 0, and whoever ran the
# script reads "is installed" as the end: a tool's own helper that goes on after its tool has
# answered, as root, with what the helper had open, would still be at work under a running app.
# Every program an apply starts as root has the apply's second lock open with it, and holds it:
# only the asking user's reader is started without. So the apply lets its own hold go and takes
# the lock afresh, which it gets once no one else holds it. Where it does not, release $1 is in
# place all the same, and that is what is said; the script does not end 0.
settled() {
  exec 8>&-
  exec 8>>"$LOCKS/running"
  flock -w "$LEFT_WAIT" 8 || fail "$1 is in place, and something that its update started as root is still running after $LEFT_WAIT seconds: start Surogate again once that has ended"
  exec 8>&-
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
  apt-get install -y -qq -o DPkg::Lock::Timeout=300 bubblewrap socat ripgrep virtiofsd uidmap zstd pkexec perl-base openssl jq curl desktop-file-utils \
    && apt-get install -y -qq -o DPkg::Lock::Timeout=300 --no-install-recommends qemu-system-x86 \
    || fail "could not install the packages it needs: ripgrep and virtiofsd are in Ubuntu's universe, which this computer's package sources must include"
  # certutil, with which the app adds the company's CA to each user's NSS database: with --ca-cert
  # ($1), and at any later run while the CA is kept, as a repair.
  if [ -n "${1:-}" ] || [ -f "$COMPANY_CA" ]; then
    apt-get install -y -qq -o DPkg::Lock::Timeout=300 libnss3-tools || fail "could not install libnss3-tools, which holds certutil"
  fi
}

# The company's certificate authority, from --ca-cert's file $1: each certificate in it, every one a
# CA's, written again by openssl. Nothing else of the file is kept, a key beside the certificates
# least of all. The file is read once, as the user who asked, as an apply's files are (taken): root
# may not see into a home another computer serves, and reads nothing that user could not. It is
# never read through a link, a pipe in its place is refused at once, and no more than a megabyte of
# it is copied, which holds hundreds of certificates. The copy is in a folder of this run's own, in
# /tmp as a download's is, which goes however the run ends. What passes here is what the app then
# takes (companyCertificates in src/shell/company-ca.ts), which stops trusting a CA whose file it
# refuses: a certificate is a CA's as the app reads one, where it says so and its key, if it says
# what the key is for, may sign certificates; and the certificates as they are written again are a
# megabyte at most. They are this run's alone (GIVEN_CA) until its downloads have passed with
# them: keep_company_ca then puts them where every user's app reads them.
company_ca() {
  local given="$1" file work cert said count=0
  # Who reads here is who asked; root's own reads after this one stay root's.
  local READER=("${READER[@]}")
  asker
  # Its whole path, as that user finds it, with no link left in it.
  file="$(as_reader "$SMALL_WAIT" realpath -e -- "$given" 2>/dev/null)" \
    && as_reader "$SMALL_WAIT" test -f "$file" && as_reader "$SMALL_WAIT" test -r "$file" \
    || fail "$(named "$given") is no file that $(named "${READER[2]}") can read"
  scratch work -p /tmp tmp.XXXXXXXXXX
  taken "$file" "$work/given" 1048576 "$SMALL_WAIT" \
    || fail "$(named "$file") holds more than a megabyte, and a file of certificate authorities holds far less"
  # Each certificate into a file of its own, one open at a time: a megabyte holds more of them
  # than a process may have files open.
  awk -v dir="$work" '/-----BEGIN CERTIFICATE-----/ { if (file) close(file); file = sprintf("%s/cert-%04d.pem", dir, ++n) } file { print > file } /-----END CERTIFICATE-----/ { if (file) close(file); file = "" }' "$work/given"
  for cert in "$work"/cert-*.pem; do
    [ -e "$cert" ] || break
    said="$(openssl x509 -in "$cert" -noout -ext basicConstraints,keyUsage 2>/dev/null)" || said=
    [[ "$said" == *CA:TRUE* ]] && { [[ "$said" != *"X509v3 Key Usage"* ]] || [[ "$said" == *"Certificate Sign"* ]]; } \
      || fail "$(named "$file") holds a certificate that is not a certificate authority's"
    openssl x509 -in "$cert" >>"$work/ca.pem"
    count=$((count + 1))
  done
  [ "$count" -gt 0 ] || fail "$(named "$file") holds no PEM certificate"
  [ "$(stat -c %s "$work/ca.pem")" -le 1048576 ] \
    || fail "$(named "$file") holds more than a megabyte of certificates, and a file of certificate authorities holds far less"
  GIVEN_CA="$work/ca.pem"
}

# Keeps the company's certificate authority this run was given, once its downloads have passed with
# it: a CA that is not this network's then never takes the place of the one that works. Replaced
# whole, by one rename, whatever stands in its place: a link there is not written through, and a
# folder, which no rename replaces, goes first. So does whatever an earlier run left where the new
# file is written: nothing there becomes the CA. Its own folder is root's own (roots_folder), and
# one that every user's app can look into, as the script makes it.
keep_company_ca() {
  local folder
  folder="$(dirname "$COMPANY_CA")"
  roots_folder "the company's certificate authority was not kept"
  mkdir -p "$folder"
  chmod 0755 "$folder"
  rm -rf -- "$COMPANY_CA.new"
  install -m 0644 -T "$GIVEN_CA" "$COMPANY_CA.new"
  [ ! -d "$COMPANY_CA" ] || [ -L "$COMPANY_CA" ] || rm -rf -- "$COMPANY_CA"
  mv -T "$COMPANY_CA.new" "$COMPANY_CA"
  say "every user's Surogate, and their Chrome, Edge and Brave, trust the company's certificate authority in $COMPANY_CA"
}

# curl for a release's files, the install's and a rollback's: no curlrc, https or http alone, and
# for no longer than a download may wait (TIMELY). curl reads its address's letters as UTF-8
# (C.UTF-8, which has no language of its own): a server's name may have letters outside ASCII, as
# the name the user fetched this script from may, and in a locale without them curl refuses the
# name before it looks it up. With the company's CA beside curl's own roots where there is one,
# through a network that signs every site with it, or from the company's own server: the CA this
# run was given, else the one kept. The kept one is root's own word, as the install record is: a
# file that anyone else may write names no certificate authority to root. The CA says whose
# certificate a server may show, and no more: what is downloaded is a release only by its
# signature (signed).
fetch() {
  local ca="$GIVEN_CA" trust=()
  if [ -z "$ca" ] && { [ -e "$COMPANY_CA" ] || [ -L "$COMPANY_CA" ]; }; then
    roots_alone "$COMPANY_CA" || fail "$COMPANY_CA is not as Surogate Desktop's install leaves it: run its install script again with --ca-cert"
    ca="$COMPANY_CA"
  fi
  [ -z "$ca" ] || trust=(--cacert "$ca" --capath /etc/ssl/certs)
  LC_ALL=C.UTF-8 curl -q --proto '=https,http' "${TIMELY[@]}" "${trust[@]}" "$@"
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
  local base="$1" download named release version size installed tarball=""
  # In /tmp, whatever TMPDIR root's own shell has: /tmp is root's, and no one there renames what
  # is another's. Whoever owns a folder that TMPDIR named could put one of their own in this
  # one's name, and root's downloads would be written through whatever stood in it.
  scratch download -p /tmp tmp.XXXXXXXXXX
  # No download is longer than what it is for, whatever its server sends, into root's /tmp, which
  # may be memory: a manifest is a line of 4096 bytes at most, as an apply takes one; its signature
  # is Ed25519's 64 bytes, and one more shows one that is too long; and a tarball is the size its
  # signed manifest names.
  fetch -fsSL --max-filesize 4096 -o "$download/manifest.json" "$base/desktop/latest.json" \
    || fail "could not download $base/desktop/latest.json"
  # Its signature is its release's own, at the release's place, which is sent before latest.json
  # names the release and never sent again: latest.json is then the one object that moves, and
  # no moment has a manifest beside another's signature. The version that names the place is the
  # manifest's own word, read before any key is asked of it, and a version is all it may be.
  named="$(named_version "$download/manifest.json")" || fail "$base/desktop/latest.json is not a release of Surogate Desktop for this computer"
  fetch -fsSL --max-filesize 65 -o "$download/manifest.json.sig" "$base/desktop/releases/$named/manifest.json.sig" \
    || fail "could not download $base/desktop/releases/$named/manifest.json.sig"
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
    fetch -fSL --max-filesize "$size" -o "$tarball" "$base/desktop/$(jq -r .url "$download/manifest.json")" \
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
# Never one with a user or a password in it, whoever calls this: the record is every user's to read.
record() {
  nameless "$1" || fail "$CREDENTIALS"
  roots_folder "where it installed from was not written"
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
  nameless "$base" || fail "$RECORD names a base with a user or a password in it: run Surogate Desktop's install script again, with a base that names its server alone"
  installed="$(installed_version)"
  [ -n "$installed" ] || fail "Surogate Desktop is not installed: run its install script first"
  # Before anything is asked of the base: where what the installed version keeps is not known, no
  # release is one that reads it.
  installed_schema >/dev/null || fail "the installed $installed names no state schema: run Surogate Desktop's install script again"
  # Its folder in /tmp, as the install's is, whatever TMPDIR root's own shell has.
  scratch download -p /tmp tmp.XXXXXXXXXX
  # Each download as the install's own (install_latest, fetch): no more of it than it is for, a
  # manifest's 4096 bytes and a signature's 64 and one more.
  fetch -fsSL --max-filesize 4096 -o "$download/manifest.json" "$base/desktop/releases/$version/manifest.json" \
    || fail "could not download $base/desktop/releases/$version/manifest.json"
  fetch -fsSL --max-filesize 65 -o "$download/manifest.json.sig" "$base/desktop/releases/$version/manifest.json.sig" \
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
    fetch -fSL --max-filesize "$size" -o "$tarball" "$base/desktop/releases/$version/surogate-desktop-$version-linux-x64.tar.gz" \
      || fail "could not download Surogate Desktop $version from $base"
  fi
  apply "$download/manifest.json" "$download/manifest.json.sig" "$tarball" older
}

# $1: the base. $2: the file --ca-cert named, or nothing.
install_all() {
  packages "$2"
  [ -z "$2" ] || company_ca "$2"
  apparmor_profile
  kvm_group
  install_latest "$1"
  [ -z "$2" ] || keep_company_ca
  integrate
  record "$1"
  notes
  say "open Surogate from your applications, or run surogate"
}

# Whether base $1 names its server alone, with no user and no password before it (user:password@):
# asked of the part between the two slashes and the next slash, question mark or hash.
nameless() {
  local server="${1#*://}"
  [[ "${server%%[/?#]*}" != *@* ]]
}

# Whether $1 is an http or https URL with no white space in it, read byte for byte in no locale of
# its caller's: what is a base is then the same for whoever runs the script, and for root's part
# of it. In most locales, more characters than ASCII's six are white space, and bytes that are no
# characters match nothing.
# Its server begins right behind the two slashes: curl reads a third slash there as a slip and
# takes what follows for the server, a user and a password before it too, where nameless above
# would find no server at all, and so no user.
http_url() {
  local LC_ALL=C
  [[ "$1" =~ ^https?://[^/?#[:space:]][^[:space:]]*$ ]]
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

# What the program that follows writes, as root reads what a user's own program says: through a
# reader of root's own, for SMALL_WAIT at most and no further than $1 bytes and one, whatever that
# program does. A user's login runs their own profile, and certutil the modules their own database
# names: one may write without end, or leave a program behind that keeps its output open, which is
# not waited for and is that user's own to end. The reader is a line of perl, which hands on each
# piece as it reads it: head keeps what it has read until it ends, and loses it when its time is
# up. Then, in a last line, how the two ended, the program and the reader.
heard() {
  "${@:2}" 2>/dev/null </dev/null | timeout --foreground -s KILL "$SMALL_WAIT" /usr/bin/perl -e \
    '$left = shift; while ($left > 0 && ($got = sysread(STDIN, $piece, $left < 65536 ? $left : 65536))) { syswrite(STDOUT, $piece); $left -= $got }' "$(( $1 + 1 ))" 2>/dev/null
  printf '\n%s' "${PIPESTATUS[*]}"
}

# The invoking user's XDG config, data and cache folders, one a line, from their login's own
# environment: sudo reset this one's. A login that does not say them in its time, or says more
# than a login's few lines, leaves XDG's own.
login_folders() {
  local home lines config data cache
  home="$(getent passwd "$1" | cut -d: -f6)"
  lines="$(heard 65536 timeout --foreground -s KILL "$SMALL_WAIT" runuser -l "$1" -c 'printf "\n%s\n%s\n%s\n" "${XDG_CONFIG_HOME:-}" "${XDG_DATA_HOME:-}" "${XDG_CACHE_HOME:-}"')"
  lines="${lines%$'\n'*}"
  if [ "${#lines}" -le 65536 ]; then lines="$(printf %s "$lines" | tail -n 3)"; else lines=; fi
  { read -r config; read -r data; read -r cache; } <<<"$lines" || true
  xdg "${config:-}" "$home/.config"
  xdg "${data:-}" "$home/.local/share"
  xdg "${cache:-}" "$home/.cache"
}

# Takes the entries the app made for the company's CA out of the NSS databases of the user named
# $1, whose number is $2, whose group's is $3 and whose home is $4: the folders that follow. Nothing else takes them
# back once the app is gone, and that user's Chrome, Edge and Brave would go on trusting the CA for
# every site. An entry is the app's by its name, to the letter: "Surogate company CA", a space and
# 16 digits of hex, as the app names one (NICKNAME in src/shell/company-ca.ts). No other entry is
# touched, a name of the user's own that only begins so least of all.
#
# By certutil run as that user, as their own login would run it and never as root: the name is
# that number's, as asker makes sure of the one it names, and what is changed is a database of
# that user's own, whatever a link in their home leads to. Each look and each change for SMALL_WAIT
# at most: a home that another computer serves may never answer, and is then said and passed by.
# With nothing to ask on, no terminal either: certutil lists and removes without a database's
# password, and one that would ask for it fails. It runs the modules that user's database names,
# so it is started with nothing of root's (as_theirs), and what it writes is read as a user's own
# program's is (heard). A database that is not there is not
# made. One that certutil cannot read or change is left as it is, and said.
forget_company_ca() {
  local user="$1" uid="$2" gid="$3" home="$4" db number groups listed line name ended ends
  local ours='^(Surogate company CA [0-9a-f]{16}) +[^ ,]*,[^ ,]*,[^ ,]* *$'
  local still="an entry of Surogate's for the company's certificate authority may still be trusted there"
  number="$(listing id -u -- "$user")" && groups="$(listing id -G -- "$user")" && ended=0 || ended="$?"
  if outlasted "$ended"; then
    say "left $(named "$user")'s NSS databases as they are, as this computer's list of users and groups did not answer within $SMALL_WAIT seconds: $still"
    return 0
  fi
  [ "$ended" -eq 0 ] && [ "$number" = "$uid" ] && [[ "$groups" =~ ^[0-9]+(\ [0-9]+)*$ ]] || return 0
  local READER=("$uid" "$gid" "$user" "${groups// /,}")
  number="$(as_theirs "$SMALL_WAIT" "$home" id -u 2>/dev/null)" || number=
  number+=":$(as_theirs "$SMALL_WAIT" "$home" id -g 2>/dev/null)" || number=
  [ "$number" = "$uid:$gid" ] || return 0
  for db in "${@:5}"; do
    as_theirs "$SMALL_WAIT" "$home" test -f "$db/cert9.db" && ended=0 || ended="$?"
    if outlasted "$ended"; then
      say "left $(named "$user")'s NSS databases as they are, as $(named "$db") did not answer within $SMALL_WAIT seconds: $still"
      return 0
    fi
    [ "$ended" -eq 0 ] || continue
    if ! as_theirs "$SMALL_WAIT" "$home" test -O "$db/cert9.db"; then
      say "left the NSS database in $(named "$db") as it is, as it is not $(named "$user")'s own: $still"
      continue
    fi
    # Its list, a megabyte of it at most, and then how certutil and root's reader of it ended.
    listed="$(heard 1048576 as_theirs "$SMALL_WAIT" "$home" certutil -L -d "sql:$db")"
    ends="${listed##*$'\n'}"
    listed="${listed%$'\n'*}"
    [[ "$ends" =~ ^[0-9]+\ [0-9]+$ ]] || ends="1 1"
    if outlasted "${ends% *}" || outlasted "${ends#* }"; then
      say "left $(named "$user")'s NSS databases as they are, as $(named "$db") did not answer within $SMALL_WAIT seconds: $still"
      return 0
    fi
    if [ "$ends" != "0 0" ] || [ "${#listed}" -gt 1048576 ]; then
      say "left $(named "$user")'s NSS database in $(named "$db") as it is, as certutil could not read it: $still"
      continue
    fi
    # "<name>   <SSL>,<S/MIME>,<code signing>" a line.
    while IFS= read -r line; do
      [[ "$line" =~ $ours ]] || continue
      name="${BASH_REMATCH[1]}"
      as_theirs "$SMALL_WAIT" "$home" certutil -D -d "sql:$db" -n "$name" >/dev/null 2>&1 && ended=0 || ended="$?"
      [ "$ended" -ne 0 ] || continue
      # A name of the user's that goes on in spaces is listed as the app's is, and is not found by
      # the app's: it is theirs, and stays with nothing said.
      outlasted "$ended" || as_theirs "$SMALL_WAIT" "$home" certutil -L -d "sql:$db" -n "$name" >/dev/null 2>&1 || continue
      say "could not take $name out of $(named "$user")'s NSS database in $(named "$db"): their browsers go on trusting it"
    done <<<"$listed"
  done
}

# Takes the app's entries for the company's CA out of every user's NSS databases, each as that
# user (forget_company_ca): from the two folders Chromium keeps a user's database in, ~/.pki/nssdb
# and the XDG data folder's, which for the invoking user $1 is also the one in $2, the data folder
# their session names. The users are the system's own list's, asked once and for a bounded while,
# as asker asks: what it named before its time was up is still gone through. The invoking user is
# asked for by name too: a company's directory names its users one by one, and lists none. $3:
# whether this computer kept a company's CA, so that its users' apps may have made entries.
forget_company_cas() {
  local user="$1" data="$2" kept="$3" users mine= each uid gid home folders seen=
  if ! command -v certutil >/dev/null; then
    [ -z "$kept" ] || say "left the company's certificate authority trusted in the browsers of this computer's users, as certutil is not installed: install libnss3-tools, and run this again"
    return 0
  fi
  if ! users="$(listing getent passwd)"; then
    say "this computer's list of users did not answer within $SMALL_WAIT seconds: an entry of Surogate's for the company's certificate authority may still be trusted in the browsers of the users it did not name"
    # Its last line may be half of one.
    if [[ "$users" == *$'\n'* ]]; then users="${users%$'\n'*}"; else users=; fi
  fi
  [ -z "$user" ] || mine="$(listing getent passwd "$user")" || mine=
  while IFS=: read -r each _ uid gid _ home _; do
    [[ "$home" == /?* ]] && [[ "$uid" =~ ^[0-9]+$ ]] && [[ "$gid" =~ ^[0-9]+$ ]] || continue
    folders=("$home/.pki/nssdb" "$home/.local/share/pki/nssdb")
    if [ -n "$user" ] && [ "$each" = "$user" ]; then
      [ -z "$seen" ] || continue
      seen=1
      [ "$data" = "$home/.local/share" ] || folders+=("$data/pki/nssdb")
    fi
    forget_company_ca "$each" "$uid" "$gid" "$home" "${folders[@]}"
  done <<<"$mine"$'\n'"$users"
}

# Removes the app for every user of the computer, and the invoking user's autostart entry; asks
# before deleting that user's data, as that user. Other users' data, every chat's folder and the
# packages stay. The app's entries for the company's CA go from every user's NSS databases, as that
# user, with nothing asked. $1-$3: the user's XDG config, data and cache folders, when their
# session gave them.
uninstall() {
  # One at a time with an apply: one that runs now finishes before the tree goes, and one that
  # starts now waits, and makes the tree again once this has ended.
  lock
  in_use "$ROOT" && fail "Surogate is running: quit it first, for every user of this computer"
  roots_folder "nothing was removed"
  local kept=
  [ ! -e "$COMPANY_CA" ] && [ ! -L "$COMPANY_CA" ] || kept=1
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

  local user="${SUDO_USER:-}" config data= cache answer=
  [ "$user" != root ] || user=
  if [ -n "$user" ] && [ "$#" -eq 3 ]; then
    config="$1" data="$2" cache="$3"
  elif [ -n "$user" ]; then
    { read -r config; read -r data; read -r cache; } < <(login_folders "$user")
  fi
  # The company's CA goes from every user's browsers with the app.
  forget_company_cas "$user" "$data" "$kept"
  [ -n "$user" ] || return 0
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
    --base | --ca-cert | "")
      # What installs as root is the system's own tools, wherever its caller's PATH points, from
      # before it runs the first of them, as an apply's are: an openssl of another's there would
      # call any release signed.
      [ "$EUID" -ne 0 ] || export PATH=/usr/sbin:/usr/bin:/sbin:/bin
      local base=https://surogate.ai ca=
      while [ "$#" -gt 0 ]; do
        case "$1" in
          --base)
            [ "$#" -ge 2 ] && http_url "$2" || fail "usage: install.sh --base <http or https URL>"
            # Before sudo is asked, and before anything is written or downloaded.
            nameless "$2" || fail "$CREDENTIALS"
            base="${2%/}"
            ;;
          --ca-cert)
            [ "$#" -ge 2 ] && [ -n "$2" ] || fail "usage: install.sh [--base <url>] [--ca-cert <file>]"
            # By its whole path, found by the user who names it, before sudo is asked: root's half reads
            # it as that user, wherever sudo starts it. Root's half takes the path as it is handed.
            if [ "$EUID" -ne 0 ]; then ca="$(realpath -e -- "$2" 2>/dev/null)" || fail "$(named "$2"): no such file"; else ca="$2"; fi
            ;;
          *)
            fail "usage: install.sh [--base <url>] [--ca-cert <file>]"
            ;;
        esac
        shift 2
      done
      supported
      unlinked
      if [ "$EUID" -ne 0 ]; then
        say "installing needs administrator rights: sudo asks for your password once"
        # Again as root, from this script's own functions: a script piped to bash has no file to name.
        # sudo resets the environment, so the proxy the user's shell names goes with them. The shell
        # that reads them is named by its whole path: a sudo with no secure_path looks for it on
        # its caller's PATH.
        local again=(--base "$base")
        [ -z "$ca" ] || again+=(--ca-cert "$ca")
        { declare -f; declare -p http_proxy https_proxy HTTPS_PROXY all_proxy ALL_PROXY no_proxy NO_PROXY 2>/dev/null || true; echo 'main "$@"'; } \
          | sudo -- /bin/bash -s -- "${again[@]}" || exit "$?"
        return
      fi
      install_all "$base" "$ca"
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
      fail "usage: install.sh [--base <url>] [--ca-cert <file>] [--version <x.y.z>] [--uninstall]"
      ;;
  esac
}

main "$@"
