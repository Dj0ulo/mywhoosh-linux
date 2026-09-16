# patch — the older way past the startup crash

`patch_windows_connectivity_dll.py` edits MyWhoosh's `WindowsConnectivity.dll`
so that `BluetoothProgram::IsBluetoothEnabled` always returns true. It finds the
method by parsing the .NET metadata rather than by a fixed file offset, so it
survives game updates, and it rewrites the body to `ldc.i4.1; ret`.

## Why it existed

Under Wine the game used to die at launch. This patch got it past that, and the
result was a MyWhoosh you could ride with sensors bridged from a phone — the
game's own Bluetooth was dead either way, so nothing was lost by editing it.

## Why nothing installs it any more

The Bluetooth support in this repository answers the game's *own* Bluetooth
calls, and for that the game has to actually run its connectivity code. It will
not: **MyWhoosh hashes `WindowsConnectivity.dll` and silently stops loading it
when a single byte differs** — including bytes that mean nothing, such as the
DOS stub. A patched game starts and looks healthy, but its connectivity DLL is
never loaded, so no sensor can ever arrive over Bluetooth.
[`../winmd/README.md`](../winmd/README.md) has the measurements.

So the Lutris installers leave the DLL alone and add the missing pieces to the
Wine prefix instead. Nothing in the install path runs this script.

## Using it anyway

It still works, if what you want is the old launch-only setup — the game
running under Wine with your sensors bridged from a phone through the MyWhoosh
Link app, and no Bluetooth from Linux at all.

```sh
python3 patch/patch_windows_connectivity_dll.py <path to>/WindowsConnectivity.dll
```

Patch a copy of the prefix, not one you also want Bluetooth in: the two
approaches are mutually exclusive, and that is the whole reason this is no
longer the default.

Tests live in [`tests/`](tests/README.md) and run against real DLLs from two
MyWhoosh releases.
