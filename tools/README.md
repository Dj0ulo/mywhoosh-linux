# tools — reading what the game actually does

Everything in this repository is built against MyWhoosh's own
`WindowsConnectivity.dll`: what it calls, in what order, with what signatures.
These are the three small programs that answer those questions, so decisions
come from the game's bytecode rather than from documentation or guesswork.

They all run **inside a Wine prefix, under wine-mono**, not under the host's
Mono. Two reasons: the host lacks `System.ServiceProcess`, which several of the
game's types reference (resolving a method fails before any code is read), and
type sizes have to come from the runtime the game will actually use.

## ILDump — what a method does

A minimal IL disassembler. Point it at a type and method:

```sh
./ildump.sh BluetoothManager.BluetoothProgram Advertisement_Received
./ildump.sh BluetoothManager.BluetoothProgram          # every method of a type
./ildump.sh FunctionsManager.MyWhoosh WD_GetDirconServiceAvailability
```

This is how the non-obvious behaviours were found — for example that the game
discards any Bluetooth advertisement carrying no service UUIDs, which otherwise
looks exactly like a scan that found nothing.

## SigDump / AllSigs — what a signature says about marshalling

Exact parameter attributes, `[MarshalAs]` (mono synthesises it from
`FieldMarshal`, so reflection sees it) and native struct layout:

```sh
mcs -platform:x64 -out:SigDump.exe SigDump.cs
mcs -platform:x64 -out:AllSigs.exe AllSigs.cs
WINEPREFIX=<prefix> wine SigDump.exe WindowsConnectivity.dll   # the four device-list exports
WINEPREFIX=<prefix> wine AllSigs.exe WindowsConnectivity.dll   # all 98 exports, odd ones flagged
```

`AllSigs` is what established that exactly four of the game's 98 exports take an
array by reference — the shape wine-mono cannot marshal, and the reason
`../exportshim/` exists. `SigDump` measured the struct those four return: 64
bytes under wine-mono.

## Types — finding the class to look at

`Types.cs` is the same idea, smaller: it lists every type in an assembly, which
is how you find the name to point `ildump.sh` at.

```sh
mcs -out:Types.exe Types.cs
WINEPREFIX=<prefix> wine Types.exe WindowsConnectivity.dll
```

## Optional Realtek Bluetooth autosuspend workaround

For a Realtek USB Bluetooth adapter (`0bda:8771`) that finds no sensors, first
check whether discovery also fails outside MyWhoosh. Close the game and helper,
wake the trainer, then run:

```sh
bluetoothctl --timeout 20 scan le
journalctl -k --since '-5 min' | grep -Ei 'Bluetooth|hci[0-9]|btusb'
```

The commands below are run from the repository root. They modify host USB power
management and are optional; the Lutris installer does not apply this rule.

Command timeouts and USB resets here are below the Wine shim. In one tested
Realtek `0bda:8771` setup, disabling USB autosuspend stopped repeated resets,
and a subsequent Bluetooth power cycle restored advertisements. This is an
observed workaround, not proof that every timeout on this chipset has that cause.

For affected hardware, the optional rule below disables autosuspend for USB
devices with that vendor/product ID, regardless of port or `hciN` numbering:

```sh
sudo install -m 0644 tools/99-mywhoosh-realtek-bluetooth.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
```

It applies on the next device connection or boot. Close Bluetooth applications
before reconnecting the dongle. To apply it to an already attached dongle without
disconnecting it:

```sh
sudo udevadm trigger --action=add --subsystem-match=usb --attr-match=idVendor=0bda --attr-match=idProduct=8771
sudo udevadm settle
```

After closing the game and helper, if scanning remains stuck, cycle Bluetooth
power and retry the scan. This disconnects Bluetooth devices using the selected
controller; with multiple controllers, select the intended one in `bluetoothctl`.

```sh
bluetoothctl power off
bluetoothctl power on
```

The launcher never resets the adapter automatically during a ride. To undo the
persistent workaround, remove `/etc/udev/rules.d/99-mywhoosh-realtek-bluetooth.rules`,
reload udev rules, and reconnect the dongle. Other power-management software may
also change USB power settings, so check the device's `power/control` if failures
return. The kernel documents the setting under
[USB power management](https://docs.kernel.org/driver-api/usb/power-management.html).
