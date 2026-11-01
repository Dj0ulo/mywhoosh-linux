#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p build
mcs -target:library -out:build/BonjourCompat.dll src/BonjourCompat.cs
mcs -r:build/BonjourCompat.dll -r:System.ServiceProcess -out:build/SetupBonjour.exe src/SetupBonjour.cs
cp src/PatchComWrapper.cs build/
printf 'Built BonjourCompat.dll and SetupBonjour.exe; the framework is generated at installation.\n'