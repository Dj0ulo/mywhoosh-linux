# lutris — installing and launching from Lutris

Two installer scripts and the small script that runs beside the game.

| File | What it is |
|---|---|
| `mywhoosh.yml` | Installs MyWhoosh from the Microsoft Store into a Wine prefix, with Bluetooth |
| `mywhoosh-hd.yml` | The same for MyWhoosh HD — a different store id and directory, otherwise identical |
| `mywhoosh-ble.sh` | Runs on the Linux side: checks the setup at install time, starts and stops the helper around the game |

```sh
lutris -i lutris/mywhoosh.yml
```

## Why no game file is patched

An older approach edited `WindowsConnectivity.dll` to get past a Bluetooth check
at startup (`../patch/`). The installers here **must not**: the game hashes that
DLL and silently stops loading it when a byte differs, and the Bluetooth support
needs it loaded — its Bluetooth code is what the shim answers. So instead of
patching a game file, the installer adds three assemblies to the prefix's own
wine-mono tree (`drive_c/windows/mono/mono-2.0/lib`), which is where mono probes
for a referenced assembly by simple name. No file in the game directory is
touched.

That also means the prefix must have wine-mono in it — the default when Lutris
creates a prefix. `mywhoosh-ble.sh check` says so plainly if it is missing.

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
| `MYWHOOSH_BLE_ADAPTER` | `hci0` | Which BlueZ adapter to use |
| `MYWHOOSH_BLE_PORT` | `27019` | The loopback port the two halves meet on |
| `MYWHOOSH_SHIM_LOG` | `$GAMEDIR/bleshim/session.log` | Where every layer logs |

The log is the diagnostic tool: the helper, the shim inside the prefix and the
export shim all write to that one file, each with its own tag. The previous
run is kept beside it as `session.log.prev`.

## Changing the shim

The installer downloads one archive — `mywhoosh-bleshim-<tag>.tar.gz`, a
GitHub release asset holding the three assemblies, `blehelper.py` and this
directory's `mywhoosh-ble.sh` — and `variables.shim_release` at the top of each
`.yml` is the tag it fetches. One archive rather than five raw URLs because the
helper and the shim inside the prefix speak a protocol between themselves:
fetched separately, they eventually arrive as different builds of it.

The `.yml` files are release assets too, so the install URL is a release rather
than a branch tip:

```sh
lutris -i https://github.com/Dj0ulo/mywhoosh-linux/releases/latest/download/mywhoosh.yml
```

So a change under `../bleshim/src/`, `../exportshim/`, `blehelper.py` or
`mywhoosh-ble.sh` reaches nobody until a release is cut:

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

The archive is unpacked into `$GAMEDIR/bleshim`, and the three assemblies are
copied on to the prefix's mono tree from there. `MANIFEST` stays behind, which
is the only thing in a prefix that says which build it has.

`lutris -i mywhoosh.yml` from a checkout is still the way to install into a
fresh prefix, but note it fetches the pinned *release*, not what you just
built — a working tree changes nothing about what lands in the prefix. To put
your own build in an existing prefix, use `../bleshim/install.sh` and
`../exportshim/install.sh`, which copy from `build/` directly.
