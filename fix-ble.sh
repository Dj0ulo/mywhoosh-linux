#!/usr/bin/env bash
# Fix for BLE (Bluetooth Low Energy) TypeLoadException in MyWhoosh under Wine
#
# Error: Could not load type of field 'BluetoothManager.BluetoothProgram:advertisment'
#        due to: Could not load file or assembly 'Windows, Version=255.255.255.255'
#
# Root cause: Wine's WinMetadata directory is missing Windows.Devices.Bluetooth types.
# The game's WindowsConnectivity10.dll references assembly 'Windows, Version=255.255.255.255'
# (the union Windows WinRT metadata), which Wine does not provide.
#
# Fix: Download Windows.winmd from Microsoft.Windows.SDK.Contracts NuGet and install it
# in three locations so Wine's Mono runtime can resolve the 'Windows' assembly reference:
#   1. Mono GAC (primary — Mono checks this first for named assemblies)
#   2. WinMetadata directories (for WinRT namespace resolution)
#   3. Alongside the game DLLs (fallback probing path)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOTTLE="${SCRIPT_DIR}/mywhoosh-bottle/MyWhoosh"
WINMETADATA="${BOTTLE}/drive_c/windows/system32/winmetadata"
WINMETADATA_WOW="${BOTTLE}/drive_c/windows/syswow64/winmetadata"
MONO_GAC="${BOTTLE}/drive_c/windows/mono/mono-2.0/lib/mono/gac"
BT_LIB_DIR="${SCRIPT_DIR}/MyWhoosh/MyWhoosh/Content/Libraries/Win64"
BINARIES_DIR="${SCRIPT_DIR}/MyWhoosh/MyWhoosh/Binaries/Win64"

NUGET_URL="https://www.nuget.org/api/v2/package/Microsoft.Windows.SDK.Contracts/10.0.19041.2"
CACHE_FILE="/tmp/mywhoosh-Windows.WinMD"

echo "==> MyWhoosh BLE fix: installing Windows.winmd"

# --- Step 1: Download the NuGet package if not cached ---
if [ ! -f "${CACHE_FILE}" ]; then
    NUPKG="/tmp/mywhoosh-sdk-contracts.nupkg"
    echo "  Downloading Microsoft.Windows.SDK.Contracts..."
    curl -L -o "${NUPKG}" "${NUGET_URL}" --max-time 120
    echo "  Extracting Windows.WinMD..."
    unzip -j -o "${NUPKG}" "ref/netstandard2.0/Windows.WinMD" -d /tmp/
    mv /tmp/Windows.WinMD "${CACHE_FILE}"
    rm -f "${NUPKG}"
fi

echo "  Windows.WinMD size: $(du -sh "${CACHE_FILE}" | cut -f1)"

# --- Step 2: Install into Mono GAC (primary resolution path) ---
# Mono looks here first. Assembly 'Windows, Version=255.255.255.255, PublicKeyToken=null'
# maps to gac/Windows/255.255.255.255__/Windows.dll
GAC_ENTRY="${MONO_GAC}/Windows/255.255.255.255__"
echo "  Installing into Mono GAC: ${GAC_ENTRY}"
mkdir -p "${GAC_ENTRY}"
cp "${CACHE_FILE}" "${GAC_ENTRY}/Windows.dll"

# --- Step 3: Place Windows.winmd in WinMetadata directories ---
# Used by WinRT namespace resolution (RoResolveNamespace)
echo "  Copying to WinMetadata (system32)..."
cp "${CACHE_FILE}" "${WINMETADATA}/Windows.winmd"

echo "  Copying to WinMetadata (syswow64)..."
cp "${CACHE_FILE}" "${WINMETADATA_WOW}/Windows.winmd"

# --- Step 4: Place Windows.dll alongside the game's BT libraries ---
# Mono also probes the directory of the referencing assembly
echo "  Copying as Windows.dll next to WindowsConnectivity10.dll..."
cp "${CACHE_FILE}" "${BT_LIB_DIR}/Windows.dll"

echo "  Copying as Windows.dll next to game executable..."
cp "${CACHE_FILE}" "${BINARIES_DIR}/Windows.dll"

echo ""
echo "==> Fix applied successfully!"
echo ""
echo "    Run the game with:"
echo "    WINEPREFIX=${BOTTLE} wine ${SCRIPT_DIR}/MyWhoosh/MyWhoosh.exe"
