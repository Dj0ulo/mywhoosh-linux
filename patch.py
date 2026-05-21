import os
import sys
import shutil
import zipfile
from pathlib import Path

from utils import download_file

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

    if new_file.exists():
        print(f"{new_file} already exists.")
        return

    if original_file.exists():
        os.rename(original_file, new_file)
    else:
        print(f"{original_file} does not exist.")


def patch_2(root_directory: Path, wine_prefix: Path):
    """
    Without this patch the following error occurs:

    Unhandled Exception:
    System.TypeLoadException:
    Could not load type of field 'BluetoothManager.BluetoothProgram:advertisment' (0) due to:
    Could not load file or assembly 'Windows, Version=255.255.255.255, Culture=neutral, PublicKeyToken=null' or one of its dependencies.
    at (wrapper native-to-managed) FunctionsManager.MyWhoosh.BT_InitBluetoothManager()
    """
    windows_dir = wine_prefix / "drive_c" / "windows"

    def extract_if_missing(zip_ref, name, dest_dir):
        """Extracts a single winmd file from the nuget zip if it doesn't exist."""
        dest_file = dest_dir / f"{name}.winmd"
        if not dest_file.exists():
            print(f"  Extracting {name}.winmd...")
            # NuGet internal path format
            zip_internal_path = f"ref/netstandard2.0/{name}.winmd"
            
            try:
                # Replicates 'unzip -j' by reading data and writing flat to destination
                with zip_ref.open(zip_internal_path) as source, open(dest_file, "wb") as target:
                    shutil.copyfileobj(source, target)
            except KeyError:
                print(f"Error: Could not find {zip_internal_path} inside the package.")
                sys.exit(1)


    def gac_install(src_path, asm_name, versions):
        """Installs a file into the Mono GAC directory under one or more versions."""
        mono_gac_dir = windows_dir / "mono" / "mono-2.0" / "lib" / "mono" / "gac"
        for ver in versions:
            # Mono GAC path format: {name}/{version}__/
            entry_dir = mono_gac_dir / asm_name / f"{ver}__"
            entry_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, entry_dir / f"{asm_name}.dll")

    print("==> MyWhoosh BLE fix: installing Windows WinRT metadata")

    # --- Step 1: Download the NuGet package if not cached ---
    nupkg_cache_path = Path("/tmp/Microsoft.Windows.SDK.Contracts.nupkg")
    if nupkg_cache_path.exists():
        print(f"{nupkg_cache_path.name} already exists.")
    else:
        download_file(
            "https://www.nuget.org/api/v2/package/Microsoft.Windows.SDK.Contracts/10.0.19041.2",
            nupkg_cache_path, 
        )

    # --- Step 2: Extract WinMD files ---
    cache_dir = Path("/tmp/mywhoosh-winmd-cache")
    cache_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(nupkg_cache_path, 'r') as zip_ref:
        # Windows.WinMD handles type forwarding (capitalized as Windows.WinMD in Zip)
        dest_windows_winmd = cache_dir / "Windows.winmd"
        if not dest_windows_winmd.exists():
            print("  Extracting Windows.WinMD...")
            try:
                with zip_ref.open("ref/netstandard2.0/Windows.WinMD") as source, open(dest_windows_winmd, "wb") as target:
                    shutil.copyfileobj(source, target)
            except KeyError:
                print("Error: Could not find Windows.WinMD in package.")
                sys.exit(1)

        # Extract remaining contract assemblies
        extract_if_missing(zip_ref, "Windows.Foundation.UniversalApiContract", cache_dir)
        extract_if_missing(zip_ref, "Windows.Foundation.FoundationContract", cache_dir)
        extract_if_missing(zip_ref, "Windows.Devices.DevicesLowLevelContract", cache_dir)

    # Print extracted file sizes (Replicates du -sh pipeline)
    sizes = []
    for f in cache_dir.glob("*.winmd"):
        size_mb = f.stat().st_size / (1024 * 1024)
        sizes.append(f"{f.name}: {size_mb:.1f}M")
    print(f"  Sizes: {'  '.join(sizes)}")

    # --- Step 3: Install Windows.WinMD facade into Mono GAC ---
    print("  Installing Windows facade into Mono GAC...")
    gac_install(cache_dir / "Windows.winmd", "Windows", ["255.255.255.255"])

    # # --- Step 4: Install contract assemblies into Mono GAC ---
    print("  Installing contract assemblies into Mono GAC...")
    gac_install(cache_dir / "Windows.Foundation.UniversalApiContract.winmd", 
                "Windows.Foundation.UniversalApiContract", ["1.0.0.0", "10.0.0.0"])
    
    gac_install(cache_dir / "Windows.Foundation.FoundationContract.winmd", 
                "Windows.Foundation.FoundationContract", ["1.0.0.0", "4.0.0.0"])
    
    gac_install(cache_dir / "Windows.Devices.DevicesLowLevelContract.winmd", 
                "Windows.Devices.DevicesLowLevelContract", ["1.0.0.0", "3.0.0.0"])

    # --- Step 5: Place WinMDs in WinMetadata directories ---
    print("  Copying to WinMetadata (system32 + syswow64)...")
    winmetadata_dirs = [
        windows_dir / "system32" / "winmetadata",
        windows_dir / "syswow64" / "winmetadata",
    ]
    files_to_copy = [
        "Windows.winmd",
        "Windows.Foundation.UniversalApiContract.winmd",
        "Windows.Foundation.FoundationContract.winmd",
        "Windows.Devices.DevicesLowLevelContract.winmd"
    ]
    
    for target_dir in winmetadata_dirs:
        target_dir.mkdir(parents=True, exist_ok=True)
        for fname in files_to_copy:
            shutil.copy2(cache_dir / fname, target_dir / fname)

    # --- Step 6: Place DLLs alongside the game's BT libraries ---
    print("  Copying contract DLLs next to game BT libraries and executable...")
    dll_dir = root_directory / "MyWhoosh" / "Content" / "Libraries" / "Win64"
    shutil.copy2(cache_dir / "Windows.winmd", dll_dir / "Windows.dll")
    shutil.copy2(cache_dir / "Windows.Foundation.UniversalApiContract.winmd", dll_dir / "Windows.Foundation.UniversalApiContract.dll")
    shutil.copy2(cache_dir / "Windows.Foundation.FoundationContract.winmd", dll_dir / "Windows.Foundation.FoundationContract.dll")
    shutil.copy2(cache_dir / "Windows.Devices.DevicesLowLevelContract.winmd", dll_dir / "Windows.Devices.DevicesLowLevelContract.dll")

    print("==> Done!")

def patch_windows_connectivity_dll(root_directory: Path):
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
        print("    Done.")
    elif bytes(data[CODE_OFF:CODE_OFF+2]) == PATCHED:
        print("    Already patched, skipping.")
    else:
        print(f"    WARNING: unexpected bytes at 0x{CODE_OFF:x}: {actual.hex()} — skipping patch.")

def patch_mywhoosh(root_directory: Path, wine_prefix: Path):
    rename_exe(root_directory)
    patch_2(root_directory, wine_prefix)
    patch_windows_connectivity_dll(root_directory)

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python patch.py <root_directory> <wine_prefix>")
    else:
        root_directory = Path(sys.argv[1])
        wine_prefix = Path(sys.argv[2])
        patch_mywhoosh(root_directory, wine_prefix)