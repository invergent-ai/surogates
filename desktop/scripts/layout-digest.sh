#!/usr/bin/env bash
# One sha256 of a layout a build job hands on as an artifact, which is the run's, under whose name any job of the run
# may put other files: of a listing of every file the artifact carries, each by its sha256 and its path from the
# layout's top, sorted by path. The job says it as its own output, and the job that takes the layout checks it before
# it does anything with it. An artifact carries files alone, and none whose path has a name that starts with a dot
# (actions/upload-artifact's include-hidden-files, false): the listing leaves those out too, and refuses a link, which
# an artifact would carry as the file it leads to.
#
#   scripts/layout-digest.sh <folder>
set -euo pipefail

[ "$#" -eq 1 ] && [ -d "$1" ] || { echo "usage: scripts/layout-digest.sh <folder>" >&2; exit 2; }
cd "$1"
link="$(find . -type l -print -quit)"
[ -z "$link" ] || { echo "layout-digest.sh: $1 holds a link, ${link#./}" >&2; exit 1; }
find . -type f ! -path '*/.*' -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum -b -- | sha256sum | cut -d' ' -f1
