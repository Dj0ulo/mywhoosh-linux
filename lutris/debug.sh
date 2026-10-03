#!/usr/bin/env bash
# Install this checkout into a fresh prefix through Lutris, the way a user
# would get a release -- the last thing to run before ../dist.sh --release.
#
#   lutris/debug.sh [--hd]
#
# Builds the archive with ../dist.sh, removes the previous debug install
# (its prefix, its Lutris entry and config -- nothing else), and hands Lutris
# mywhoosh.yml -- or mywhoosh-hd.yml with --hd -- rewritten to install this
# build instead of a release.  The MSIX is kept in ../downloads/, which is
# gitignored, so only a new game version is ever downloaded again.
set -e
repo="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo"

src=lutris/mywhoosh.yml
slug=mywhoosh-debug
case "${1-}" in
    "") ;;
    --hd) src=lutris/mywhoosh-hd.yml; slug=mywhoosh-hd-debug ;;
    *) echo "usage: $0 [--hd]" >&2; exit 2 ;;
esac

./dist.sh
# dist.sh names the archive after the commit; the installer wants one name.
archive="$(ls -t build/mywhoosh-bleshim-*.tar.gz | grep -v -- '-debug\.tar\.gz$' | head -1)"
cp -f "$archive" build/mywhoosh-bleshim-debug.tar.gz
echo "installing $archive"

# The release installer, pointed at this checkout.  Each edit must match
# exactly once, so a change to the installers fails here rather than
# installing a release behind our back.
mkdir -p downloads
yml="build/$slug.yml"
python3 - "$src" "$yml" "$repo" "$slug" <<'EOF'
import re, sys
src, dst, repo, slug = sys.argv[1:]
text = open(src).read()

def sub(pattern, repl, count=1):
    global text
    text, n = re.subn(pattern, repl, text, flags=re.M)
    if n != count:
        sys.exit(f"debug.sh: {pattern!r} matched {n} times in {src}, expected {count}")

sub(r"^name: (.*)$", r"name: \1 (debug)")
sub(r"^game_slug: .*$", lambda m: f"game_slug: {slug}")
sub(r"^slug: .*$", lambda m: f"slug: {slug}")
sub(r"^version: .*$", "version: Local build")
# The release archive is not fetched; this checkout's is extracted directly.
# Not listed under `files`: Lutris 0.5.14 fetches those with requests, which
# has no file:// adapter.
sub(r"^ *- shim: .*\n", "")
sub(r"^( *)file: shim$", lambda m: f"{m[1]}file: {repo}/build/mywhoosh-bleshim-debug.tar.gz")
# The downloader skips a package already in its download directory when the
# store's SHA256 matches, so a fresh prefix does not cost a 2 GB download.
sub(r"\$CACHE\b", lambda m: f"{repo}/downloads", count=2)
open(dst, "w").write(text)
EOF

# Lutris records every install, and a second one with the same slug is a
# second entry rather than a replacement -- so forget the old one first.
python3 - "$slug" <<'EOF'
import glob, os, shutil, sqlite3, sys
slug = sys.argv[1]
db = os.path.expanduser("~/.local/share/lutris/pga.db")
if os.path.exists(db):
    con = sqlite3.connect(db)
    for gid, directory, configpath in con.execute(
            "select id, directory, configpath from games where slug = ?", (slug,)).fetchall():
        if directory and os.path.isdir(directory):
            print(f"removing {directory}")
            shutil.rmtree(directory)
        if configpath:
            for f in glob.glob(os.path.expanduser(f"~/.config/lutris/games/{configpath}.yml")):
                os.remove(f)
        con.execute("delete from games where id = ?", (gid,))
    con.commit()
EOF
# An install cancelled before Lutris recorded it still leaves its prefix.
rm -rf "$HOME/Games/$slug"

exec lutris -i "$repo/$yml"
