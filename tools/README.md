# tools

## ILDump

Minimal IL disassembler for `WindowsConnectivity.dll`, used to find out what the
game actually does rather than guessing from names.

```sh
./ildump.sh WahooDirconManager.WahooProgram WFTNP_Init
./ildump.sh FunctionsManager.MyWhoosh WD_GetDirconServiceAvailability
./ildump.sh BluetoothManager.BluetoothProgram          # every method of a type
```

It runs under wine-mono inside the game prefix on purpose: the host Mono lacks
`System.ServiceProcess`, which several `WahooProgram` methods reference, and
resolving a method's local-variable signature fails before any IL is read.

Reflection is the ground truth for signatures — the hand-written API reference in
`wine-ble/test-ble/WindowsConnectivity.md` has several wrong.

## SigDump / AllSigs

What the game's metadata says about marshalling, which is what blocker 4 turned
on: exact parameter attributes, `[MarshalAs]` (mono synthesises it from
`FieldMarshal`, so reflection sees it) and native struct layout.

```sh
mcs -platform:x64 -out:SigDump.exe SigDump.cs
mcs -platform:x64 -out:AllSigs.exe AllSigs.cs
WINEPREFIX=<prefix> wine SigDump.exe WindowsConnectivity.dll   # the *DevicesList four + their struct
WINEPREFIX=<prefix> wine AllSigs.exe WindowsConnectivity.dll   # all 98 exports, odd params flagged
```

`Types.cs` is the same idea, smaller: it just lists every type in an assembly,
which is how you find the class to point `ildump.sh` at.

Run them under wine-mono, not the host Mono: `Marshal.SizeOf` has to be the
runtime the game will actually use (it reports 64 bytes for
`DeviceInformationStruct`), and the host lacks `System.ServiceProcess`.
`AllSigs` is what established that only four exports take a byref array.
