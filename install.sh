#!/bin/bash

if [ -z "$1" ]; then
    echo "Error: Please provide the installation directory as an argument"
    exit 1
fi

mkdir -p "$1"

prefix="$1/wineprefix"

mywhoosh_dir="$1/mywhoosh"
if [ ! -d "$mywhoosh_dir" ]; then
    mkdir -p "$mywhoosh_dir"
fi
mywhoosh_last_version_dir="$mywhoosh_dir/$(ls -1 "$mywhoosh_dir" | sort -V | tail -1)"
mywhoosh_ms_store_id="9ndh0f2vhzx2"

if [ ! -d "$mywhoosh_last_version_dir/MyWhoosh/Binaries/Win64" ]; then
    ./get_msstore_download_links.py "$mywhoosh_ms_store_id" --download --extract "$mywhoosh_dir" || { exit 1; }
    mywhoosh_last_version_dir="$mywhoosh_dir/$(ls -1 "$mywhoosh_dir" | sort -V | tail -1)"
fi
if [ ! -f "$mywhoosh_last_version_dir/MyWhoosh/Binaries/Win64/MyWhoosh-Win64-Shipping.exe" ]; then
    ./patch.py "$mywhoosh_last_version_dir" || { exit 1; }
fi

mkdir -p "$prefix"
# TODO make sure winetricks is installed
if [ ! -f "$prefix/drive_c/windows/syswow64/d3d11.dll" ]; then
    WINEPREFIX="$prefix" winetricks dxvk
fi

# TODO make sure sudo winetricks --self-update was done 
# This shouldn't be necessary with wine 11
# WINEPREFIX="$prefix" winetricks vcrun2022

cat > "$1/start_mywhoosh.sh" << 'EOF'
#!/bin/bash
WINEPREFIX="$(dirname "$0")/wineprefix" wine "$(dirname "$0")/mywhoosh/$(ls -1 "$(dirname "$0")/mywhoosh" | sort -V | tail -1)/MyWhoosh/Binaries/Win64/MyWhoosh-Win64-Shipping.exe"
EOF
chmod +x "$1/start_mywhoosh.sh"

echo "MyWhoosh installed successfully"
echo "Start MyWhoosh with '$1/start_mywhoosh.sh'"