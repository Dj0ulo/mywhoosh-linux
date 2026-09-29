# exportshim — replacing broken entry points, in memory

MyWhoosh's `WindowsConnectivity.dll` is a .NET assembly that also exposes 98 C
functions, and wine-mono gets two kinds of them wrong. Four are the ones the
game's device-list UI polls twice a second:

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

The other twelve return a `float` — `BT_GetHeart`, `BT_GetPower`,
`WD_GetCadence` and the rest. Those work, but the game's engine reads the
answer from the wrong register, and wine-mono leaves a pointer there: see
*Heart rate as a nine-digit number* below.

This directory fixes both without touching the game: it finds the function
pointers in the running process and replaces them with code of our own.

```sh
./build.sh
WINEPREFIX=<prefix> ./install.sh        # --restore removes it
```

It is a library, not a program — something inside the game has to call
`MyWhoosh.ExportShim.Install()`. That is `../bleshim/src/Loader.cs`, from a
static constructor of the assembly the game loads for Bluetooth. You do not call
it yourself.

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

## Heart rate as a nine-digit number

The symptom was a heart-rate monitor that worked everywhere except where it
mattered: the pairing screen showed the right BPM, and the riding HUD showed a
number like 861,795,712 — a different one each run.

The game's own getter was never wrong. `BT_GetHeart` is `(float)GetHeart()`,
and called through the export it answered 88 while the HUD said 864,271,936.
A `float` comes back in `xmm0`; the engine reads `rax`, as if the export
returned an `int`. On Windows that works by accident, because the CLR's
conversion leaves the original `int` sitting in `rax`. wine-mono's
native-to-managed wrapper leaves a heap pointer there instead, and the HUD
prints it.

So each float-returning export gets a few bytes of machine code in front of it
that call the real one and then copy the truncated result into `eax` too. A
caller reading `xmm0` sees no change. The one subtlety is that mono compiles
these lazily: the first call through a slot replaces it with the compiled
wrapper, so the stub takes the slot back afterwards.

## What it looks like when it works

```
[exportshim] BT_GetScannedDevicesList: slot 0x180036080 0x39871b50 -> 0x39879500
             ... hooked 4/4 exports
[exportshim] float returns mirrored into eax: 12/12 -- BT_GetPower,BT_GetCadence,BT_GetHeart,...
[exportshim] BT_GetConnectedDevicesList -> 3 device(s) at 0x337e6310
```

`hooked 4/4 exports` is the line to look for. Anything less means the stub shape
was not recognised and the game will crash on its first poll. Short of `12/12`
on the second line, the missing ones show pointers on the HUD again.

## Files

| File | What it is |
|---|---|
| `ExportShim.cs` | Finds the slots, replaces them, marshals the arrays, fixes the float returns |
| `build.sh` | `mcs` → `build/MyWhooshShim.dll` |
| `install.sh` | Copies it into the prefix's wine-mono tree (`--restore` removes it) |

`CLAUDE.md` has the engineering notes: why the runtime cannot be talked into
doing this itself, and who owns the memory.
