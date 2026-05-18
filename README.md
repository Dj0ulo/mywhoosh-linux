# MyWhoosh on Linux via Wine

[MyWhoosh](https://www.mywhoosh.com/) is a free indoor cycling / virtual training app (similar to Zwift) that is officially only available on Windows, iOS, Android, and Apple TV. This repo contains notes and scripts to run it on Linux using Wine.

---

## What is MyWhoosh?

MyWhoosh is a free-to-use virtual cycling and running platform. It connects to ANT+ and Bluetooth Low Energy (BLE) sensors — smart trainers, heart rate monitors, power meters — so you can ride virtual routes while your real-world effort is captured. It hosts structured workouts and races, and is used by many cyclists as a Zwift alternative.

---

## Setup

### 1. Get the installer

Use [https://store.rg-adguard.net/](https://store.rg-adguard.net/) to download the `.msix` / `.appx` package directly from the Microsoft Store, then unpack it:

```bash
unzip -d MyWhoosh file.msix
```

### 2. Rename the executable

```bash
mv MyWhoosh/Binaries/Win64/MyWhoosh.exe MyWhoosh/Binaries/Win64/MyWhoosh-Win64-Shipping.exe
```

### 3. Install ICU

Download the 64-bit `icu.dll` from [dll-files.com](https://www.dll-files.com/icu.dll.html) and place it in `MyWhoosh/Binaries/Win64/`.

### 4. Install dependencies

```bash
sudo apt-get install winbind winetricks

WINEPREFIX=~/Documents/mywhoosh-wine/mywhoosh-bottle/MyWhoosh winetricks \
  dotnet461 dxvk vcrun2022 \
  atmlib corefonts gdiplus msxml3 msxml6 fontsmooth-rgb gecko
```

### 5. Fix BLE (Bluetooth Low Energy) — see below

```bash
./fix-ble.sh
```

### 6. Run

```bash
WINEPREFIX=~/Documents/mywhoosh-wine/mywhoosh-bottle/MyWhoosh wine MyWhoosh/MyWhoosh.exe
```

---

## BLE Fix (`fix-ble.sh`)

### The problem

When launching the game you may see one of three errors:

**Error 1** (assembly not found):
```
System.TypeLoadException: Could not load type of field
  'BluetoothManager.BluetoothProgram:advertisment' (0) due to:
  Could not load file or assembly
  'Windows, Version=255.255.255.255, Culture=neutral, PublicKeyToken=null'
```

**Error 2** (type not found inside the assembly):
```
System.TypeLoadException: Could not load type of field
  'BluetoothManager.BluetoothProgram:advertisment' (0) due to:
  Could not resolve type with token 01000090 from typeref
  (expected class 'Windows.Devices.Bluetooth.Advertisement.BluetoothLEAdvertisementWatcher'
  in assembly 'Windows, Version=255.255.255.255, ...')
```

**Error 3** (WinRT COM activation fails at runtime):
```
System.MissingMethodException: Method not found:
  Windows.Foundation.IAsyncOperation<IReadOnlyList<Radio>> Radio.GetRadiosAsync()
  at BluetoothManager.BluetoothProgram.CheckRadioState()
  at BluetoothManager.BluetoothProgram.IsBluetoothEnabled()
```

**Why they happen:**

MyWhoosh uses `WindowsConnectivity10.dll`, a .NET assembly that accesses BLE sensors via the Windows Runtime (WinRT) API — specifically `Windows.Devices.Bluetooth` and `Windows.Devices.Radios`. In .NET, all WinRT types are accessed through the assembly `Windows, Version=255.255.255.255` (`Windows.winmd`). Wine ships with only generic WinRT stubs and does not implement the Bluetooth or Radio COM servers.

The `Windows.WinMD` in Microsoft's NuGet package is a **type-forwarding facade**, not a monolithic union assembly. Its `ExportedType` table redirects every WinRT type to a *contract assembly* (e.g. `Windows.Foundation.UniversalApiContract`). Mono follows these forwarders — but those contracts are never installed, producing Errors 1 and 2.

Even after the contracts are installed (resolving type metadata), **Wine has no COM server** for `Windows.Devices.Radios`. When the game's `IsBluetoothEnabled()` calls `Radio.GetRadiosAsync()` via Mono's WinRT/COM bridge, `RoGetActivationFactory` fails and Mono throws Error 3.

### The fix

`fix-ble.sh` applies two independent fixes:

**Part A — Install WinRT type metadata** (fixes Errors 1 & 2):

Downloads `Microsoft.Windows.SDK.Contracts` from NuGet and installs four WinMD files so the `Windows` assembly ref and all type-forwarder targets resolve:

| Assembly | What it provides |
|---|---|
| `Windows` (`Windows.WinMD`) | Resolves `Windows, v255.255.255.255`; forwards all types to contracts below |
| `Windows.Foundation.UniversalApiContract` | Actual BLE + Radio type definitions (5.6 MB) |
| `Windows.Foundation.FoundationContract` | Dependency of `UniversalApiContract` |
| `Windows.Devices.DevicesLowLevelContract` | Dependency referenced by the `Windows` facade |

Each file is installed in:
- `Mono GAC/{Name}/{Version}__/` — at both the version the facade expects (1.0.0.0) and the file's declared version
- `system32/winmetadata/` and `syswow64/winmetadata/` — for WinRT namespace resolution
- `Content/Libraries/Win64/` and `Binaries/Win64/` — Mono's assembly-directory probing paths

**Part B — CIL patch on `WindowsConnectivity10.dll`** (fixes Error 3):

Wine cannot implement `Windows.Devices.Radios` without a full WinRT COM server. Instead, the script patches `IsBluetoothEnabled()` in the DLL directly: replaces its CIL body with two bytes — `ldc.i4.1` + `ret` — so it returns `true` unconditionally. The method's fat-format header and remaining 98 bytes of original code are left in place (unreachable after the early `ret`).

This is safe: the game only queries BLE radio state to guard its sensor-connection path; telling it "Bluetooth is on" causes it to proceed normally.

To re-apply after rebuilding the bottle or updating the game:

```bash
./fix-ble.sh
```

The script caches the NuGet package at `/tmp/mywhoosh-sdk-contracts.nupkg` and the extracted WinMDs at `/tmp/mywhoosh-winmd-cache/` so subsequent runs are instant.
