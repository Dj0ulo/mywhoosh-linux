# mywhoosh-linux — orientation

MyWhoosh is a Windows indoor-cycling game with no Linux build. This repository
runs it under Wine and, harder, connects real fitness sensors to it.

## The branches

| Branch | What it is |
|---|---|
| `main` | Installing and launching the game: the Lutris installer, and a patch to `WindowsConnectivity.dll` that stops it crashing at startup |
| `dev` | Sensors over the LAN: the game is told its trainer is a Wahoo Direct Connect device on the network, and a Linux bridge relays a real Bluetooth trainer to it |
| this branch | Sensors over Bluetooth: the game's *own* Bluetooth code, answered from BlueZ |

The `dev` stack works and is the shipping one. This branch does the same job
with far less disguise — no fake Bonjour service, no `.local` resolver preload,
no patched wine-mono, no single-connection workaround for heart-rate straps —
and as of 2026-09-11 it drives a real trainer and a real strap in the game.

## The directories here

| Directory | What it is |
|---|---|
| `bleshim/` | The Bluetooth stack: a .NET assembly the game loads instead of WinRT, plus a Linux helper speaking BlueZ |
| `exportshim/` | Replaces four game entry points wine-mono cannot marshal, in memory |
| `winmd/` | The declaration-only stubs the game needs to start at all — and the "Bluetooth off" state to fall back to |
| `tools/` | Small programs that read the game's own bytecode; every decision here comes from them |
| `patch/`, `lutris/` | From `main`: the startup patch and the installer |

Each of the first four has a `README.md` for orientation. `bleshim/` and
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

## Where to start reading

For the Bluetooth work: `bleshim/README.md`, then `bleshim/CLAUDE.md`. The
second one lists what is still open, including the two things this branch does
not yet ship for itself.
