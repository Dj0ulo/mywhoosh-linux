#!/usr/bin/env bash
# Build libusb0.dll, the ANT+ bridge, for the Wine prefix.
#
#   ./build.sh
#
# Needs clang and lld (lld-link, llvm-dlltool): a cross build with no Windows
# SDK and no MinGW, since the DLL imports only kernel32 and has no C runtime.
set -e
cd "$(dirname "$0")"
mkdir -p build

# The import library is generated from the functions libusb0.c declares.
{
    echo "LIBRARY kernel32.dll"
    echo "EXPORTS"
    sed -n 's/^IMP [^(]*[ *]\([A-Za-z]*\)(.*/\1/p' libusb0.c
} > build/kernel32.def
llvm-dlltool -m i386:x86-64 -d build/kernel32.def -l build/kernel32.lib

clang --target=x86_64-pc-windows-msvc -O2 -Wall -Werror \
      -ffreestanding -fno-builtin -fno-stack-protector -mno-stack-arg-probe \
      -c libusb0.c -o build/libusb0.obj
lld-link /dll /nodefaultlib /entry:DllMain /out:build/libusb0.dll \
         build/libusb0.obj build/kernel32.lib

echo "built build/libusb0.dll"
