#!/usr/bin/env bash
# The app's own rg: ripgrep's own release for this computer, its archive checked against the sha256 pinned here, at
# bin/ beside the app's node. On macOS (src/hosts/srt-darwin.ts) the aarch64-apple-darwin build at bin/rg, which links
# nothing but the system's libSystem and libiconv, which the file helper's sandbox reads. On Windows (src/files/os.ts)
# the x86_64-pc-windows-msvc build at bin/rg.exe, run from Git for Windows' bash; the release signs it again. An rg of
# that version already there is kept. Linux's file helper runs the system's rg, which the install script installs.
#
# A pin moves in one change: VERSION and its SHA256s together, each hash read from the release's own .sha256 file and
# checked against the archive fetched beside it.
set -euo pipefail

VERSION=15.2.0
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64) TARGET=aarch64-apple-darwin SHA256=3750b2e93f37e0c692657da574d7019a101c0084da05a790c83fd335bad973e4 ARCHIVE=tar.gz RG=rg CHECK="shasum -a 256" ;;
  MINGW64_NT*-x86_64 | MSYS_NT*-x86_64)
    TARGET=x86_64-pc-windows-msvc SHA256=71b2fef860abe467217a538ff31de02f5258807c0129f771846f87bd029aafc5 ARCHIVE=zip RG=rg.exe CHECK=sha256sum ;;
  *) echo "rg.sh fetches the app's rg for macOS on Apple silicon and Windows on x64" >&2; exit 1 ;;
esac
NAME="ripgrep-$VERSION-$TARGET"

cd "$(dirname "$0")/.."
[ "$(bin/$RG --version 2>/dev/null | head -n 1)" = "ripgrep $VERSION" ] && exit 0
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# -q first, or curl reads the user's curlrc, whose headers, logins and netrc would go out with it.
curl -q -fsSL --proto '=https' -o "$tmp/rg.$ARCHIVE" "https://github.com/BurntSushi/ripgrep/releases/download/$VERSION/$NAME.$ARCHIVE"
echo "$SHA256  $tmp/rg.$ARCHIVE" | $CHECK --check --status || { echo "rg.sh: the archive is not the pinned one" >&2; exit 1; }
if [ "$ARCHIVE" = zip ]; then unzip -q "$tmp/rg.zip" "$NAME/$RG" -d "$tmp"; else tar -xzf "$tmp/rg.tar.gz" -C "$tmp" "$NAME/$RG"; fi
mkdir -p bin
mv "$tmp/$NAME/$RG" "bin/$RG"
