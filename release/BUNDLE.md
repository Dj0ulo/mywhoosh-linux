# MyWhoosh connectivity stack

Everything in here is prebuilt. Installing it needs Wine (Lutris' own runner
will do) and nothing else — no compiler, no mono-devel, no p7zip, and no part
of Apple's Bonjour.

```sh
./install.sh /path/to/wine/prefix      # the prefix MyWhoosh is installed in
./install.sh --verify /path/to/prefix  # check what landed
./install.sh --restore /path/to/prefix # undo it
```

Then launch the game with

```sh
LD_PRELOAD=/path/to/prefix/dotlocal_shim.so
```

which is what the Lutris installer sets for you.

## What it does

MyWhoosh's own connectivity DLL works under Wine once four things are supplied
from outside the game tree:

| | |
|---|---|
| `winemono/` | rewrites `ComAwareEventInfo` in the prefix's copy of wine-mono, where it is a throw-only stub, because that is how the game wires its Bonjour event handlers |
| `winmd/` | stub `Windows` / `System.Runtime.WindowsRuntime` assemblies, which Wine does not have and the game's DLL cannot load without |
| `exportshim/` | serves the four device-list exports whose byref array wine-mono's marshaller refuses |
| `fakesensor/` | answers Bonjour discovery in-process, satisfies the `Bonjour Service` gate with a stub service, and resolves the `.local` name Wine cannot |

**No game file is modified.** `WindowsConnectivity.dll` in particular must stay
byte-identical: MyWhoosh hashes it and silently stops loading it if anything
changed — which is what the old `patch_windows_connectivity_dll.py` did, and why
that patch and this stack cannot be used together.

## Connecting a real trainer

`fakesensor/blebridge.py` connects to a Bluetooth LE trainer through BlueZ and
serves it to the game over Wahoo Direct Connect — the companion app's job, done
from Linux:

```sh
fakesensor/blebridge.py --list
fakesensor/blebridge.py --mac AA:BB:CC:DD:EE:FF
```

Start it before the game and there is nothing to configure: it writes your
trainer's name, serial and port into the prefix, and the DLL reads them when the
game loads it. Without a bridge the prefix serves one hard-coded sensor instead
(`FAKESENSOR_NAME` / `FAKESENSOR_POWER` / `FAKESENSOR_BPM`), which is enough to
see the whole path work.

Source and the full write-up: https://github.com/Dj0ulo/mywhoosh-linux
