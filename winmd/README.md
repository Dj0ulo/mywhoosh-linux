# winmd — the two assemblies the game cannot start without

MyWhoosh's `WindowsConnectivity.dll` references two .NET assemblies that exist
on Windows and not under Wine:

| Assembly | What the game wants from it |
|---|---|
| `Windows` (the WinRT metadata, `Version=255.255.255.255`) | 34 Bluetooth types — the advertisement watcher, the GATT family, `Radio`, `DataReader`/`DataWriter` |
| `System.Runtime.WindowsRuntime` | one member, `WindowsRuntimeSystemExtensions.GetAwaiter`, which is how `await` works on a WinRT call |

Without them, mono cannot even lay out the game's `BluetoothProgram` class — one
of its fields is typed `BluetoothLEAdvertisementWatcher` — so the game throws
`TypeLoadException` and dies at launch, before any UI.

This directory builds both as **declaration-only stubs**: every type and member
the game references, every one of them answering "no radio, no devices, no
services". With them installed the game starts and runs normally, with Bluetooth
simply reporting nothing.

```sh
./build.sh
WINEPREFIX=<prefix> ./install.sh
```

They go into the prefix's own wine-mono directory,
`c:\windows\mono\mono-2.0\lib`, because that is where mono looks for a
referenced assembly by simple name. **No game file is touched** — see below for
why that is non-negotiable.

`../bleshim/` builds assemblies with the same names and the same surface, whose
answers are real Bluetooth from BlueZ. Install one or the other, never both.
These stubs remain the "off switch": `../bleshim/install.sh --restore` puts them
back.

## Why stubs and not a patch to the game

The obvious fix is to edit `WindowsConnectivity.dll` — retype the offending
field, or make the Bluetooth check return early. It does appear to work, and it
works for a reason nobody wants:

**MyWhoosh hashes `WindowsConnectivity.dll` and silently skips loading it when a
single byte differs.** The game then continues to its menus as if the DLL had
never existed — which is why a patched game starts, and also why nothing that
DLL does is ever reached.

Measured on 6.1.2, same prefix, `WINEDEBUG=+loaddll`:

| `WindowsConnectivity.dll` | Does the game load it? |
|---|---|
| pristine | yes → crash in `BT_InitBluetoothManager` |
| pristine bytes, fresh mtime | yes → same crash |
| `advertisment` field retyped to `object` | **no** |
| one character of the DOS stub `'T'`→`'t'` — pure padding | **no** |

The DOS-stub run is the decisive one: those bytes mean nothing to any loader, so
the check is on the file's content and not on anything the edit did. There is no
stored hash in the game tree to update, and the DLL carries no Authenticode
signature, so this is the game's own check.

That rules out every approach that edits the file. Satisfying the missing
reference from outside is what is left — and it is also the least invasive thing
available.

## Checking the stubs against the game

`members.py` reads the exact set of types and members out of the game's own
metadata, so the surface is checked against the game rather than guessed from
WinRT documentation:

```sh
./members.py <path to>/WindowsConnectivity.dll           # what is referenced
./members.py <path to>/WindowsConnectivity.dll --check   # ... and whether build/ has it
```

`build.sh` runs the check automatically when it can find the DLL. The same 34
types and 60 members cover MyWhoosh 5.7.2, 5.8.2 and 6.1.2 unchanged, so a game
update reaching further into WinRT shows up here as a failed check rather than
as a crash mid-ride.

**Known gap:** the check compares member *names* only. A member with the right
name and the wrong signature passes here and throws `MissingMethodException` at
run time. That has happened once and cost an evening; `../bleshim/CLAUDE.md`
records it.

## Files

| File | What it is |
|---|---|
| `Windows.cs`, `SystemRuntimeWindowsRuntime.cs` | The stubs |
| `members.py` | What the game references, and whether a build covers it |
| `rename_assembly.py` | Renames an assembly in metadata after compilation |
| `build.sh` / `install.sh` | Build, and install into a prefix |

`rename_assembly.py` exists for one build-time reason: `mcs` refuses to emit an
assembly named `System.Runtime.WindowsRuntime` (CS0281 — mscorlib grants that
name friend access and we cannot sign with Microsoft's key). It is a
compiler-side check only, so the stub is built one character longer and the name
shortened in metadata afterwards. Nothing moves; only a string terminator lands
a byte earlier.

## What the stubs must never do

They run under mono's native-to-managed wrappers, where an escaping exception is
a fatal crash rather than a caught error. **Nothing may throw.** Every member
returns an empty or default value. `IsBluetoothEnabled` walks
`Radio.GetRadiosAsync()`, finds no Bluetooth radio, and reports Bluetooth
off — which is the truthful answer when nothing is behind them. The assigned
service UUIDs in `GattServiceUuids` are the real constants, so a caller
comparing against them compares against the right thing.

On a prefix with these stubs and a pristine DLL, startup survives with no
exception at all and the game keeps running.
