import os
from pathlib import Path
import sys

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

    parent_dir = root_directory / "MyWhoosh" / "Binaries" / "Win64"
    original_file = parent_dir / "MyWhoosh.exe"
    new_file = parent_dir / "MyWhoosh-Win64-Shipping.exe"
    
    if original_file.exists():
        os.rename(original_file, new_file)
    else:
        print(f"{original_file} does not exist.")


def patch_2(root_directory: Path):
    """
    Without this patch the following error occurs:

    Unhandled Exception:
    System.TypeLoadException:
    Could not load type of field 'BluetoothManager.BluetoothProgram:advertisment' (0) due to:
    Could not load file or assembly 'Windows, Version=255.255.255.255, Culture=neutral, PublicKeyToken=null' or one of its dependencies.
    at (wrapper native-to-managed) FunctionsManager.MyWhoosh.BT_InitBluetoothManager()
    """

    pass

def patch_mywhoosh(root_directory: Path):
    rename_exe(root_directory)
    patch_2(root_directory)

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python patch.py <root_directory>")
    else:
        root_directory = Path(sys.argv[1])
        patch_mywhoosh(root_directory)