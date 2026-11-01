# Contributing to mywhoosh-linux

MyWhoosh is a Windows indoor-cycling game with no Linux build. This repository
runs it under Wine and, harder, connects real fitness sensors to it — the game's
*own* Bluetooth code, answered from BlueZ.

## The directories here

| Directory | What it is |
|---|---|
| `bleshim/` | The Bluetooth stack: a .NET assembly the game loads instead of WinRT, plus a Linux helper speaking BlueZ |
| `exportshim/` | Replaces game entry points wine-mono marshals wrongly, in memory |
| `bonjour/` | Real COM event subscriptions and runtime-local framework generation, with callback tests |
| `winmd/` | The declaration-only stubs the game needs to start at all — and the "Bluetooth off" state to fall back to |
| `tools/` | Small programs that read the game's own bytecode; every decision here comes from them |
| `lutris/` | The Lutris installers, the script that runs the helper beside the game, and the game updater |
| `dist.sh` | Builds the release archive the installers download, and publishes it |
| `patch/` | The older launch-only patch, no longer installed by anything |

Every one of these has a `README.md` for orientation. `bleshim/` and
`exportshim/` also have an `ENGINEERING.md` with the engineering detail.

## Rules that apply everywhere

**Never modify a file in the game's own directory.** MyWhoosh hashes
`WindowsConnectivity.dll` and silently stops loading it if one byte
differs — including bytes that mean nothing, like the DOS stub. A "successful"
patch usually means the game is no longer loading its connectivity DLL at all.
`winmd/README.md` has the measurements. Everything here works by adding files to
the Wine prefix or by writing our own process memory.

**Mono is not the CLR.** The game runs on wine-mono inside the prefix, which has
no WinRT projection at all, splits BCL types across different assemblies than
the host Mono, refuses some marshalling shapes the CLR supports, and treats a
Unix path as a Windows one. Code that is correct on the host is routinely wrong
in the prefix. Test in the prefix.

**Nothing may throw across a native boundary.** Much of this code runs in mono's
native-to-managed wrappers, where an escaping exception is a process crash, not
an error. Entry points catch, log, and return something harmless.

**Read the IL before deciding.** `tools/ildump.sh` answers what the game does;
`winmd/members.py` answers what it references. Both have overturned reasonable
assumptions here.

**Keep Bonjour working, not hidden.** A running `"Bonjour Service"` enables
the game's COM event path. Mono's original `ComAwareEventInfo` throws there;
`bonjour/` supplies an isolated, guarded framework copy and real connection-point
subscriptions. Never disable Bonjour to make a test pass, patch a game DLL, or
replace a runner's installed framework. Tests must exercise real callbacks and
assert that the game's Bonjour gate is enabled. See `bonjour/README.md` for the
supported runtime, tests, installation and current limits.

## Where to start reading

For the Bluetooth work: `bleshim/README.md`, then `bleshim/ENGINEERING.md`. The
second one also lists what is still open.
