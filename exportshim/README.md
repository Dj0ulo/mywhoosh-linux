# exportshim — replacing four broken entry points, in memory

MyWhoosh's `WindowsConnectivity.dll` is a .NET assembly that also exposes 98 C
functions. Four of them are the ones the game's device-list UI polls twice a
second:

```
int WD_GetScannedDevicesList  (out DeviceInformationStruct[] devices)
int WD_GetConnectedDevicesList(out DeviceInformationStruct[] devices)
int BT_GetScannedDevicesList  (out DeviceInformationStruct[] devices)
int BT_GetConnectedDevicesList(out DeviceInformationStruct[] devices)
```

They return an array *by reference*, and wine-mono cannot marshal that shape
from native code back into managed code. It does not fail at load; it compiles a
throw into the wrapper, so the game dies on its first poll:

```
System.Runtime.InteropServices.MarshalDirectiveException:
  Byref array marshalling to managed code is not implemented.
```

This directory fixes that without touching the game: it finds the four function
pointers in the running process and replaces them with managed implementations
of our own, which do the marshalling by hand.

```sh
./build.sh
WINEPREFIX=<prefix> ./install.sh        # --restore removes it
```

It is a library, not a program — something inside the game has to call
`MyWhoosh.ExportShim.Install()`. On this branch that is `../bleshim/src/Loader.cs`,
from a static constructor of the assembly the game loads for Bluetooth. You do
not call it yourself.

## How the replacement works

Each export is a 12-byte stub that jumps through a pointer:

```
48 A1 <abs64>    mov rax, [slot]
FF E0            jmp rax
```

Those slots are the CLI header's **VTableFixups** table. On disk they hold
method tokens; when the assembly loads, the .NET runtime overwrites each with
its own native-to-managed thunk. Writing a different address into a slot
redirects the export — and the game re-reads the slot on every call, so even the
pointers it looked up with `GetProcAddress` at startup follow along. Nothing but
our own memory changes, and no file is touched, which matters because MyWhoosh
hashes `WindowsConnectivity.dll` and stops loading it if a byte differs (see
`../winmd/README.md`).

`ExportShim.cs` decodes each stub to find its slot rather than trusting a fixed
offset, so a game update that shifts the layout still lands correctly; if the
stub shape ever changes it logs the bytes it found and hooks nothing rather than
corrupting a pointer.

What our replacement then does is what the .NET runtime would do on Windows:
call the game's own managed method by reflection, allocate native memory for the
returned elements, copy each one out, store the block's address through the
caller's pointer, and return the count.

## What it looks like when it works

```
[exportshim] BT_GetScannedDevicesList: slot 0x180036080 0x39871b50 -> 0x39879500
             ... hooked 4/4 exports
[exportshim] BT_GetConnectedDevicesList -> 3 device(s) at 0x337e6310
```

`hooked 4/4 exports` is the line to look for. Anything less means the stub shape
was not recognised and the game will crash on its first poll.

## Files

| File | What it is |
|---|---|
| `ExportShim.cs` | Finds the slots, replaces them, marshals the arrays; plus the Dircon-only scan rescue described in `CLAUDE.md` |
| `build.sh` | `mcs` → `build/MyWhooshShim.dll` |
| `install.sh` | Copies it into the prefix's wine-mono tree (`--restore` removes it) |

`CLAUDE.md` has the engineering notes: why the runtime cannot be talked into
doing this itself, the memory-ownership question, and the parts that only apply
to the LAN-sensor stack on the `dev` branch.
