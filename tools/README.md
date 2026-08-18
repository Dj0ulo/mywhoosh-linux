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
