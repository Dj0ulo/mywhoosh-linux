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

## What the installer does differently from `main`

`main`'s installer patches `WindowsConnectivity.dll` to get past a Bluetooth
check at startup. This one **must not**: the game hashes that DLL and silently
stops loading it when a byte differs, and this branch needs it loaded — its
Bluetooth code is what the shim answers. So instead of patching a game file,
the installer adds three assemblies to the prefix's own wine-mono tree
(`drive_c/windows/mono/mono-2.0/lib`), which is where mono probes for a
referenced assembly by simple name. No file in the game directory is touched.

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

The installer downloads the assemblies from `../dist/` by raw URL, and
`variables.shim_repo` at the top of each `.yml` is the branch it fetches from —
one line to change when this branch merges. After changing anything under
`../bleshim/src/` or `../exportshim/`, run `../dist.sh` and commit `dist/`, or
users will keep installing the old build.
