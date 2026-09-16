# MyWhoosh on Linux

[MyWhoosh](https://www.mywhoosh.com/) is a free indoor cycling and running app:
you ride a smart trainer at home and it puts you in a virtual world, alone or in
a race with other people.

It ships for Windows, macOS, iOS and Android — but not Linux. This repository
installs it on Linux with [Lutris](https://lutris.net/) and
[Wine](https://www.winehq.org/), **and connects your Bluetooth sensors to it**
through your computer's own Bluetooth adapter. Your trainer and heart-rate strap
pair in the game's normal device screen. No phone, no companion app.

---

## Before you start

- A 64-bit Linux system with [Lutris](https://lutris.net/downloads/) installed
- `python3`
- Working Bluetooth: if `bluetoothctl scan on` shows your trainer when you
  pedal, you are ready

The install checks all of this at the end and tells you exactly what is missing,
so you do not have to get it right in advance.

> **Flatpak Lutris users:** the Flatpak sandbox cannot reach Bluetooth, so the
> Bluetooth helper is run on your host system instead. That works, but your
> *host* then needs two Python packages: `python3-dbus` and `python3-gi` on
> Debian/Ubuntu, `python3-dbus` and `python3-gobject` on Fedora, `python-dbus`
> and `python-gobject` on Arch. A distro package of Lutris already depends on
> both, so this only applies to the Flatpak.

---

## Install

There is nothing to clone. Pick your edition and run one command:

**MyWhoosh**

```bash
lutris -i https://github.com/Dj0ulo/mywhoosh-linux/releases/latest/download/mywhoosh.yml
```

**MyWhoosh HD** — the same game with higher-resolution assets, and a separate
install:

1. MyWoosh with low quality textures:

```bash
lutris -i https://github.com/Dj0ulo/mywhoosh-linux/releases/latest/download/mywhoosh-hd.yml
```
2. MyWoosh-HD with high quality textures:

```bash
lutris -i lutris/mywhoosh-hd.yml
```

Lutris then does everything itself:

1. creates a 64-bit Wine prefix,
2. downloads the MyWhoosh package straight from the Microsoft Store,
3. installs it into the prefix,
4. adds the Bluetooth support to the prefix and puts the Linux-side helper next
   to the game,
5. wires that helper to start and stop with the game, and checks your setup.

Installing both editions side by side is fine — they are separate games in
Lutris, with separate prefixes.

## Play

Launch it from Lutris like any other game. The Bluetooth helper starts with it
and stops when you quit; there is nothing to run by hand.

## Connect your trainer and sensors

Pair them from MyWhoosh's own device screen, exactly as you would on Windows.

**Wake the sensor first** — pedal a turn, or touch the strap. A trainer that is
asleep does not advertise itself, so nothing can find it: this is the single
most common reason a device does not show up.

Several sensors at once are fine (a trainer and a heart-rate strap, say); each
gets its own connection.

## When something does not work

Everything writes to one log file, inside the game's directory:

```
<game directory>/bleshim/session.log
```

That is the first thing to look at, and the previous run is kept beside it as
`session.log.prev`.

| What you see | What it usually means |
|---|---|
| The game says Bluetooth is off | The adapter is off (`bluetoothctl power on`), or the helper did not start — the log says which |
| Your trainer never appears in the scan | It is asleep. Pedal a turn, then scan again |
| Phones and watches never appear | Normal. The game only lists devices advertising a fitness service |
| Nothing at all in the log | The helper is not running; on Flatpak Lutris, check the two host packages above |

[`bleshim/README.md`](bleshim/README.md) goes further, and
[`lutris/README.md`](lutris/README.md) covers the install itself — which adapter
is used, where things are put, and how the helper is started.

## Using a phone instead

If you would rather bridge your sensors from a phone, the **MyWhoosh Link**
companion app still works and needs nothing from this repository:

- **Android:** [MyWhoosh Link on Google Play](https://play.google.com/store/apps/details?id=com.whoosh.companion)
- **iOS:** [MyWhoosh Link on the App Store](https://apps.apple.com/be/app/mywhoosh-link/id1561724525)

---

## How it works, briefly

MyWhoosh talks to sensors through a Windows API (WinRT) that Wine does not
implement. The game's own Bluetooth code is therefore fine — it is simply
calling something that is not there.

So rather than change the game, this repository supplies the missing piece. The
game asks for a library called `Windows`; it gets one, written here, whose
answers come from BlueZ, the Linux Bluetooth stack. The game cannot tell the
difference, and **no file in the game's directory is ever touched** — which
matters, because MyWhoosh checks its own files and quietly stops using them if
they change.

| Directory | What it is |
|---|---|
| [`lutris/`](lutris/README.md) | The Lutris installers, and the script that runs the helper beside the game |
| [`bleshim/`](bleshim/README.md) | The Bluetooth support: the library the game loads, plus the Linux helper that speaks to BlueZ |
| [`exportshim/`](exportshim/README.md) | Four game entry points Wine's .NET runtime cannot handle, replaced in memory |
| [`winmd/`](winmd/README.md) | The same library with Bluetooth switched off — what the game needs just to start |
| [`tools/`](tools/README.md) | Small programs that read the game's own code, so decisions here are based on it |
| [`patch/`](patch/README.md) | An older, launch-only approach, kept for reference |
| `dist.sh` | Builds and publishes the release the installers download |

Contributions are welcome. [`CLAUDE.md`](CLAUDE.md) is the map for anyone
working on the code.
