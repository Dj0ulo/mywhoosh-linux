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
# The game updater (lutris/mywhoosh-update.py) resolves downloads with the
# Microsoft Store downloader the installer uses, so a copy ships beside it --
# pinned, because it runs on every machine with an update to fetch.  Bump both
# lines together, after reading the new commit.
DOWNLOADER_COMMIT=4dbdee1ed08e3c24ff34a41fa92f3407188a24ad
DOWNLOADER_SHA256=01a524cdc2bd48863da4f86eb9eebb2b9f0f061eef32ef9f983d2ac92b63a7b9
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
[[ -z "$(git status --porcelain 2>/dev/null)" ]] || commit="$commit+dirty"
version="${tag:-$commit}"

./bleshim/build.sh
./exportshim/build.sh
bash ./bonjour/build.sh

name="mywhoosh-bleshim-$version"
stage="build/$name"
rm -rf "$stage"
mkdir -p "$stage"

cp -f bleshim/build/Windows.dll \
      bleshim/build/System.Runtime.WindowsRuntime.dll \
      exportshim/build/MyWhooshShim.dll \
    bonjour/build/BonjourCompat.dll \
    bonjour/build/SetupBonjour.exe \
    bonjour/build/PatchComWrapper.cs \
      bleshim/blehelper.py \
    lutris/mywhoosh-bonjour.sh \
      lutris/mywhoosh-ble.sh \
      lutris/mywhoosh-update.py \
      "$stage/"
chmod +x "$stage/blehelper.py" "$stage/mywhoosh-ble.sh" "$stage/mywhoosh-update.py" "$stage/mywhoosh-bonjour.sh"

mkdir -p build/cache
downloader="build/cache/ms_store_download-$DOWNLOADER_COMMIT.py"
if ! echo "$DOWNLOADER_SHA256  $downloader" | sha256sum -c --status 2>/dev/null; then
    curl -fsSL -o "$downloader" \
        "https://raw.githubusercontent.com/Dj0ulo/ms-store-download/$DOWNLOADER_COMMIT/ms_store_download.py"
    echo "$DOWNLOADER_SHA256  $downloader" | sha256sum -c --quiet
fi
cp -f "$downloader" "$stage/ms_store_download.py"

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
repository="${RELEASE_REPOSITORY:-$(git remote get-url origin)}"
repository="${repository#https://github.com/}"
repository="${repository#git@github.com:}"
repository="${repository%.git}"
if [[ ! "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
    echo "Set RELEASE_REPOSITORY to the GitHub owner/repository to publish." >&2
    exit 1
fi
sed -i -e "s|^\( *shim_release: \).*|\1$tag|" \
       -e "s|^\( *shim_repository: \).*|\1$repository|" lutris/*.yml
if [[ -n "$(git status --porcelain)" ]]; then
    echo
    echo "lutris/*.yml now pin $tag, and the tree has uncommitted changes."
    echo "gh tags the release at HEAD, so commit first and run this again:"
    echo
    echo "    git add <reviewed files> && git commit -m 'Release $tag'"
    echo "    ./dist.sh --release $tag"
    exit 1
fi

gh release create "$tag" \
    --repo "$repository" \
    --title "$tag" \
    --notes "$(printf 'Downloads and verifies Bonjour directly from Apple. Complete its installer and licence prompt; no local MSI or Apple account is required.\n\nInstall with:\n\n    lutris -i https://github.com/%s/releases/download/%s/mywhoosh.yml\n\n```\n%s\n```\n' "$repository" "$tag" "$(cat "$stage/MANIFEST")")" \
    "$archive" lutris/mywhoosh.yml lutris/mywhoosh-hd.yml

echo
echo "released $tag"
