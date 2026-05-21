#!/bin/bash

if [ -z "$1" ]; then
    echo "Error: Please provide the installation directory as an argument"
    exit 1
fi

mywhoosh="$1/mywhoosh"
prefix="$1/wineprefix"


echo "Uninstalling MyWhoosh..."
echo "Removing MyWhoosh files..."
rm -r "$mywhoosh"
echo "Removing Wine prefix..."
rm -r "$prefix"

rm "$1/start_mywhoosh.sh"

rmdir "$1"

echo "MyWhoosh uninstalled"