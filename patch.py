#!/usr/bin/python3

import os
import sys
from pathlib import Path


def rename_exe(root_directory: Path):
    """
    Renames the executable file for MyWhoosh from 'MyWhoosh.exe' to 'MyWhoosh-Win64-Shipping.exe'.
    
    Without this patch a popup appears with:

    **Error**\\
    Couldn't start:\\
    "{root_directory}/MyWhoosh/Binaries/Win64/MyWhoosh-Win64-Shipping.exe"\\
    MyWhoosh\\
    CreateProcess() returned 2.
    """
    print("Renaming MyWhoosh executable...")
    parent_dir = root_directory / "MyWhoosh" / "Binaries" / "Win64"
    original_file = parent_dir / "MyWhoosh.exe"
    new_file = parent_dir / "MyWhoosh-Win64-Shipping.exe"

    if new_file.exists():
        print(f"{new_file} already exists.")
        return

    if original_file.exists():
        os.rename(original_file, new_file)
    else:
        print(f"{original_file} does not exist.")


def patch_windows_connectivity_dll(root_directory: Path):
    """
    Patch WindowsConnectivity.dll to bypass Bluetooth state check.

    Without this patch a popup appears with:

    System.MissingMethodException: Method not found: Windows.Foundation.IAsyncOperation`1<System.Collections.Generic.IReadOnlyList`1<Windows.Devices.Radios.Radio>> Windows.Devices.Radios.Radio.GetRadiosAsync()
    at System.Runtime.CompilerServices.AsyncTaskMethodBuilder`1[TResult].Start[TStateMachine] (TStateMachine& stateMachine)
    at BluetoothManager.BluetoothProgram.CheckRadioState ()
    at BluetoothManager.BluetoothProgram.IsBluetoothEnabled ()
    at FunctionsManager.MyWhoosh.BT_GetModuleState ()
    at (wrapper native-to-managed) FunctionsManager.MyWhoosh.BT_GetModuleState()
    """
    print("Patching WindowsConnectivity.dll to bypass Bluetooth state check...")
    dll_path = root_directory / "MyWhoosh" / "Content" / "Libraries" / "Win64" / "WindowsConnectivity.dll"
    with open(dll_path, 'rb') as f:
        data = bytearray(f.read())

    CODE_OFF  = 0xc50
    EXPECTED  = bytes([0x02, 0x28, 0x67]) # ldarg.0; call (first 3 bytes of original body)
    PATCHED   = bytes([0x17, 0x2a])       # ldc.i4.1; ret

    actual = bytes(data[CODE_OFF:CODE_OFF+3])
    if actual == EXPECTED:
        data[CODE_OFF]   = 0x17
        data[CODE_OFF+1] = 0x2a
        with open(dll_path, 'wb') as f:
            f.write(data)
    elif bytes(data[CODE_OFF:CODE_OFF+2]) == PATCHED:
        print("Already patched, skipping.")
    else:
        print(f"WARNING: unexpected bytes at 0x{CODE_OFF:x}: {actual.hex()} — skipping patch.")


def patch_mywhoosh(root_directory: Path):
    print("Patching MyWhoosh to make it work with Wine...")
    rename_exe(root_directory)
    patch_windows_connectivity_dll(root_directory)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python patch.py <root_directory>")
        exit(1)
    else:
        root_directory = Path(sys.argv[1])
        patch_mywhoosh(root_directory)