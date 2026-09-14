#!/usr/bin/env bash
# Build the Bluetooth stack and pack it as the release archive the Lutris
# installer downloads.
#
#   ./dist.sh                  build and pack into build/
#   ./dist.sh --release v2     ... and publish it as a GitHub release
#
# The installer runs on a machine with no Mono compiler and no checkout, so it
# fetches one archive: the three assemblies, the Linux-side helper, and the
# script that runs it.  One archive on purpose -- the helper and the shim inside
# the prefix speak a private protocol over loopback, and fetching them
# separately is how they end up being different builds of it.
#
# Env overrides: GAME_LIBS (passed through to bleshim/build.sh, which checks the
# shim's surface against the game's own metadata -- worth setting, since a
# missing member is a MissingMethodException in the middle of a ride).
set -e
cd "$(dirname "$0")"

tag=
case "${1-}" in
    "")        ;;
    --release) tag="${2-}" ;;
    *)         echo "usage: ./dist.sh [--release <tag>]" >&2; exit 2 ;;
esac
if [ "${1-}" = "--release" ] && [ -z "$tag" ]; then
    echo "usage: ./dist.sh --release <tag>" >&2
    exit 2
fi

# Unreleased builds are named after the commit they came from, so an archive
# lying around in build/ can still be identified a month later.
commit="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
git diff --quiet 2>/dev/null || commit="$commit+dirty"
version="${tag:-$commit}"

./bleshim/build.sh
./exportshim/build.sh

name="mywhoosh-bleshim-$version"
stage="build/$name"
rm -rf "$stage"
mkdir -p "$stage"

cp -f bleshim/build/Windows.dll \
      bleshim/build/System.Runtime.WindowsRuntime.dll \
      exportshim/build/MyWhooshShim.dll \
      bleshim/blehelper.py \
      lutris/mywhoosh-ble.sh \
      "$stage/"
chmod +x "$stage/blehelper.py" "$stage/mywhoosh-ble.sh"

# The manifest is the only way a user can tell which build they are running:
# the installer unpacks this into a prefix, where nothing else records where it
# came from.  It ships inside the archive, next to what it describes.
# (written last, so the hashes below cover everything but the manifest itself)
{
    echo "# Built by dist.sh -- do not edit"
    echo "version $version"
    echo "date    $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "commit  $commit"
    echo "mcs     $(mcs --version | head -1)"
    echo
    (cd "$stage" && sha256sum -- *)
} > "build/$name.MANIFEST"
mv -f "build/$name.MANIFEST" "$stage/MANIFEST"

archive="build/$name.tar.gz"
tar -czf "$archive" -C build "$name"

echo
cat "$stage/MANIFEST"
echo
echo "packed $archive"

if [ -z "$tag" ]; then
    echo
    echo "publish with:  ./dist.sh --release <tag>"
    exit 0
fi

# The installers are release assets too, so a user installs a tested pair
# rather than whatever the branch looked like today -- which means the tag has
# to point at the commit that pins it.  Bump first, commit, then publish.
sed -i "s|^\( *shim_release: \).*|\1$tag|" lutris/*.yml
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo
    echo "lutris/*.yml now pin $tag, and the tree has uncommitted changes."
    echo "gh tags the release at HEAD, so commit first and run this again:"
    echo
    echo "    git commit -am 'Release $tag' && ./dist.sh --release $tag"
    exit 1
fi

gh release create "$tag" \
    --title "$tag" \
    --notes "$(printf 'Install with:\n\n    lutris -i https://github.com/Dj0ulo/mywhoosh-linux/releases/download/%s/mywhoosh.yml\n\n```\n%s\n```\n' "$tag" "$(cat "$stage/MANIFEST")")" \
    "$archive" lutris/mywhoosh.yml lutris/mywhoosh-hd.yml

echo
echo "released $tag"
