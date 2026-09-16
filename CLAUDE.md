# mywhoosh-linux — orientation

MyWhoosh is a Windows indoor-cycling game with no Linux build. This repository
runs it under Wine and, harder, connects real fitness sensors to it — the game's
*own* Bluetooth code, answered from BlueZ.

## The directories here

| Directory | What it is |
|---|---|
| `bleshim/` | The Bluetooth stack: a .NET assembly the game loads instead of WinRT, plus a Linux helper speaking BlueZ |
| `exportshim/` | Replaces four game entry points wine-mono cannot marshal, in memory |
| `winmd/` | The declaration-only stubs the game needs to start at all — and the "Bluetooth off" state to fall back to |
| `tools/` | Small programs that read the game's own bytecode; every decision here comes from them |
| `lutris/` | The Lutris installers, and the script that runs the helper beside the game |
| `dist.sh` | Builds the release archive the installers download, and publishes it |
| `patch/` | The older launch-only patch, no longer installed by anything |

Every one of these has a `README.md` for orientation. `bleshim/` and
`exportshim/` also have a `CLAUDE.md` with the engineering detail.

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

**The game must not find a Bonjour service in the prefix.** It reaches Apple
Bonjour's COM objects — and the `ComAwareEventInfo` that wine-mono does not
implement — only when the SCM reports a service named exactly `"Bonjour Service"`
in state `Running`. With no such service, neither is needed and the Bluetooth
path is all that runs. `bleshim/CLAUDE.md` has the IL.

## Where to start reading

For the Bluetooth work: `bleshim/README.md`, then `bleshim/CLAUDE.md`. The
second one also lists what is still open.
