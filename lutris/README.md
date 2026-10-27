# lutris — installing and launching from Lutris

Two installer scripts and the small script that runs beside the game.

| File | What it is |
|---|---|
| `mywhoosh.yml` | Installs MyWhoosh from the Microsoft Store into a Wine prefix, with Bluetooth |
| `mywhoosh-hd.yml` | The same for MyWhoosh HD — a different store id and directory, and its own vkd3d-proton: HD renders with D3D12, which Lutris' vkd3d v2.14 crashes at startup |
| `mywhoosh-ble.sh` | Runs on the Linux side: checks the setup at install time, updates the game and starts the helper before it, stops the helper after |
| `mywhoosh-update.py` | Brings an installed game up to the Microsoft Store's version; standalone, so anything that launches the game can run it |
| `debug.sh` | Builds, removes the previous debug install, and runs `mywhoosh.yml` (`--hd`: `mywhoosh-hd.yml`) rewritten to install this checkout's build and the MSIX cached in `../downloads/`: a fresh install to try before a release |

```sh
lutris -i lutris/mywhoosh.yml
```

## Why no game file is patched

An older approach edited `WindowsConnectivity.dll` to get past a Bluetooth check
at startup (`../patch/`). The installers here **must not**: the game hashes that
DLL and silently stops loading it when a byte differs, and the Bluetooth support
needs it loaded — its Bluetooth code is what the shim answers. So instead of
patching a game file, the installer unpacks three assemblies into
`$GAMEDIR/bleshim` and points mono at them through the game's environment:

```yaml
MONO_PATH: Z:$GAMEDIR/bleshim
```

mono searches `MONO_PATH` first for a referenced assembly by simple name, and
`Windows` is exactly the name the game's metadata asks for. No file in the game
directory is touched.

Not the prefix's own wine-mono tree (`drive_c/windows/mono/mono-2.0/lib`),
where v0.1.0 put them: current Wine and Proton builds — GE-Proton, wine-ge 8 —
run wine-mono from the runner's directory and install none into the prefix, so
that directory is one nothing reads. `MONO_PATH` works with either layout, and
beats a copy in the prefix's tree when there is one. It must be a Windows path —
mono inside the prefix reads `/home/...` as `C:\home\...` — hence `Z:`, which
Wine maps to `/`.

`mywhoosh-ble.sh start` checks this at every launch, and says how to fix an
install that has neither `MONO_PATH` nor a real mono tree holding the shim:
that game dies at its first Bluetooth call with a `TypeLoadException` naming
`Windows`.

## How the helper gets started

The game's Bluetooth calls are answered inside the prefix, but the adapter is
reached from outside it, because BlueZ is on D-Bus and Wine's winsock has no
`AF_UNIX`. So `blehelper.py` has to be running whenever the game is. The
installer writes two of Lutris' own system options into the game's config:

```yaml
prelaunch_command: $GAMEDIR/bleshim/mywhoosh-ble.sh start
prelaunch_wait: true
postexit_command: $GAMEDIR/bleshim/mywhoosh-ble.sh stop
```

`prelaunch_wait` is on so the helper is listening before the game's first scan;
`start` returns as soon as the port is up, or after saying why it could not.
Lutris hands a pre-launch script the *game's* environment, which carries the
Lutris runtime's `LD_LIBRARY_PATH` — poison for a system `python3` importing
`dbus` and `gi` — so `mywhoosh-ble.sh` clears it before starting the helper.

Nothing in that script ever exits non-zero. A Bluetooth problem must not stop
the game from launching, and a problem at install time must not throw away a
finished download; it says what is wrong (in the log, and through `notify-send`
when there is a desktop to say it to) and gets out of the way.

## Updating the game

MyWhoosh refuses to start once the Store has a newer build, and a Lutris install
has no Store behind it: the installer downloads the package once. So `start`
runs `mywhoosh-update.py` before anything else. It reads the edition from the
installed `AppxManifest.xml` — `<Identity Name=… Version=…>` — so no game
release ever needs a change here.

**Where the Store's version comes from.** Measured from here:

| Source | Time | What it gives |
|---|---|---|
| `displaycatalog.mp.microsoft.com/v7.0/products/<id>` | ~0.2 s | `PackageFullName` (`Name_6.2.3.0_x64__…`), SHA-256, sizes; TLS verifies |
| Windows Update SOAP (what the downloader does) | 2.3–8 s | the same name and SHA-256, and the only download URL |
| The game's own check | — | MyWhoosh's backend (`services`/`service20.mywhoosh.com`), behind Firebase auth, with its paths in the encrypted pak; it tells the game the version it *requires*, not what the Store can deliver |

The catalog is the check; SOAP runs only when there is something to download,
or as the check when the catalog is unreachable. The package URL is plain
`http`, and `fe3.delivery.mp.microsoft.com` chains to a Microsoft root no Linux
trust store has, so the downloader talks to it unverified. The package is
installed only if its SHA-256 matches the catalog's.

The updater has no network code of its own. The catalog check, the SOAP
lookup, the resumable download and the extraction all go through
[ms-store-download](https://github.com/Dj0ulo/ms-store-download)'s library API
(`get_catalog_packages`, `get_download_links`, `download_package`,
`extract_package`). What remains here is MyWhoosh-specific: editions, the exe
rename, the staging and swap, and the UI. The downloader ships in the archive
beside the updater, pinned by commit and hash in `../dist.sh`.

**Never half a game.** The package is downloaded and unpacked in
`drive_c/.MyWhoosh.update/` (`.MyWhooshHD.update/`), on the same filesystem as
the game, and the exe is renamed there exactly as the installer renames it.
Once all of that is synced, a `ready` marker is written, and the swap is two
renames: game → `old/`, `new/` → game. Every run first finishes or rolls back
what the last one left: a download resumes with a `Range` request, a half-unpacked
`new/` is deleted, and a missing game is put back from `new/` (if `ready`) or
`old/`. No game file is ever written in place. The game directory is replaced
whole, and the result is byte-identical to a fresh install.

**What the user sees.** Lutris 0.5 runs a waited pre-launch command on its GTK
main thread and keeps its output for its own debug log, so during a download its
window is frozen and nothing printed reaches the screen. The updater therefore
opens its own `zenity --progress` window (with Cancel), and sends notifications
through `notify-send` — through the host under Flatpak, like everything else
here. Both are skipped when there is no display. Everything is logged to
`MYWHOOSH_SHIM_LOG` with the tag `updater`.

**Failures launch the game.** No network, a Store that does not answer, a full
disk, a bad digest, Cancel: each is logged, and the installed game starts. It
may refuse to run, but that is the game's decision, not ours. The update is
also skipped while that game's binary is running.

For a container or a script, `mywhoosh-update.py DIR` works on its own — `DIR`
being the prefix, its `drive_c` or the game directory. `--check` only reports
(exit 10 when an update is available), `--no-gui` drops the window and the
notifications, and `--source catalog|soap` forces one source.

## Flatpak Lutris

The sandbox is no place to reach Bluetooth from. The Flathub manifest grants no
`--socket=system-bus` and no `--system-talk-name=org.bluez`, so inside it BlueZ
is not merely unauthorised — it is absent, and a helper started there would
report no adapter no matter what is installed.

Two other permissions it *does* grant make that a detour rather than a wall:

| Permission | What it buys |
|---|---|
| `--talk-name=org.freedesktop.Flatpak` | `flatpak-spawn --host` works, so the helper runs on the host, with the host's `python3` and the host's system bus |
| `--share=network` | the host's loopback is the same loopback the shim inside Wine dials, so nothing about the protocol changes |

So `mywhoosh-ble.sh` checks for `/.flatpak-info` and, when it finds one, routes
every command that wants Linux rather than the sandbox — the dependency probes,
the BlueZ probe, `notify-send`, and the helper itself — through
`flatpak-spawn --host`. The helper is started with `--watch-bus` so it dies with
the `flatpak-spawn` that carries it: that process is what the pidfile holds, and
killing it is the only handle `stop` has on a process in another namespace.

Game files are under `~/Games`, which the manifest maps at its real path, so the
helper's path is valid on the host as written.

Two consequences worth knowing:

- The *host* is what needs `dbus-python` and `PyGObject`. A distro package of
  Lutris depends on both, so a native install is always ready; a Flatpak pulls
  in neither, and the host may genuinely lack them. `check` says so, with the
  command for each distro.
- With `flatpak-spawn` missing from the sandbox there is no way out at all, and
  `check` says that instead of blaming BlueZ.

Two environment variables, settable in Lutris under *Configure → System options
→ Environment variables*:

| Variable | Default | |
|---|---|---|
| `MYWHOOSH_BLE_ADAPTER` | `auto` | Use the sole powered BlueZ adapter, or `hci0` if several are; set an explicit `hciN` to choose another |
| `MYWHOOSH_BLE_PORT` | `27019` | The loopback port the two halves meet on |
| `MYWHOOSH_SHIM_LOG` | `$GAMEDIR/bleshim/session.log` | Where every layer logs |
| `MYWHOOSH_AUTO_UPDATE` | `1` | `0` skips the game update at launch |

The log is the diagnostic tool: the helper, the shim inside the prefix and the
export shim all write to that one file, each with its own tag, and the
install-time `check` writes its report there too. The previous
run is kept beside it as `session.log.prev`.

Adapter selection runs on every launch, so a controller renumbered from `hci0`
to `hci5` still works. Explicit overrides are respected and must name a powered
adapter. If a controller resets during a ride, the helper reports the loss and
clears its old scan and subscription state. A controller that returns under the
same name is used again without a restart; one that returns renumbered needs the
game closed and relaunched. See [Bluetooth recovery](../bleshim/README.md#bluetooth-recovery)
for scans that return nothing even outside MyWhoosh.

## Changing the shim

The installer downloads one archive — `mywhoosh-bleshim-<tag>.tar.gz`, a
GitHub release asset holding the three assemblies, `blehelper.py`, this
directory's `mywhoosh-ble.sh` and `mywhoosh-update.py`, and the pinned Store
downloader — and `variables.shim_release` at the top of each `.yml` is the tag
it fetches. One archive rather than a handful of raw URLs because the
helper and the shim inside the prefix speak a protocol between themselves:
fetched separately, they eventually arrive as different builds of it.

The `.yml` files are release assets too, so the install URL is a release rather
than a branch tip:

```sh
lutris -i https://github.com/Dj0ulo/mywhoosh-linux/releases/latest/download/mywhoosh.yml
```

So a change under `../bleshim/src/`, `../exportshim/`, `blehelper.py`,
`mywhoosh-ble.sh` or `mywhoosh-update.py` reaches nobody until a release is cut.
That includes the updater itself: an install from before it has no update path
except a reinstall.

```sh
../dist.sh                 # build and pack into ../build/, to test by hand
../dist.sh --release v2    # bump the .yml pins, then publish with gh
```

`--release` bumps `shim_release` in both `.yml` files and stops if that leaves
the tree dirty — `gh` tags the release at `HEAD`, and the tag has to point at
the commit whose installers pin it. Commit, run it again, and it uploads the
archive and both installers. Keep `GAME_LIBS` set while building: `../bleshim/build.sh`
then checks the shim's surface against the game's own metadata, and a member
missing from a release build is a `MissingMethodException` in the middle of
someone's ride.

The archive is unpacked into `$GAMEDIR/bleshim`, and everything stays there:
the assemblies mono loads through `MONO_PATH`, the helper, and `MANIFEST`,
which is the only thing in a prefix that says which build it has.

`lutris -i mywhoosh.yml` from a checkout is still the way to install into a
fresh prefix, but note it fetches the pinned *release*, not what you just
built — a working tree changes nothing about what lands in the prefix. To put
your own build in an existing prefix, use `../bleshim/install.sh` and
`../exportshim/install.sh`, which copy from `build/` directly.
