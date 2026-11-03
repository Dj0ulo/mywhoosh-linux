# container — MyWhoosh in a podman or docker container

The same install as `../lutris/`, without Lutris: a launcher on the host, an
image with the runtime, and a volume per edition with the prefix and the game.

| File | What it is |
|---|---|
| `mywhoosh` | The host launcher: picks podman or docker, passes the display, sound, GPU and system bus, and runs the image on the edition's volume |
| `Dockerfile` | The runtime: WineHQ wine-staging 11.20, wine-mono 11.3.0, DXVK 2.6.2, vkd3d-proton 3.0.1, python3 with `dbus` and `gi`, zenity, and one release archive |
| `entrypoint.sh` | Inside the container: creates the prefix, installs the game the first time, `mywhoosh-ble.sh start`, the game, `mywhoosh-ble.sh stop` |
| `build.sh` | Builds the image from a release's archive (`./build.sh v0.1.5`), or from this checkout (`./build.sh`) |

```sh
mywhoosh          # MyWhoosh,    in the volume mywhoosh-standard
mywhoosh --hd     # MyWhoosh HD, in the volume mywhoosh-hd
mywhoosh --shell  # a shell in the container, same environment, no game
mywhoosh --dry-run
```

## What is in the image, and what is not

**Not the game.** netbrain/zwift puts Zwift in its image and rebuilds the image
in CI when Zwift updates. MyWhoosh refuses to start the moment the Store has a
newer build, so an image holding the game would be stale for everyone between a
game release and the next image pull. Here the game is in the volume, and
`mywhoosh-update.py` brings it up to the Store's version at each launch, exactly
as it does under Lutris (`../lutris/README.md`, *Updating the game*). The image
changes only with this repository: `../dist.sh --release vX` publishes the
release, and `.github/workflows/image.yml` builds
`ghcr.io/dj0ulo/mywhoosh-linux:vX` from that release's archive. The launcher
pins the same tag (`shim_release=` at its top, bumped by `dist.sh`), so a
launcher and its image are always one release.

**The release archive, whole.** The shim, `blehelper.py`, `mywhoosh-ble.sh`,
`mywhoosh-update.py` and the pinned `ms_store_download.py` come from the one
`mywhoosh-bleshim-<tag>.tar.gz`, never fetched separately: the helper and the
shim speak a protocol between themselves, and the updater and its downloader are
pinned together.

**Which Wine.** WineHQ's wine-staging, pinned to `11.20~trixie-1`:

- it is the Wine the Lutris installs here create their prefixes with;
- it is an apt package, so its libraries come with it. A GE-Proton build (what
  Lutris *runs* the game with by default) expects the Steam or Lutris runtime
  to provide libavcodec, libpcap and a dozen more, which a Debian image does
  not have;
- WineHQ keeps every past build in its repository, so the pin keeps resolving.

It does not ship wine-mono, so the image adds the one its `mscoree` asks for
(11.3.0), by hash, in `share/wine/mono` — the shared location, so creating a
prefix needs no download and no dialog. `MONO_PATH=Z:/data/prefix/bleshim`
points mono at the shim, as the installers do.

## The volume

One per edition, `mywhoosh-standard` and `mywhoosh-hd`, mounted on `/data`,
laid out like a Lutris install so the scripts run unchanged:

```
/data/edition                    which edition this volume holds
/data/home/                      $HOME
/data/prefix/                    WINEPREFIX -- Lutris' $GAMEDIR
/data/prefix/bleshim/            the archive, copied from the image at each launch;
                                 session.log is here
/data/prefix/drive_c/MyWhoosh/   the game (MyWhooshHD for HD)
/data/prefix/drive_c/.MyWhoosh.update/   the updater's stage, while it works
```

The stage is on the volume, beside the game, because the swap is two renames.
So an update needs about twice the game's size free: about 4 GB for MyWhoosh,
9 GB for HD. The updater checks before downloading and says so if there is not.

At each launch the entrypoint copies the image's shim, DXVK and (for HD)
vkd3d-proton into the prefix when they differ, so a new image reaches an old
volume. No game file is ever written: the updater replaces the game directory
whole.

To start over: `podman volume rm mywhoosh-standard` (or `docker volume rm`).

## First launch, updates, interruptions

On a volume with no game, the entrypoint runs
`mywhoosh-update.py --install <product id> /data/prefix/drive_c/MyWhoosh` — the
updater's own install mode, the one the Lutris installers use: same Store
lookup, SHA-256 check, exe rename and staged swap. Then `mywhoosh-ble.sh start`
runs the updater again (a 0.2 s catalog check, or the update) and starts the
helper.

The progress window is the updater's zenity window, on the display the
container already has. Notifications (`notify-send`) are not: they would need
the session bus, a wider door than anything else passed in, for messages that
are also printed to the terminal and to `session.log`.

Ctrl-C, closing the terminal or `docker stop` stops everything in the container
and runs `mywhoosh-ble.sh stop`. A download interrupted that way resumes at the
next launch. `MYWHOOSH_AUTO_UPDATE=0` skips updates, not the first install.

## What the launcher passes in, and why

| | podman (rootless) | docker (rootful) |
|---|---|---|
| user | `--userns=keep-id` | `--user $(id -u):$(id -g)` |
| system bus | `-v /run/dbus:/run/dbus` | the same |
| AppArmor | — | `--security-opt apparmor=unconfined` |
| SELinux enforcing | `--security-opt label=disable` | the same |
| GPU | `--device /dev/dri`, and `--device nvidia.com/gpu=all` for an NVIDIA card (podman 5 or newer) | `--device /dev/dri --group-add <render, video>`, and `--runtime=nvidia --gpus all` for an NVIDIA card |
| X11 | `/tmp/.X11-unix`, `DISPLAY`, the Xauthority file, `--hostname` | the same |
| Wayland (no X11, or `MYWHOOSH_WAYLAND=1`) | the socket, `WAYLAND_DISPLAY` | the same |
| sound | `$XDG_RUNTIME_DIR/pulse` (PulseAudio, or PipeWire's pipewire-pulse) | the same |
| passed through | `MYWHOOSH_AUTO_UPDATE`, `MYWHOOSH_BLE_ADAPTER` | the same |

**The user.** The container runs as you. BlueZ authorises by uid; the display
and sound sockets accept your uid; the volume's files stay yours. Under podman,
keep-id is required, not tidy: dbus-python sends the in-container uid in its
`AUTH EXTERNAL`, and the bus rejects it when it differs from the uid it sees on
the socket. Under docker, without `--user` it works, but BlueZ then sees root.
Porting `blehelper.py` to Gio would remove the keep-id requirement for the bus,
but not for the rest.

**docker and AppArmor.** docker's default AppArmor profile denies every D-Bus
call, `Hello` included. The container is otherwise as confined as any: default
capabilities and seccomp, not privileged.

**Not needed:** `--privileged`, `--net=host`, extra capabilities, host groups
(beyond docker's GPU nodes), D-Bus policy files. The helper and the shim meet on
`127.0.0.1:27019` inside the container's own network namespace, so a helper on
the host does not collide with it.

**NVIDIA.** MyWhoosh refuses an integrated Intel GPU ("your device does not
meet the minimum requirements"), so on a laptop with an NVIDIA card the game
must get that card, and a container gets it only through
[nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
The toolkit describes the card to podman in a file (`/etc/cdi/nvidia.yaml`) whose
current format podman reads only from version 5.0 on; Ubuntu 24.04 and Mint 22
ship podman 4.9, which reports `unresolvable CDI devices`. So with an NVIDIA
card and an older podman, the launcher uses docker when it is installed, and
otherwise stops and says so. It also stops, with the command to run, when the
toolkit is not set up: launching on the integrated GPU would only end at the
game's refusal. `MYWHOOSH_NVIDIA=0` launches anyway.

**The bus directory, not the socket file.** A bind mount of a file holds the
inode it found. When dbus restarts it unlinks the socket and creates a new
one, and a container holding the old one gets `Connection refused` until it is
restarted; the directory shows the new socket. Measured with a private bus
restarted under two running containers, one binding each way. The same holds
for the pulse directory.

## Tested, and not

On Linux Mint 22.3 (Ubuntu 24.04 base, kernel 6.8, BlueZ 5.72, dbus-daemon,
AppArmor), X11, a laptop with an Intel UHD iGPU and a GTX 1650 (driver 580,
nvidia-container-toolkit 1.20):

| | docker 29.1 (rootful) | podman 5.4.2 (rootless) |
|---|---|---|
| fresh volume: prefix, install from the Store | yes | yes |
| download killed mid-way (`docker stop`), resumed at the next launch | yes | — |
| `Version` lowered in the installed manifest: updated at the next launch | yes | — |
| game on the NVIDIA card, shim connected to the helper | yes | yes |
| sensors paired, game quit, helper stopped | yes | |

podman 4.9.3 was tried too: everything up to the game works, on the Intel
GPU, where MyWhoosh shows its minimum-requirements refusal (and once crashed in
`D3D11Query` first). podman 5 was Debian trixie's, unpacked beside the system
one with its own storage, with Debian's crun 1.21 (4.9's crun 1.14 is too old
for it); a distribution shipping podman 5 ships a matching crun.

Not tested:

- **SELinux (Fedora).** A socket under `/run` cannot be relabelled with `:z`, so
  the launcher disables labelling for the container when SELinux is enforcing.
  Untested; something narrower might be `label=type:container_runtime_t`, which
  netbrain/zwift uses, or a udica policy allowing `container_t` to
  `connectto` `system_dbusd_t`.
- **dbus-broker hosts (Fedora, Arch)** with the real system bus. Only a private
  bus was tried, where dbus-broker behaved like dbus-daemon.
- **AMD GPUs**, and **native Wayland** (`MYWHOOSH_WAYLAND=1`, or a session with
  no XWayland).
- **MyWhoosh HD** in the container: the vkd3d-proton wiring is the installer's,
  not yet run here.
