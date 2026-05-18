#!/usr/bin/python3

import sys


def patch_windows_connectivity_dll(dll_path: str):
    """
    Patch WindowsConnectivity.dll to bypass Bluetooth state check.

    Without this patch MyWhoosh would crash launching.
    """
    print("Patching WindowsConnectivity.dll to bypass Bluetooth state check...")
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


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python patch_windows_connectivity_dll.py <WindowsConnectivity.dll path>")
        exit(1)
    else:
        patch_windows_connectivity_dll(sys.argv[1])