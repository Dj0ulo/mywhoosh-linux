#!/usr/bin/env bash
# Refresh dist/ -- the built assemblies the Lutris installer downloads.
#
#   ./dist.sh
#
# Everything else here is built on demand from source; these three files are
# the exception, because the Lutris installer runs on a machine that has no
# Mono compiler and no checkout.  It fetches them by raw URL from this branch,
# so what is committed in dist/ is what users install.  Rebuild and commit
# after any change under bleshim/src/ or exportshim/.
#
# Env overrides: GAME_LIBS (passed through to bleshim/build.sh, which checks
# the shim's surface against the game's own metadata -- worth setting, since a
# missing member is a MissingMethodException in the middle of a ride).
set -e
cd "$(dirname "$0")"

./bleshim/build.sh
./exportshim/build.sh

cp -f bleshim/build/Windows.dll \
      bleshim/build/System.Runtime.WindowsRuntime.dll \
      exportshim/build/MyWhooshShim.dll \
      dist/

# The manifest is the only way a user can tell which build they are running:
# the installer copies the DLLs into a prefix, where nothing records where they
# came from.  Keep it next to them and commit it in the same commit.
{
    echo "# Built by dist.sh -- do not edit"
    echo "date    $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "commit  $(git rev-parse --short HEAD 2>/dev/null || echo unknown)$(git diff --quiet 2>/dev/null || echo '+dirty')"
    echo "mcs     $(mcs --version | head -1)"
    echo
    (cd dist && sha256sum Windows.dll System.Runtime.WindowsRuntime.dll MyWhooshShim.dll)
} > dist/MANIFEST

echo
cat dist/MANIFEST
echo
echo "commit dist/ so the Lutris installer picks it up"
