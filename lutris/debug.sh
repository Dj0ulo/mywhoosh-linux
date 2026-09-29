#!/usr/bin/env bash
# Install this checkout into a fresh prefix through Lutris, the way a user
# would get a release -- the last thing to run before ../dist.sh --release.
#
#   lutris/debug.sh
#
# Builds the archive with ../dist.sh, removes the previous debug install
# (its prefix, its Lutris entry and config -- nothing else), and hands
# mywhoosh-debug.yml to Lutris.  The MSIX is kept in ../downloads/, which is
# gitignored, so only a new game version is ever downloaded again.
set -e
repo="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo"

./dist.sh
# dist.sh names the archive after the commit; the installer wants one name.
archive="$(ls -t build/mywhoosh-bleshim-*.tar.gz | grep -v -- '-debug\.tar\.gz$' | head -1)"
cp -f "$archive" build/mywhoosh-bleshim-debug.tar.gz
echo "installing $archive"

# Lutris records every install, and a second one with the same slug is a
# second entry rather than a replacement -- so forget the old one first.
python3 - <<'EOF'
import glob, os, shutil, sqlite3
db = os.path.expanduser("~/.local/share/lutris/pga.db")
if os.path.exists(db):
    con = sqlite3.connect(db)
    for gid, directory, configpath in con.execute(
            "select id, directory, configpath from games where slug = 'mywhoosh-debug'").fetchall():
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
rm -rf "$HOME/Games/mywhoosh-debug"

mkdir -p downloads
yml="build/mywhoosh-debug.yml"
sed "s|@REPO@|$repo|" lutris/mywhoosh-debug.yml > "$yml"
exec lutris -i "$repo/$yml"
