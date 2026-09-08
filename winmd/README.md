# Stub winmd assemblies — letting the game's own connectivity init run

MyWhoosh's `WindowsConnectivity.dll` references two assemblies that do not exist
under Wine:

| Assembly | What the game wants from it |
|---|---|
| `Windows` (the Windows winmd, `Version=255.255.255.255`) | 33 WinRT types — `BluetoothLEAdvertisementWatcher`, the `Gatt*` family, `Radio`, `DataReader`/`DataWriter` |
| `System.Runtime.WindowsRuntime` | one member, `WindowsRuntimeSystemExtensions.GetAwaiter`, the `await` bridge from `IAsyncOperation` to `Task` |

Missing them, mono cannot lay out `BluetoothManager.BluetoothProgram` — it has
one field, `advertisment`, typed `BluetoothLEAdvertisementWatcher` — so
`BT_InitBluetoothManager` throws `TypeLoadException` and the process dies at
launch, connectivity init and all. This directory supplies both assemblies as
declaration-only stubs, and **with them the game starts and runs its own
connectivity init**, `WD_InitWahooDirconManager` included.

```sh
./build.sh
WINEPREFIX=~/Games/mywhoosh ./install.sh
```

They go in the prefix's wine-mono tree (`c:\windows\mono\mono-2.0\lib`), which
is where mono probes for a referenced assembly by simple name. **No game file is
touched, and none may be** — see below.

## Why stubs rather than a patch

`../patch/patch_windows_connectivity_dll.py` rewrites
`BluetoothProgram::IsBluetoothEnabled` to `ldc.i4.1; ret`. That patch is what
makes 6.1.2 start today, and the reason it works is not the one it was written
for: **MyWhoosh hashes `WindowsConnectivity.dll` and silently skips loading it
when a single byte differs.** The patch makes the game start by making the game
stop loading the DLL at all, which is also why nothing in the Dircon stack was
ever reached.

Measured on 6.1.2, same prefix, `WINEDEBUG=+loaddll`:

| `WindowsConnectivity.dll` | `mscoree` + the DLL load? |
|---|---|
| pristine | yes → crash in `BT_InitBluetoothManager` |
| pristine bytes, fresh mtime | yes → same crash |
| `advertisment` retyped to `object` (4 bytes) | **no** |
| one character of the DOS stub `'T'`→`'t'` (1 byte, pure padding) | **no** |

The DOS-stub run is the decisive one: those bytes mean nothing to any loader, so
the gate is on the file's content, not on anything the edit did. The game
reaches the same point either way — the branch where `mscoree` would load — and
simply goes on to the UI instead. There is no stored hash in the exe or the game
tree to update, and the DLL carries no Authenticode signature, so this is the
game's own check.

That rules out **every** approach that edits the DLL: retyping the field,
dropping it, early-returning `BT_InitBluetoothManager`. Satisfying the reference
from outside is what is left, and it is also the least invasive thing available.

## What the stubs do, and what they deliberately do not

`members.py` reads the exact set of types and members out of the game's own
metadata, so the stubs are checked against the game rather than guessed from
WinRT documentation:

```sh
./members.py <path to>/WindowsConnectivity.dll           # what is referenced
./members.py <path to>/WindowsConnectivity.dll --check   # ... and whether build/ covers it
```

`build.sh` runs the check automatically when it can find the DLL. The same 34
types and 60 members cover 5.7.2, 5.8.2 and 6.1.2 unchanged, so a MyWhoosh
update reaching further into WinRT would show up here as a failed check rather
than as a crash.

The bodies are inert — BLE needs WinRT, which Wine does not have, so nothing
here can do the real work. What matters is that **nothing throws**. These run
under mono's native-to-managed wrappers, where an escaping exception is a fatal
crash rather than a caught error, which is the exact failure the stubs exist to
avoid. So every member returns an empty or default value: no radios, no devices,
no services. `IsBluetoothEnabled` walks `Radio.GetRadiosAsync()`, finds no
Bluetooth radio and reports Bluetooth off, which is the truthful answer under
Wine. The assigned-number service UUIDs in `GattServiceUuids` are real, so a
caller comparing against them compares against the right constants.

`rename_assembly.py` exists for one build-time reason: `mcs` refuses to emit an
assembly named `System.Runtime.WindowsRuntime` (CS0281 — mscorlib grants that
name friend access and we cannot sign with Microsoft's key). It is a
compiler-side check only, so the stub is built one character longer and the name
is shortened in metadata afterwards. Nothing moves; only a string terminator
lands a byte earlier.

## Where this leaves the game

On a prefix with the stubs and a pristine DLL, startup survives with **no**
exception at all and the game keeps running — measured over 100 s, with
`svcctl_EnumServicesStatusExW` recurring, which is `GetBonjourService()` inside
the Dircon manager.

On the full stack — patched wine-mono (`../winemono`) plus `../fakesensor` — the
game's own init goes further still, into our Bonjour replacement:

```
[fakebonjour] created DNSSDEventManager -> 0x00000000
[fakebonjour] Advise: sink … (_IDNSSDEvents …), events start at vtable slot 7
[fakebonjour] created DNSSDService -> 0x00000000
```

That is the game process, not a probe, reaching the Dircon stack — blocker 3
closed. It then hits a new one; see "Blocker 4" in `../dircon/README.md`.
