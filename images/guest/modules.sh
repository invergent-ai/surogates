#!/usr/bin/env bash
# The kernel's modules a part of the guest loads, out of the pinned kernel's tree <modules>
# (lib/modules/<version>), as its modules.dep names each one's file and the modules it needs. The names
# are given in an order they load in, each after every module it needs: one the tree lacks, and one given
# before a module it needs or without it, fail the build, each named, so each list is whole. Run in the
# image's build: keep for the image's own tree, which vm/init loads from with modprobe and the kernel loads
# no other from (modules_disabled); copy for the initrd's (images/guest/initrd), which its init loads in
# its list's order.
#
# Usage:
#   images/guest/modules.sh keep <modules> <name>…         # every other module of the tree deleted
#   images/guest/modules.sh copy <modules> <out> <name>…   # each named module's file copied into <out>
set -euo pipefail

usage="usage: images/guest/modules.sh keep <modules> <name>… | copy <modules> <out> <name>…"
MODE="${1:-}"
TREE="${2:-}"
case "$MODE" in
  keep) [ "$#" -ge 3 ] && shift 2 ;;
  copy) [ "$#" -ge 4 ] && OUT="$3" && shift 3 ;;
  *) false ;;
esac || { echo "$usage" >&2; exit 2; }
DEP="$TREE/modules.dep"
[ -r "$DEP" ] || { echo "modules.sh: cannot read $DEP" >&2; exit 1; }

# Each module of the tree, a line: its name, its file, and the names of the modules it needs.
listed="$(awk -F: '
  function named(file) { sub(/.*\//, "", file); sub(/\.ko(\.[a-z0-9]+)?$/, "", file); return file }
  { line = named($1) " " $1; n = split($2, need, " "); for (i = 1; i <= n; i++) line = line " " named(need[i]); print line }' "$DEP")"

problems=()
files=()
given=" "
for name in "$@"; do
  entry="$(awk -v want="$name" '$1 == want { print; exit }' <<< "$listed")"
  if [ -z "$entry" ]; then
    problems+=("the kernel has no module $name")
  else
    read -r _ file needs <<< "$entry"
    files+=("$file")
    for need in $needs; do
      case "$given" in *" $need "*) ;; *) problems+=("$name needs $need, which is not given before it") ;; esac
    done
  fi
  given="$given$name "
done
if [ "${#problems[@]}" -gt 0 ]; then
  printf 'modules.sh: %s\n' "${problems[@]}" >&2
  exit 1
fi

case "$MODE" in
  keep)
    # Every module's file under the tree's kernel/ but the named ones'.
    while IFS= read -r -d '' path; do
      keep=
      for file in "${files[@]}"; do [ "$path" = "$TREE/$file" ] && keep=1; done
      [ -n "$keep" ] || rm -f -- "$path"
    done < <(find "$TREE/kernel" -type f -print0)
    ;;
  copy)
    mkdir -p "$OUT"
    for file in "${files[@]}"; do cp -- "$TREE/$file" "$OUT/"; done
    ;;
esac
