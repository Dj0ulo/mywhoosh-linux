#!/bin/bash

if [ -z "$1" ]; then
    echo "Error: Please provide the installation directory as an argument"
    exit 1
fi

mkdir -p "$1"

mywhoosh="$1/mywhoosh"
prefix="$1/wineprefix"

if [ ! -d "$mywhoosh/MyWhoosh/Binaries/Win64" ]; then
    ./mywhoosh.py "$mywhoosh" || { exit 1; }
fi
if [ ! -f "$mywhoosh/MyWhoosh/Binaries/Win64/MyWhoosh-Win64-Shipping.exe" ]; then
    ./patch.py "$mywhoosh" || { exit 1; }
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
WINEPREFIX="$(dirname "$0")/wineprefix" wine "$(dirname "$0")/mywhoosh/mywhoosh.exe"
EOF
chmod +x "$1/start_mywhoosh.sh"

echo "MyWhoosh installed successfully"
echo "Start MyWhoosh with '$1/start_mywhoosh.sh'"