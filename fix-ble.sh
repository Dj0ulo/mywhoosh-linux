#!/usr/bin/env bash
# Fix for BLE (Bluetooth Low Energy) errors in MyWhoosh under Wine
#
# Error 1 (fixed): Could not load file or assembly 'Windows, Version=255.255.255.255'
# Error 2 (fixed): Could not resolve type 'BluetoothLEAdvertisementWatcher' from typeref
#                  in assembly 'Windows, Version=255.255.255.255'
# Error 3 (fixed): MissingMethodException: Method not found:
#                  Windows.Foundation.IAsyncOperation<...> Radio.GetRadiosAsync()
#
# Root cause of errors 1 & 2:
#   The game's WindowsConnectivity10.dll references 'Windows, Version=255.255.255.255'
#   (the union Windows WinRT metadata), which Wine does not provide.
#
#   The 'Windows.WinMD' shipped in Microsoft.Windows.SDK.Contracts is a *type-forwarding
#   facade* — it does not define WinRT types directly. Instead, its ExportedType table
#   redirects every type (including BluetoothLEAdvertisementWatcher) to a contract assembly
#   such as 'Windows.Foundation.UniversalApiContract v1.0.0.0'.  When Mono follows that
#   forwarder it looks in the GAC for Windows.Foundation.UniversalApiContract — but that
#   assembly is never installed, so the type still can't be found.
#
# Root cause of error 3:
#   Even with all WinMD types resolved, Wine does not implement the
#   Windows.Devices.Radios COM server.  When the game's BluetoothProgram.IsBluetoothEnabled()
#   calls Radio.GetRadiosAsync() via Mono's WinRT/COM bridge, RoGetActivationFactory()
#   returns an error and Mono converts it to MissingMethodException.
#
# Fix:
#   1. Install Windows.WinMD as Windows.dll so the 'Windows' assembly ref resolves.
#   2. Extract the contract WinMDs that Windows.WinMD forwards types to and install
#      them in the Mono GAC.  The facade references the contracts at v1.0.0.0; the
#      files in the NuGet package declare higher versions (10.0, 4.0, 3.0).  Mono
#      uses directory-based GAC lookup and does not re-validate the file's internal
#      version, so we install each contract at *both* the version the facade expects
#      (1.0.0.0) and the version the file declares, ensuring forward and backward
#      resolution both work.
#   3. Place every contract DLL alongside the game's BT libraries as a fallback.
#   4. Patch WindowsConnectivity10.dll: replace the CIL body of IsBluetoothEnabled()
#      with two instructions — ldc.i4.1 (push 1/true) + ret — so it returns true
#      unconditionally, bypassing the unimplementable WinRT radio check.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOTTLE="${SCRIPT_DIR}/mywhoosh-bottle/MyWhoosh"
WINMETADATA="${BOTTLE}/drive_c/windows/system32/winmetadata"
WINMETADATA_WOW="${BOTTLE}/drive_c/windows/syswow64/winmetadata"
MONO_GAC="${BOTTLE}/drive_c/windows/mono/mono-2.0/lib/mono/gac"
BT_LIB_DIR="${SCRIPT_DIR}/MyWhoosh/MyWhoosh/Content/Libraries/Win64"
BINARIES_DIR="${SCRIPT_DIR}/MyWhoosh/MyWhoosh/Binaries/Win64"

NUGET_URL="https://www.nuget.org/api/v2/package/Microsoft.Windows.SDK.Contracts/10.0.19041.2"
NUPKG_CACHE="/tmp/mywhoosh-sdk-contracts.nupkg"
CACHE_DIR="/tmp/mywhoosh-winmd-cache"

echo "==> MyWhoosh BLE fix: installing Windows WinRT metadata"

# --- Step 1: Download the NuGet package if not cached ---
if [ ! -f "${NUPKG_CACHE}" ]; then
    echo "  Downloading Microsoft.Windows.SDK.Contracts..."
    curl -L -o "${NUPKG_CACHE}" "${NUGET_URL}" --max-time 120
fi

# --- Step 2: Extract WinMD files ---
mkdir -p "${CACHE_DIR}"

extract_if_missing() {
    local name="$1"      # e.g. Windows.Foundation.UniversalApiContract
    local dest="${CACHE_DIR}/${name}.winmd"
    if [ ! -f "${dest}" ]; then
        echo "  Extracting ${name}.winmd..."
        unzip -j -o "${NUPKG_CACHE}" \
            "ref/netstandard2.0/${name}.winmd" \
            -d "${CACHE_DIR}/"
    fi
}

# Windows.WinMD — the type-forwarding facade that resolves 'Windows, v255.255.255.255'
# Note: capitalized as 'Windows.WinMD' inside the NuGet zip.
if [ ! -f "${CACHE_DIR}/Windows.winmd" ]; then
    echo "  Extracting Windows.WinMD..."
    unzip -j -o "${NUPKG_CACHE}" "ref/netstandard2.0/Windows.WinMD" -d "${CACHE_DIR}/"
    mv "${CACHE_DIR}/Windows.WinMD" "${CACHE_DIR}/Windows.winmd"
fi

# Contract assemblies that Windows.WinMD forwards types into.
# UniversalApiContract contains the full Bluetooth LE stack.
# FoundationContract and DevicesLowLevelContract are its dependencies.
extract_if_missing "Windows.Foundation.UniversalApiContract"
extract_if_missing "Windows.Foundation.FoundationContract"
extract_if_missing "Windows.Devices.DevicesLowLevelContract"

echo "  Sizes: $(du -sh ${CACHE_DIR}/*.winmd | awk '{print $2": "$1}' | tr '\n' '  ')"

# Helper: install a WinMD file into the Mono GAC under one or more version directories.
# Usage: gac_install <winmd_path> <assembly_name> <version> [<version2> ...]
# Mono GAC path format: {name}/{version}__{pubkeytoken}/ — empty token = just two underscores.
gac_install() {
    local src="$1"; shift
    local asmname="$1"; shift
    for ver in "$@"; do
        local entry="${MONO_GAC}/${asmname}/${ver}__"
        mkdir -p "${entry}"
        cp "${src}" "${entry}/${asmname}.dll"
    done
}

# --- Step 3: Install Windows.WinMD facade into Mono GAC ---
# Resolves: assembly ref 'Windows, Version=255.255.255.255, PublicKeyToken=null'
echo "  Installing Windows facade into Mono GAC..."
gac_install "${CACHE_DIR}/Windows.winmd" "Windows" "255.255.255.255"

# --- Step 4: Install contract assemblies into Mono GAC ---
# Windows.WinMD's ExportedType table references all three contracts at v1.0.0.0.
# The files themselves declare higher versions, so we install at both.
echo "  Installing contract assemblies into Mono GAC..."
gac_install "${CACHE_DIR}/Windows.Foundation.UniversalApiContract.winmd" \
    "Windows.Foundation.UniversalApiContract" "1.0.0.0" "10.0.0.0"

gac_install "${CACHE_DIR}/Windows.Foundation.FoundationContract.winmd" \
    "Windows.Foundation.FoundationContract" "1.0.0.0" "4.0.0.0"

gac_install "${CACHE_DIR}/Windows.Devices.DevicesLowLevelContract.winmd" \
    "Windows.Devices.DevicesLowLevelContract" "1.0.0.0" "3.0.0.0"

# --- Step 5: Place WinMDs in WinMetadata directories ---
# Used by WinRT namespace resolution (RoResolveNamespace)
echo "  Copying to WinMetadata (system32 + syswow64)..."
for dir in "${WINMETADATA}" "${WINMETADATA_WOW}"; do
    cp "${CACHE_DIR}/Windows.winmd"                              "${dir}/Windows.winmd"
    cp "${CACHE_DIR}/Windows.Foundation.UniversalApiContract.winmd" \
                                                                 "${dir}/Windows.Foundation.UniversalApiContract.winmd"
    cp "${CACHE_DIR}/Windows.Foundation.FoundationContract.winmd" \
                                                                 "${dir}/Windows.Foundation.FoundationContract.winmd"
    cp "${CACHE_DIR}/Windows.Devices.DevicesLowLevelContract.winmd" \
                                                                 "${dir}/Windows.Devices.DevicesLowLevelContract.winmd"
done

# --- Step 6: Place DLLs alongside the game's BT libraries ---
# Mono also probes the directory of the referencing assembly (ApplicationBase fallback)
echo "  Copying contract DLLs next to game BT libraries and executable..."
for dir in "${BT_LIB_DIR}" "${BINARIES_DIR}"; do
    cp "${CACHE_DIR}/Windows.winmd"                                  "${dir}/Windows.dll"
    cp "${CACHE_DIR}/Windows.Foundation.UniversalApiContract.winmd"  "${dir}/Windows.Foundation.UniversalApiContract.dll"
    cp "${CACHE_DIR}/Windows.Foundation.FoundationContract.winmd"    "${dir}/Windows.Foundation.FoundationContract.dll"
    cp "${CACHE_DIR}/Windows.Devices.DevicesLowLevelContract.winmd"  "${dir}/Windows.Devices.DevicesLowLevelContract.dll"
done

# --- Step 7: Patch WindowsConnectivity10.dll: stub out IsBluetoothEnabled() ---
# Wine does not implement Windows.Devices.Radios, so Radio.GetRadiosAsync() throws
# MissingMethodException at runtime.  The method IsBluetoothEnabled() is a sync
# wrapper around the async CheckRadioState() which calls GetRadiosAsync().
# We patch its CIL body at file offset 0xee8 (code start of the fat-format method)
# to: ldc.i4.1 (0x17) + ret (0x2a) — return true unconditionally.
# The remaining 98 bytes of the original body become unreachable dead code and are
# harmless.  The fat-format header (InitLocals, local var sig) is left unchanged.
BT_DLL="${BT_LIB_DIR}/WindowsConnectivity10.dll"
echo "  Patching IsBluetoothEnabled() in WindowsConnectivity10.dll..."
python3 - "${BT_DLL}" << 'PYEOF'
import sys, struct
path = sys.argv[1]
with open(path, 'rb') as f:
    data = bytearray(f.read())

# Expected code start offset and first 3 bytes as a sanity guard
CODE_OFF = 0xee8
EXPECTED = bytes([0x00, 0x02, 0x28])  # nop; ldarg.0; call
actual = bytes(data[CODE_OFF:CODE_OFF+3])
if actual != EXPECTED:
    # Already patched or unexpected content — skip safely
    if bytes(data[CODE_OFF:CODE_OFF+2]) == bytes([0x17, 0x2a]):
        print("    Already patched, skipping.")
        sys.exit(0)
    print(f"    WARNING: unexpected bytes at 0x{CODE_OFF:x}: {actual.hex()} — skipping patch.")
    sys.exit(0)

data[CODE_OFF]   = 0x17  # ldc.i4.1  (push true)
data[CODE_OFF+1] = 0x2a  # ret

with open(path, 'wb') as f:
    f.write(data)
print("    Done.")
PYEOF

echo ""
echo "==> Fix applied successfully!"
echo ""
echo "    Run the game with:"
echo "    WINEPREFIX=${BOTTLE} wine ${SCRIPT_DIR}/MyWhoosh/MyWhoosh.exe"
