#!/usr/bin/env bash
# Build the image from one release archive.
#
#   ./build.sh v0.1.5      the archive published with that release (what CI does)
#   ./build.sh             this checkout's, built by ../dist.sh, to try it first
#
# Tags the image mywhoosh-linux:<version> locally; IMAGE=<name> to tag another
# (CI passes its registry name).  BUILDER=docker|podman, default: podman if
# installed.  Run the result with:  MYWHOOSH_IMAGE=mywhoosh-linux:<version> ./mywhoosh
set -e
cd "$(dirname "$0")"

tag="${1-}"
rm -f mywhoosh-bleshim-*.tar.gz
if [ -n "$tag" ]; then
    url="https://github.com/Dj0ulo/mywhoosh-linux/releases/download/$tag/mywhoosh-bleshim-$tag.tar.gz"
    curl -fsSL -o "mywhoosh-bleshim-$tag.tar.gz" "$url"
    version="$tag"
else
    ../dist.sh >&2
    archive=$(ls -t ../build/mywhoosh-bleshim-*.tar.gz | head -1)
    version=$(basename "$archive" .tar.gz)
    version="${version#mywhoosh-bleshim-}"
    cp "$archive" .
fi

builder="${BUILDER:-}"
[ -n "$builder" ] || { command -v podman >/dev/null && builder=podman || builder=docker; }
image="${IMAGE:-mywhoosh-linux}"
# An image tag allows no '+' (a dirty checkout's version has one).
"$builder" build --build-arg "SHIM_RELEASE=$version" -t "$image:${version//+/-}" .
echo "built $image:${version//+/-}"
