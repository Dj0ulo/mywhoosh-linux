#!/usr/bin/env python3
"""Bring an installed MyWhoosh up to the Microsoft Store's version.

    mywhoosh-update.py [--check] [--no-gui] [--source auto|catalog|soap] DIR

DIR is the game directory (drive_c/MyWhoosh or drive_c/MyWhooshHD), or a
prefix or drive_c holding one.  The edition, and so the Store product, is read
from the installed AppxManifest.xml -- nothing here names a game version, so a
new release needs no change to this file.

The game refuses to start once the Store has a newer build, and a Lutris
install has no Store to update it: the installer downloads the package once.
`mywhoosh-ble.sh start` runs this before every launch; a container entrypoint
can run it the same way.

Exit status: 0 up to date or updated, 1 failed (the installed game is left
exactly as it was), 2 usage, 10 with --check when an update is available.
Nothing here raises: every failure is logged and the installed game launches.

Where the version comes from
----------------------------
displaycatalog.mp.microsoft.com answers in ~0.2 s with the package's full name
(Name_6.2.3.0_x64__publisher) and its SHA-256, over TLS that verifies.  The
Windows Update SOAP path the installer uses (cookie, SyncUpdates) takes ~2.5 s
and is the only source of a download URL, so it runs only when there is an
update -- or as the check itself, when the catalog cannot be reached.  The
game's own check is no use here: it asks MyWhoosh's backend, behind Firebase
auth, for the version it *requires*, which says nothing about what the Store
can deliver.

Why the catalog's digest matters: the download URL is plain http, and the
SOAP endpoint (fe3.delivery.mp.microsoft.com) chains to a Microsoft root no
Linux trust store has, so the downloader talks to it unverified.  The package
is installed only if its SHA-256 matches -- the catalog's when it answered.

Swapping, not overwriting
-------------------------
The new package is downloaded and extracted beside the game, in
.<name>.update/, which is on the same filesystem, so the swap is two renames:

    game -> .update/old,   .update/new -> game

A `ready` marker is written into .update/ only once new/ is complete and
synced.  Every run first finishes or rolls back whatever a previous one left:
an interrupted download resumes, a half-extracted new/ is thrown away, and a
game directory missing between the two renames is put back from new/ (if
ready) or old/.  So there is never a moment when a kill, a crash or a power
cut leaves a half-written game in place of a whole one.
"""

import argparse
import contextlib
import errno
import fcntl
import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import time
from xml.etree import ElementTree as ET

# Identity Name in AppxManifest.xml -> Microsoft Store product id.
EDITIONS = {
    "MyWhooshTechnologyService.644173E064ED2": ("9ndh0f2vhzx2", "MyWhoosh"),
    "MyWhooshTechnologyService.MyWhoosh": ("9nvdcph3xdz2", "MyWhoosh HD"),
}
# The directories the installers unpack those into, under drive_c.  Exact
# names: a copy the user keeps beside the game (MyWhoosh.bak) is not the game.
GAME_DIRS = ("MyWhoosh", "MyWhooshHD")
ARCH = "x64"
# The installer renames the game binary (lutris/mywhoosh.yml, `move:`), and so
# does this.  Paths inside the package, which is extracted flat.
EXE_FROM = "MyWhoosh/Binaries/Win64/MyWhoosh.exe"
EXE_TO = "MyWhoosh/Binaries/Win64/MyWhoosh-Win64-Shipping.exe"
DOWNLOADER = pathlib.Path(__file__).resolve().parent / "ms_store_download.py"

CHECK_TIMEOUT = 5      # the catalog; it answers in a fraction of that
NET_TIMEOUT = 30       # anything else, per socket operation

LOG = os.environ.get("MYWHOOSH_SHIM_LOG") or str(pathlib.Path(__file__).resolve().parent / "session.log")


class Cancelled(Exception):
    pass


class Failed(Exception):
    pass


def log(fmt, *a):
    line = "[%s updater] %s" % (time.strftime("%H:%M:%S"), fmt % a if a else fmt)
    print(line, flush=True)
    with contextlib.suppress(OSError), open(LOG, "a") as f:
        f.write(line + "\n")


# ------------------------------------------------------------- what users see
# Lutris 0.5 runs a waited pre-launch command on its GTK main thread and keeps
# its output for its own debug log: during a download its window is frozen and
# nothing printed here reaches the screen.  So the updater shows its own -- a
# zenity progress window when there is one, and notifications regardless.
class Ui:
    def __init__(self, enabled, title):
        self.title = title
        self.enabled = enabled and bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        # The same detour as mywhoosh-ble.sh: inside a Flatpak, notify-send
        # and zenity are the host's.
        self.host = []
        if os.path.exists("/.flatpak-info") and shutil.which("flatpak-spawn"):
            self.host = ["flatpak-spawn", "--host"]
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME")}
        self.icon = None
        self.notify_id = None
        self.zenity = None
        self.last_pct = -1
        self.last_notified = -100

    def _run(self, argv, **kw):
        return subprocess.run(self.host + argv, env=self.env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=10, text=True, **kw)

    def notify(self, body, urgency="normal", pct=None):
        if not self.enabled:
            return
        argv = ["notify-send", "-a", "MyWhoosh", "-u", urgency, "-p"]
        if self.icon:
            argv += ["-i", self.icon]
        if self.notify_id:
            argv += ["-r", self.notify_id]
        if pct is not None:
            argv += ["-h", "int:value:%d" % pct]
        argv += [self.title, body]
        try:
            r = self._run(argv)
            if r.returncode != 0 and not self.notify_id:
                # notify-send before 0.7.9 has neither -p nor -r.
                argv.remove("-p")
                r = self._run(argv)
            if r.stdout.strip().isdigit():
                self.notify_id = r.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass

    def open_progress(self, text):
        if not self.enabled:
            return
        try:
            if self._run(["sh", "-c", "command -v zenity"]).returncode != 0:
                return
            # Without --window-icon, zenity shows its own stock progress icon.
            icon = ["--window-icon", self.icon] if self.icon else []
            self.zenity = subprocess.Popen(
                self.host + ["zenity", "--progress", "--title", self.title, "--text", text,
                             "--percentage", "0", "--auto-close", "--width", "420"] + icon,
                env=self.env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, text=True)
        except (OSError, subprocess.SubprocessError):
            self.zenity = None

    def progress(self, pct, text):
        """pct 0-100.  Raises Cancelled when the user closed the window."""
        pct = max(0, min(100, int(pct)))
        if self.zenity:
            if self.zenity.poll() is not None:
                raise Cancelled()
            if pct != self.last_pct:
                try:
                    # 100 would auto-close the window before the swap is done.
                    self.zenity.stdin.write("%d\n# %s\n" % (min(pct, 99), text))
                    self.zenity.stdin.flush()
                except (BrokenPipeError, OSError):
                    raise Cancelled()
        elif pct >= self.last_notified + 10:
            self.notify(text, pct=pct)
            self.last_notified = pct
        self.last_pct = pct

    def close(self):
        if self.zenity:
            with contextlib.suppress(Exception):
                self.zenity.stdin.close()
            with contextlib.suppress(Exception):
                self.zenity.terminate()
                self.zenity.wait(2)
            self.zenity = None


# ------------------------------------------------------------------ versions
def parse_version(s):
    return tuple(int(x) for x in s.split("."))


def vstr(v):
    return ".".join(map(str, v))


def read_identity(game):
    """(Identity Name, version tuple) from an installed or extracted package."""
    root = ET.parse(str(game / "AppxManifest.xml")).getroot()
    ident = next(el for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "Identity")
    return ident.get("Name"), parse_version(ident.get("Version"))


def icon_of(game):
    """The game's own logo, as its manifest names it, or None."""
    try:
        root = ET.parse(str(game / "AppxManifest.xml")).getroot()
        rel = next(el.get("Square44x44Logo") for el in root.iter()
                   if el.tag.rsplit("}", 1)[-1] == "VisualElements" and el.get("Square44x44Logo"))
    except (OSError, ET.ParseError, StopIteration):
        return None
    logo = game / rel.replace("\\", "/")
    # The 48 px rendering, when the package has one, beats scaling up 44.
    sized = logo.with_name(logo.stem + ".targetsize-48" + logo.suffix)
    return str(sized if sized.is_file() else logo) if logo.is_file() else None


def load_downloader():
    """ms_store_download.py, shipped beside this script (dist.sh vendors it).
    By path: `python3 -I` leaves this directory off sys.path."""
    global msd
    if not DOWNLOADER.is_file():
        raise Failed("%s is missing" % DOWNLOADER)
    spec = importlib.util.spec_from_file_location("ms_store_download", str(DOWNLOADER))
    msd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(msd)
    msd.TIMEOUT = NET_TIMEOUT


msd = None


def latest(store_id, name, source):
    """The newest package the Store offers, as {version, sha256, url, download_size}.
    One source at a time, so the log says which answered and how fast."""
    errors = []
    for src, label in (("catalog", "Store catalog"), ("soap", "Windows Update")):
        if source not in ("auto", src):
            continue
        t = time.monotonic()
        try:
            got = msd.get_latest_package(store_id, name, architecture=ARCH, source=src,
                                         catalog_timeout=CHECK_TIMEOUT)
        except msd.StoreError as e:
            errors.append(str(e))
            if src == "catalog" and source == "auto":
                log("Store catalog unavailable (%s), asking Windows Update", e)
            continue
        log("%s: %s %s (%.2f s)", label, name, got["Version"], time.monotonic() - t)
        return {"version": parse_version(got["Version"]), "sha256": got["Digest"],
                "url": got["Url"], "download_size": got["Size"]}
    raise Failed("could not reach the Store -- " + "; ".join(errors))


# ------------------------------------------------------------------- layout
def find_game(path):
    """The game directory for DIR, which may also be a prefix or its drive_c."""
    path = pathlib.Path(path).resolve()
    if (path / "AppxManifest.xml").is_file() or stage_of(path).is_dir():
        return path
    # A prefix is looked into only through its drive_c, where the installers
    # put the game -- never its top level, where a backup could pass for it.
    base = path / "drive_c" if (path / "drive_c").is_dir() else path
    # A game missing between the two renames of a swap still counts: recover()
    # puts it back.
    found = [n for n in GAME_DIRS
             if (base / n / "AppxManifest.xml").is_file() or stage_of(base / n).is_dir()]
    if len(found) == 1:
        return base / found[0]
    if len(found) > 1:
        raise Failed("%s holds more than one game (%s); name one" % (base, ", ".join(found)))
    raise Failed("no MyWhoosh installation in %s" % path)


def stage_of(game):
    return game.parent / (".%s.update" % game.name)


def game_running(game):
    """Is this game's binary running?  Wine shows the Windows path in cmdline,
    and that holds the game's directory name -- MyWhoosh or MyWhooshHD."""
    needle = ("\\%s\\%s" % (game.name, EXE_TO.replace("/", "\\"))).lower().encode()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                if needle in f.read().lower():
                    return True
        except OSError:
            pass
    return False


def recover(game):
    """Finish or roll back whatever an earlier run left half done."""
    stage = stage_of(game)
    new, old, ready = stage / "new", stage / "old", stage / "ready"
    if ready.is_file() and new.is_dir():
        want = parse_version(ready.read_text().strip())
        have = read_identity(game)[1] if (game / "AppxManifest.xml").is_file() else None
        if have is None or want > have:
            log("finishing the interrupted update to %s", vstr(want))
            swap(game)
            return
    # Anything else in new/ is unfinished, or no newer than the game.
    if ready.exists():
        ready.unlink()
    if not game.exists() and old.is_dir():
        log("putting back %s after an interrupted update", game.name)
        old.rename(game)
    for leftover in (old, new):
        if leftover.exists() and game.exists():
            shutil.rmtree(str(leftover), ignore_errors=True)


def swap(game):
    stage = stage_of(game)
    new, old, ready = stage / "new", stage / "old", stage / "ready"
    if game.exists():
        if old.exists():
            shutil.rmtree(str(old))
        game.rename(old)
    new.rename(game)
    ready.unlink()
    shutil.rmtree(str(stage), ignore_errors=True)


def du(path):
    total = 0
    for root, _dirs, files in os.walk(str(path)):
        for f in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, f)).st_size
    return total


def need_space(where, needed, what):
    free = shutil.disk_usage(str(where)).free
    if free < needed:
        raise Failed("not enough disk space to %s: %.1f GB needed, %.1f GB free in %s"
                     % (what, needed / 1e9, free / 1e9, where))


# ---------------------------------------------------------------- the update
def download_progress(ui, label):
    """A download_package callback: the progress window, and a log line per quarter."""
    start, last_log = [], [0]

    def progress(done, total):
        if not start:
            start[:] = [time.monotonic(), done]
        pct = 100 * done / total if total else 0
        mbps = (done - start[1]) / 1e6 / max(time.monotonic() - start[0], 0.001)
        ui.progress(pct * 0.8, "%s: %.0f / %.0f MB (%.0f MB/s)" % (label, done / 1e6, total / 1e6, mbps))
        if done > start[1] and pct >= last_log[0] + 25:
            log("downloaded %d%% (%.0f MB, %.0f MB/s)", pct, done / 1e6, mbps)
            last_log[0] = int(pct // 25 * 25)
    return progress


def update(game, ui, source, check_only):
    name, have = read_identity(game)
    if name not in EDITIONS:
        raise Failed("unknown package %s in %s" % (name, game))
    store_id, title = EDITIONS[name]
    ui.title = title
    ui.icon = icon_of(game)

    want = latest(store_id, name, source)
    if want["version"] <= have:
        log("%s %s is the Store's latest", title, vstr(have))
        # Nothing staged for an update that is no longer needed is any use.
        if not check_only:
            shutil.rmtree(str(stage_of(game)), ignore_errors=True)
        return 0
    log("%s %s is installed, the Store has %s", title, vstr(have), vstr(want["version"]))
    if check_only:
        return 10
    if game_running(game):
        log("%s is running; not replacing it under the game", title)
        return 1
    if not want.get("sha256"):
        raise Failed("the Store gave no SHA-256 for the package; not installing it unchecked")

    ui.notify("Updating to %s before launch..." % vstr(want["version"]))
    ui.open_progress("Updating %s to %s..." % (title, vstr(want["version"])))
    ui.progress(0, "Asking the Store for the download...")

    stage = stage_of(game)
    stage.mkdir(exist_ok=True)
    target = vstr(want["version"])
    package = stage / ("%s_%s_%s.msix" % (name, target, ARCH))
    part = package.with_name(package.name + ".part")
    for stale in stage.glob("*.msix*"):
        if stale not in (package, part):
            stale.unlink()

    # A package verified by an earlier run that stopped before the swap.  Its
    # rename may have reached the disk before its data did, so it is checked
    # again -- seconds, against minutes for the download.  (Checked here, not
    # left to download_package, so that reusing it skips Windows Update.)
    if package.exists() and msd.verify_sha256(package, want["sha256"]):
        log("reusing the package downloaded earlier")
    else:
        # Disk: the package, then the game it unpacks to, while the old one
        # is still there.  The real unpacked size is checked once it is in.
        size = want.get("download_size") or 0
        remaining = max(size - (part.stat().st_size if part.exists() else 0), 0)
        need_space(stage, remaining + max(du(game), size), "download the update")

        url = want.get("url")
        if not url:
            wu = latest(store_id, name, "soap")
            if wu["version"] != want["version"]:
                raise Failed("the Store catalog has %s but Windows Update offers %s; try again later"
                             % (target, vstr(wu["version"])))
            url = wu["url"]

        if part.exists():
            log("resuming the download at %.0f MB", part.stat().st_size / 1e6)
        t = time.monotonic()
        msd.download_package({"FileName": package.name, "Url": url, "Digest": want["sha256"]},
                             stage, download_progress(ui, "Downloading %s %s" % (title, target)))
        log("downloaded and verified in %.0f s", time.monotonic() - t)

    new = stage / "new"
    if new.exists():
        shutil.rmtree(str(new))
    new.mkdir()
    t = time.monotonic()
    # As the installer does: every member, flat into the directory.
    msd.extract_package(package, new, lambda done, total: ui.progress(
        80 + 18 * done / max(total, 1), "Unpacking: %d%%" % (100 * done // max(total, 1))))
    # The package stays until the swap, which removes the whole stage: were
    # this run killed before it, the next would not download it again.
    log("unpacked in %.0f s", time.monotonic() - t)

    got_name, got = read_identity(new)
    if (got_name, got) != (name, want["version"]):
        raise Failed("the package holds %s %s, not %s %s" % (got_name, vstr(got), name, target))
    if (new / EXE_FROM).is_file():
        (new / EXE_FROM).rename(new / EXE_TO)
    if not (new / EXE_TO).is_file():
        raise Failed("the package has no %s" % EXE_FROM)

    # Everything above must be on disk before the marker that says so is.
    ui.progress(99, "Installing...")
    os.sync()
    (stage / "ready").write_text(target + "\n")
    os.sync()
    if game_running(game):
        log("%s started meanwhile; the update is installed at its next launch", title)
        return 1
    swap(game)
    log("%s updated from %s to %s", title, vstr(have), target)
    ui.close()
    ui.notify("Updated to %s" % target)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dir", help="the game directory, or the prefix or drive_c holding it")
    ap.add_argument("--check", action="store_true", help="only report; exit 10 if an update is available")
    ap.add_argument("--no-gui", action="store_true", help="no progress window or notifications")
    ap.add_argument("--source", choices=("auto", "catalog", "soap"), default="auto",
                    help="where to ask for the version (default: the catalog, then Windows Update)")
    args = ap.parse_args()

    ui = Ui(not args.no_gui and not args.check, "MyWhoosh")
    try:
        game = find_game(args.dir)
        load_downloader()
        stage = stage_of(game)
        with open(str(game.parent / (".%s.update.lock" % game.name)), "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                    log("another update of %s is running; leaving it to that one", game.name)
                    return 1
                raise
            if stage.exists():
                recover(game)
            return update(game, ui, args.source, args.check)
    except Cancelled:
        log("update cancelled; the download resumes at the next launch")
        ui.close()
        ui.notify("Update cancelled -- starting the installed version, which the game may refuse to run",
                  urgency="critical")
        return 1
    except Exception as e:
        # Including OSError, and anything unforeseen: the installed game is
        # whole -- every step above leaves it so -- and it launches.
        log("update failed: %s: %s", type(e).__name__, e)
        ui.close()
        if ui.last_pct >= 0:
            ui.notify("Update failed -- starting the installed version. See %s" % LOG, urgency="critical")
        return 1
    finally:
        ui.close()


if __name__ == "__main__":
    sys.exit(main())
