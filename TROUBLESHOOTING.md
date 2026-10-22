# When a sensor does not show up

Work through these in order. Most problems are solved by the first one.

## 1. Wake the sensor

A trainer or strap that is asleep does not advertise itself, so nothing can find
it. Pedal a turn, or touch the strap, then look again.

## 2. Check Bluetooth without the game

Quit MyWhoosh, wake the sensor, and scan from Linux directly:

```sh
bluetoothctl scan on
```

Stop it with Ctrl-C after twenty seconds or so.

- **Your trainer appears:** Bluetooth is fine, and the problem is between the
  game and the helper. Go to step 3.
- **It does not appear:** the game cannot see what Linux cannot see. Nothing in
  this repository will help until this works; see step 4.

## 3. Read the log

Every part of the Bluetooth support writes to one file, each line tagged with
the part that wrote it — `[blehelper]`, `[bleshim]`, `[exportshim]`:

```
<game directory>/bleshim/session.log
```

The previous run is kept beside it as `session.log.prev`.

| What you see | What it usually is |
|---|---|
| No log, or nothing in it | The helper is not running, or the game has not reached its device screen |
| `scanning`, but your trainer never appears | It is asleep (step 1), or Linux cannot see it either (step 2) |
| A phone or watch shows in `bluetoothctl` but not in the game | Normal. The game only lists devices advertising a fitness service |
| The game says Bluetooth is off | The helper is not reachable, or the adapter is off: `bluetoothctl power on` |
| Lutris is a Flatpak and there is no adapter | The host needs two Python packages; see the Flatpak note in the [README](README.md#before-you-start) |
| The wrong adapter is used, with more than one | Set `MYWHOOSH_BLE_ADAPTER` (for example `hci1`); see [`lutris/README.md`](lutris/README.md) |

When opening an issue, attach `session.log`.

## 4. When Linux cannot scan either

This is a problem with the computer's Bluetooth, not with MyWhoosh, and the fix
belongs to your system. The kernel log usually says what is going on:

```sh
journalctl -k --since '-5 min' | grep -Ei 'bluetooth|hci[0-9]|btusb'
```

(`sudo` may be needed to read it.)

**Command timeouts or repeated USB resets during a scan** point at the adapter
or its driver. One known cause is USB autosuspend: Linux puts the adapter to
sleep when it looks idle, and some dongles — Realtek ones in particular — do not
always wake up properly. Turning Bluetooth off and on again often gets scanning
back for a while:

```sh
bluetoothctl power off
bluetoothctl power on
```

To keep the adapter awake permanently, disable USB autosuspend for it. How to do
that depends on your distribution and on any power-management tool you run (TLP,
for example, has its own setting). The kernel documents the mechanism under
[USB power management](https://docs.kernel.org/driver-api/usb/power-management.html),
and your distribution's Bluetooth documentation is the place to look for the
specifics.
